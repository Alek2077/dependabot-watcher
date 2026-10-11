from __future__ import annotations

import copy
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

import pytest

from dependabot_watcher import maintenance_completion_runtime
from dependabot_watcher.maintenance_completion_runtime import evidence_digest, observe_completion
from dependabot_watcher.config import load_config
from dependabot_watcher.execution import _action_id
from dependabot_watcher.local_sync import confirm_sync, prepare_sync, selected_checkouts
from dependabot_watcher.review_actions import ActionReviews, _digest
from dependabot_watcher.review_queue import ReviewQueue
from dependabot_watcher.reconciliation import reconcile
from dependabot_watcher.planning import _overall_plan_identifier, _repository_plan_identifier
from dependabot_watcher.review_queue import ReviewQueueError
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json, load_run_state, run_lock, save_private_json, save_run_state
from test_review_actions import action_case, reviews_case, saved_review
from test_review_queue import persist_plan
from test_supervisor_workspace import git
from test_reconciliation import _no_work_inspection


@pytest.fixture
def completed_case(reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return record_completed_merge(reviews_case, monkeypatch)


def record_completed_merge(reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    review = saved_review(reviews_case)
    outcome = reviews_case["reviews"].confirm(review["review_id"])
    reviews_case["review_id"] = review["review_id"]
    reviews_case["merge_sha"] = outcome["completion"]["merge_commit_sha"]
    queue = reviews_case["queue"]
    report = copy.deepcopy(reviews_case["report"])
    repository = report["repositories"][0]
    repository["recommended_order"] = []
    repository["evidence_snapshot"]["base_sha"] = reviews_case["merge_sha"]
    core = {key: value for key, value in repository.items() if key != "plan_id"}
    repository["plan_id"] = _repository_plan_identifier(core)
    report["plan_id"] = _overall_plan_identifier(report["status"], [repository["plan_id"]])
    persist_plan(queue.directory, report)
    state = load_run_state(queue.directory)
    state["last_attempt"] = {"completed_at": queue.clock().isoformat()}
    save_run_state(queue.directory, state)

    def fetch(repository: str, branch: str) -> str:
        # This is the transport seam, not a substitute journal/queue writer.
        # Both actual status calls must leave the shared operation lock free.
        with run_lock(queue.directory):
            return reviews_case["merge_sha"]

    monkeypatch.setattr(reviews_case["client"], "fetch_branch_head", fetch, raising=False)
    return reviews_case


def test_selected_loop_uses_the_existing_real_git_writer_and_requires_separate_confirmation(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    checkout = tmp_path / "selected-checkout"
    checkout.mkdir()
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "user.name", "Synthetic Operator")
    git(checkout, "config", "user.email", "operator@example.invalid")
    git(checkout, "remote", "add", "origin", "https://github.com/example-org/example-app.git")
    (checkout / "app.txt").write_text("before\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Initial")
    before = git(checkout, "rev-parse", "HEAD")
    git(checkout, "switch", "-c", "remote-update")
    (checkout / "app.txt").write_text("after\n")
    git(checkout, "commit", "-am", "Synthetic merged revision")
    merged = git(checkout, "rev-parse", "HEAD")
    git(checkout, "switch", "main")
    config_path = reviews_case["config_path"]
    text = config_path.read_text().replace('default_branch = "main"',
        'default_branch = "main"\ncompletion_policy = "selected_local_sync"')
    config_path.write_text(text + '\n[[repositories.local_sync]]\n'
        f'directory = {json.dumps(str(checkout))}\nbranch = "main"\n')
    queue = ReviewQueue(load_config(config_path), reviews_case["queue"].directory)
    reviews_case["queue"] = queue
    reviews_case["reviews"] = ActionReviews(queue, reviews_case["client"], config_path=config_path)
    merge = reviews_case["client"].merge_pull_request
    monkeypatch.setattr(reviews_case["client"], "merge_pull_request",
        lambda *args, **kwargs: {**merge(*args, **kwargs), "merge_commit_sha": merged})
    case = record_completed_merge(reviews_case, monkeypatch)
    pending = observe_completion(case["reviews"], case["review_id"])
    assert pending["stage"] == "local_sync" and not pending["complete"]
    assert git(checkout, "rev-parse", "HEAD") == before
    target_id = selected_checkouts(queue.config)[0]["target_id"]
    preview = prepare_sync(queue.config, config_path, queue.directory, case["client"], target_id)
    assert preview["status"] == "ready"
    assert git(checkout, "rev-parse", "HEAD") == before
    observing_fetch = case["client"].fetch_branch_head
    # The existing mutating transaction intentionally owns its operation lock;
    # only the completion observer promises unlocked transport inspection.
    monkeypatch.setattr(case["client"], "fetch_branch_head", lambda *args: merged)
    confirmed = confirm_sync(queue.config, config_path, queue.directory, case["client"],
                             preview["review_id"], preview["approval_token"])
    assert confirmed["status"] == "synchronized"
    verification_reads = 0

    def coordinated_fetch(repository: str, branch: str) -> str:
        nonlocal verification_reads
        verification_reads += 1
        if verification_reads == 2:
            # Network work holds neither coordination lock.
            lock_directory = Path(git(checkout, "rev-parse", "--absolute-git-dir")) / "dependabot-watcher-sync"
            with run_lock(lock_directory):
                pass
        return observing_fetch(repository, branch)

    original_local_state = maintenance_completion_runtime._local_state
    final_local_reads = 0

    def coordinated_local_state(*args: Any) -> dict[str, Any]:
        nonlocal final_local_reads
        actual = original_local_state(*args)
        if verification_reads == 2:
            final_local_reads += 1
            # The final recheck follows the writer's queue→checkout lock order.
            for lock_directory in (queue.directory, Path(actual["git_directory"]) / "dependabot-watcher-sync"):
                with pytest.raises(RuntimeStateError, match="Saved state is busy"):
                    with run_lock(lock_directory):
                        pytest.fail("Completion did not coordinate its final local recheck")
        return actual

    monkeypatch.setattr(maintenance_completion_runtime, "_local_state", coordinated_local_state)
    monkeypatch.setattr(case["client"], "fetch_branch_head", coordinated_fetch)
    complete = observe_completion(case["reviews"], case["review_id"])
    assert complete["complete"] and complete["targets"][0]["status"] == "verified"
    assert verification_reads == 2
    assert final_local_reads == 1
    assert git(checkout, "rev-parse", "HEAD") == merged
    assert git(checkout, "status", "--porcelain") == ""
    assert len(case["client"].merges) == 1


def test_remote_only_completion_records_fresh_evidence_without_replaying_merge(completed_case: dict[str, Any]) -> None:
    result = observe_completion(completed_case["reviews"], completed_case["review_id"])
    assert result["complete"] is True and result["stage"] == "completed"
    assert result["targets"] == []
    assert result["verification"]["base_sha"] == completed_case["merge_sha"]
    saved = load_private_json(completed_case["queue"].directory, Path("review-actions.json"))
    assert saved["reviews"][completed_case["review_id"]]["completion"]["finalization"] == result["verification"]
    assert len(completed_case["client"].merges) == 1


@pytest.mark.parametrize("offset", [-100, 100])
def test_actual_empty_reconciliation_retains_completion_evidence_without_restoring_merge_authority(
    completed_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, offset: int,
) -> None:
    case = completed_case
    queue = case["queue"]
    review = case["reviews"].snapshot()[0]
    consent = datetime.fromisoformat(review["consented_at"].replace("Z", "+00:00"))
    inspection = _no_work_inspection()
    repository = inspection["repositories"][0]
    repository["evidence_snapshot"]["base_sha"] = case["merge_sha"]
    repository["manifest_evidence"]["base_sha"] = case["merge_sha"]
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: inspection)
    result = reconcile(queue.config, case["client"], config_path=case["config_path"], state_directory=queue.directory,
                       clock=lambda: consent + timedelta(microseconds=offset), notification_mode="off")
    assert result["outcome"] == "no_work" and result["plan"] is None
    assert load_run_state(queue.directory)["latest_plan"]["plan_id"] is None
    context = queue.supervision_context("example-org/example-app")
    assert context["evidence_status"] == "complete" and context["proposals"] == []
    progress = observe_completion(case["reviews"], case["review_id"])
    assert progress["complete"] is (offset > 0)
    assert progress["stage"] == ("completed" if offset > 0 else "refresh")
    assert len(case["client"].merges) == 1


@pytest.mark.parametrize("previously_finalized", [False, True])
def test_remote_advancement_does_not_finalize_old_collected_state(
    completed_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, previously_finalized: bool,
) -> None:
    if previously_finalized:
        assert observe_completion(completed_case["reviews"], completed_case["review_id"])["complete"]
    monkeypatch.setattr(completed_case["client"], "fetch_branch_head", lambda *args: "d" * 40)
    result = observe_completion(completed_case["reviews"], completed_case["review_id"])
    assert result["complete"] is False and result["stage"] == "refresh"
    assert completed_case["reviews"].snapshot()[0]["completion"]["finalization"] is None
    assert len(completed_case["client"].merges) == 1


def test_final_remote_race_is_observed_without_replay(
    completed_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter([completed_case["merge_sha"], "d" * 40])
    monkeypatch.setattr(completed_case["client"], "fetch_branch_head", lambda *args: next(values))
    result = observe_completion(completed_case["reviews"], completed_case["review_id"])
    assert result["stage"] == "refresh" and not result["complete"]
    assert completed_case["reviews"].snapshot()[0]["completion"]["finalization"] is None


def test_context_change_during_live_inspection_does_not_accept_completion(
    completed_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = completed_case["queue"]
    original = queue._supervision_context_under_lock
    calls = 0

    def changed(repository: str) -> dict[str, Any]:
        nonlocal calls
        context = original(repository)
        calls += 1
        if calls > 1:
            context["operation"]["mutation_enabled"] = False
        return context

    monkeypatch.setattr(queue, "_supervision_context_under_lock", changed)
    with pytest.raises(ReviewQueueError, match="evidence changed"):
        observe_completion(completed_case["reviews"], completed_case["review_id"])
    assert completed_case["reviews"].snapshot()[0]["completion"]["finalization"] is None


def test_two_completed_actions_do_not_share_post_merge_completion_context(completed_case: dict[str, Any]) -> None:
    queue = completed_case["queue"]
    state = load_private_json(queue.directory, Path("review-actions.json"))
    later = copy.deepcopy(state["reviews"][completed_case["review_id"]])
    later["review_id"] = "review_" + "f" * 32
    binding = later["preparation"]["binding"]
    binding["pull_request"] += 1
    action_id = _action_id(binding)
    later["preparation"]["action_id"] = action_id
    later["preparation_digest"] = _digest(later["preparation"])
    later["result"]["binding"] = copy.deepcopy(binding)
    later["result"]["action_id"] = action_id
    later["completion"]["action_id"] = action_id
    state["reviews"][later["review_id"]] = later
    save_private_json(queue.directory, Path("review-actions.json"), state)
    before = (queue.directory / "review-actions.json").read_bytes()
    with pytest.raises(ReviewQueueError, match="different action"):
        observe_completion(completed_case["reviews"], completed_case["review_id"])
    assert (queue.directory / "review-actions.json").read_bytes() == before


def test_historical_check_cannot_borrow_a_different_actions_current_purpose(
    completed_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = completed_case["queue"]
    original = queue._supervision_context_under_lock
    journal = queue.directory / "review-actions.json"
    before = journal.read_bytes()

    def different_action(repository: str) -> dict[str, Any]:
        context = original(repository)
        context["maintenance_completion"]["review_id"] = "review_" + "f" * 32
        return context

    monkeypatch.setattr(queue, "_supervision_context_under_lock", different_action)
    with pytest.raises(ReviewQueueError, match="different action"):
        observe_completion(completed_case["reviews"], completed_case["review_id"])
    assert journal.read_bytes() == before
    assert len(completed_case["client"].merges) == 1


def test_assessment_evidence_identity_ignores_only_historical_display_receipts() -> None:
    context: dict[str, Any] = {"repository_plan": {"base_sha": "a" * 40}, "operation": {"mutation_enabled": True},
                               "maintenance_completion": {"action_id": "action_a"}}
    assert evidence_digest(context) == evidence_digest({**context, "previous_assessment": {"summary": "old"},
                                                       "local_sync_observations": [{"status": "completed"}]})
    assert evidence_digest(context) != evidence_digest({**context, "operation": {"mutation_enabled": False}})
    assert evidence_digest(context) != evidence_digest({**context, "maintenance_completion": {"action_id": "action_b"}})
    # Tool upgrades change job freshness, not the identity of repository evidence
    # supporting an already-finalized point-in-time maintenance milestone.
    assert evidence_digest(context) == evidence_digest({**context,
        "validation_capability_revision": "merged_action_v1"})


@pytest.mark.parametrize('conclusion', ['ready_for_completion', 'blocked'])
def test_capability_upgrade_requires_new_pending_assessment_but_preserves_finalized_evidence(
    completed_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, conclusion: str,
) -> None:
    case = completed_case
    jobs = SupervisorJobs(case['queue'])
    job = jobs.prepare('example-org/example-app')
    jobs.start(job['job_id'])
    jobs.attach_session(job['job_id'], 'retained_session')
    result = {'summary': 'Saved exact-action assessment.', 'recommendations': [], 'replies': [],
        'maintenance_assessment': {'action_id': job['context']['maintenance_completion']['obligation']['action_id'],
            'conclusion': conclusion, 'explanation': 'Controlled provider evidence.'}}
    try:
        completed = jobs.complete(job['job_id'], result)
    finally:
        jobs.release_owner(job['job_id'])
    # Only the provider-observation seam is controlled; job/context/action
    # admission and persisted result validation use their real implementations.
    monkeypatch.setattr(maintenance_completion_runtime, 'supervision_status', lambda *args: {
        'jobs': [{**completed, 'provider_completion_observed': True}]})
    context = {**job['context'], 'validation_capability_revision': 'merged_action_v1'}
    assert maintenance_completion_runtime._assessment(case['reviews'], context) == (None, None)
    accepted, blocked = maintenance_completion_runtime._assessment(case['reviews'], context, finalized=True)
    if conclusion == 'ready_for_completion':
        assert accepted is not None and accepted['session_id'] == 'retained_session' and blocked is None
    else:
        assert accepted is None and blocked == result['maintenance_assessment']['explanation']


def test_observer_preserves_saved_finalization_across_capability_only_upgrade(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = reviews_case['config_path']
    workspace = tmp_path / 'maintenance'
    workspace.mkdir()
    path.write_text(path.read_text() + '\n[supervisor]\nmode = "approval_first"\n'
        'provider = "codex_app_server"\nmodel = "fixture-model"\nreasoning_effort = "high"\n'
        'expected_account_type = "chatgpt"\naccess = "inherit"\nmodel_use_acknowledged = true\n'
        '[[supervisor.workspaces]]\nrepository = "example-org/example-app"\n'
        f'directory = {json.dumps(str(workspace))}\ninstruction_sources = []\n')
    queue = ReviewQueue(load_config(path), reviews_case['queue'].directory)
    reviews_case['queue'] = queue
    reviews_case['reviews'] = ActionReviews(queue, reviews_case['client'], config_path=path)
    # The pre-merge provider-readiness seam is outside this completion test.
    # Remote action/journal and all completion/finalization writers remain real.
    monkeypatch.setattr('dependabot_watcher.execution.require_supervisor_recommendation', lambda *args: None)
    case = record_completed_merge(reviews_case, monkeypatch)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare('example-org/example-app')
    jobs.start(job['job_id'])
    jobs.attach_session(job['job_id'], 'retained_session')
    result = {'summary': 'Ready after controlled validation.', 'recommendations': [], 'replies': [],
        'maintenance_assessment': {'action_id': job['context']['maintenance_completion']['obligation']['action_id'],
            'conclusion': 'ready_for_completion', 'explanation': 'Controlled provider evidence.'}}
    completed = jobs.complete(job['job_id'], result)
    monkeypatch.setattr(maintenance_completion_runtime, 'supervision_status', lambda *args: {
        'jobs': [{**completed, 'provider_completion_observed': True}]})
    first = observe_completion(case['reviews'], case['review_id'])
    assert first['complete']
    before = case['reviews'].snapshot()[0]['completion']
    assert before['assessment_required'] and before['finalization'] is not None
    original = queue._supervision_context_under_lock
    monkeypatch.setattr(queue, '_supervision_context_under_lock', lambda *args, **kwargs: {
        **original(*args, **kwargs), 'validation_capability_revision': 'merged_action_v1'})
    second = observe_completion(case['reviews'], case['review_id'])
    assert second['complete'] and second['verification']['context_digest'] == first['verification']['context_digest']
    after = case['reviews'].snapshot()[0]['completion']
    assert after['assessment'] == before['assessment'] and after['finalization'] is not None
    monkeypatch.setattr(case['client'], 'fetch_branch_head', lambda *args: 'f' * 40)
    contradicted = observe_completion(case['reviews'], case['review_id'])
    assert not contradicted['complete'] and contradicted['stage'] == 'refresh'
    assert case['reviews'].snapshot()[0]['completion']['finalization'] is None
    assert len(case['client'].merges) == 1
