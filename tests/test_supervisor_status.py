from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from dependabot_watcher.config import PathPolicyOverrides, parse_config
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json, run_lock, save_private_json
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_runner import SupervisorRunner
from dependabot_watcher.supervisor_status import supervision_status
from test_execution import refresh_plan_identities, saved_plan
from test_review_queue import decision, persist_plan, snapshot
from test_supervision import refresh_multi_repository_plan, result_for
from test_supervisor_runner import FixtureProvider
from test_supervisor_launch import launch_case
from test_supervisor_workspace import workspace_case


@pytest.fixture
def status_case(tmp_path: Path) -> tuple[ReviewQueue, SupervisorJobs, dict[str, Any]]:
    queue = ReviewQueue(parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]}), tmp_path / "state")
    persist_plan(queue.directory, saved_plan())
    queue.handle(decision(queue, "feedback", message="What would you check?"))
    jobs = SupervisorJobs(queue)
    return queue, jobs, jobs.prepare("example-org/example-app")


def request() -> dict[str, Any]:
    return {"schema_version": 1, "request_id": "read_supervision", "operation": "supervisor_status",
            "repository": "example-org/example-app"}


def test_status_projects_only_job_bound_storage_blocker(status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]]) -> None:
    queue, _jobs, job = status_case
    relative = Path('supervisor-validation') / job['job_id'] / 'resource-blocker.json'
    record = {'job_id': job['job_id'], 'context_digest': job['context_digest'], 'active': True,
              'maximum_bytes': 1024**3, 'minimum_free_bytes': 1024**3, 'allocated_bytes_after_cleanup': 4096}
    save_private_json(queue.directory, relative, record)
    assert supervision_status(queue, request())['jobs'][0]['validation_blocker'] == record
    record['active'] = False
    save_private_json(queue.directory, relative, record)
    assert supervision_status(queue, request())['jobs'][0]['validation_blocker'] == record
    record['context_digest'] = 'f' * 64
    save_private_json(queue.directory, relative, record)
    with pytest.raises(RuntimeStateError, match='does not match'):
        supervision_status(queue, request())


def test_retry_after_clock_rollback_remains_latest_current_result(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]],
) -> None:
    queue, jobs, parent = status_case
    jobs.start(parent["job_id"])
    failed = jobs.fail(parent["job_id"])
    earlier = datetime.fromisoformat(failed["finished_at"]) - timedelta(seconds=1)
    queue.clock = lambda: earlier
    retry = jobs.prepare(parent["repository"], retry_job_id=parent["job_id"])
    SupervisorRunner(jobs, FixtureProvider(queue)).run(retry["job_id"])
    current = [item for item in supervision_status(queue, request())["jobs"] if item["current_context"]]
    assert [item["job_id"] for item in current] == [parent["job_id"], retry["job_id"]]
    assert current[-1]["status"] == "completed" and current[-1]["provider_completion_observed"] is True


def test_preparation_is_not_agent_delivery(status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]]) -> None:
    queue, _, _ = status_case
    record = supervision_status(queue, request())["jobs"][0]
    assert record["status"] == "prepared"
    assert record["provider_completion_observed"] is False and record["messages"] == []


def test_status_discloses_saved_launch_policy_without_claiming_live_runtime_or_spending(
    launch_case: dict[str, Any],
) -> None:
    case = launch_case
    outgoing = io.BytesIO()
    assert exchange(case["queue"], io.BytesIO(json.dumps(request()).encode()), outgoing) == 0
    view = json.loads(outgoing.getvalue())
    summary = view["configured_launch"]
    assert summary["status"] == "configured" and summary["reason"] is None
    assert summary["model"] == "gpt-5.6-luna" and summary["reasoning_effort"] == "xhigh"
    assert summary["access"] == "inherit" and summary["expected_account_type"] == "chatgpt"
    assert summary["workspace"] == str(case["worktree"])
    assert summary["instruction_sources"] == [str(case["worktree"] / "AGENTS.md")]
    assert summary["requires_fresh_preflight"] is True and summary["live_runtime_verified"] is False
    assert summary["github_mutations_authorized"] is False
    assert summary["usage_effect"] == "subscription_or_api_usage_if_launched"
    assert summary["model_use_acknowledged"] is True and summary["validation_authorized"] is False
    assert case["collections"] == [] and case["launches"] == []
    assert view["context_digest"] == snapshot(case["queue"])["supervisor_context_digests"]["example-org/example-app"]


def test_unconfigured_status_is_readable_but_never_claims_a_launch_is_available(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]],
) -> None:
    queue, _, _ = status_case
    summary = supervision_status(queue, request())["configured_launch"]
    assert summary["status"] == "blocked" and summary["reason"]
    assert summary["model"] is None and summary["workspace"] is None
    assert summary["live_runtime_verified"] is False and summary["github_mutations_authorized"] is False


def test_saved_launch_policy_change_rejects_old_status_instead_of_silently_reloading(
    launch_case: dict[str, Any],
) -> None:
    case = launch_case
    case["path"].write_text(case["path"].read_text().replace('model = "gpt-5.6-luna"', 'model = "gpt-6.1-sol"'))
    with pytest.raises(ReviewQueueError, match="configuration changed"):
        supervision_status(case["queue"], request())
    assert case["collections"] == [] and case["launches"] == []


def test_case_alias_status_uses_same_context_and_preserves_exact_reply_binding(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]],
) -> None:
    queue, jobs, job = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(job["job_id"])
    view = supervision_status(queue, {**request(), "repository": "EXAMPLE-ORG/Example-App"})
    assert view["jobs"][0]["job_id"] == job["job_id"]
    assert view["jobs"][0]["messages"][0]["processing"] == "answered_current"
    assert view["context_digest"] == snapshot(queue)["supervisor_context_digests"][job["repository"]]


@pytest.mark.parametrize("operation", ["status", "read", "prepare"])
def test_scoped_job_reads_do_not_inspect_or_recover_another_repository_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    report = saved_plan()
    second_plan = json.loads(json.dumps(report["repositories"][0]))
    second_plan["repository"] = "example-org/other-app"
    for step in second_plan["recommended_order"]:
        step["identity"]["repository"] = second_plan["repository"]
    report["repositories"].append(second_plan)
    config = parse_config({"schema_version": 1, "repositories": [
        {"name": "example-org/example-app"}, {"name": "example-org/other-app"}]})
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, refresh_multi_repository_plan(report))
    jobs = SupervisorJobs(queue)
    first = jobs.prepare("example-org/example-app")
    second = jobs.prepare("example-org/other-app")
    jobs.start(second["job_id"])
    jobs.release_owner(second["job_id"])
    original = SupervisorJobs._owner_is_live

    def inspect_only_requested(self: SupervisorJobs, job_id: str) -> bool:
        assert job_id != second["job_id"], "Scoped read inspected another repository's owner"
        return original(self, job_id)

    with monkeypatch.context() as patch:
        patch.setattr(SupervisorJobs, "_owner_is_live", inspect_only_requested)
        if operation == "status":
            assert len(supervision_status(queue, request())["jobs"]) == 1
        elif operation == "read":
            assert jobs.read(first["job_id"])["status"] == "prepared"
        else:
            assert jobs.prepare("EXAMPLE-ORG/Example-App")["job_id"] == first["job_id"]
        assert jobs._load()["jobs"][second["job_id"]]["status"] == "running"
    # B's own read still recovers the actual missing OS owner.
    assert jobs.read(second["job_id"])["status"] == "interrupted"


@pytest.mark.parametrize("operation", ["status", "snapshot", "context", "feedback"])
def test_interactive_reads_and_feedback_wait_for_short_journal_transactions(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]], operation: str,
) -> None:
    queue, _, job = status_case
    feedback = decision(queue, "feedback", message="Concurrent saved question")
    started = Event()

    def read() -> dict[str, Any]:
        started.set()
        if operation == "status":
            return supervision_status(queue, request())
        if operation == "snapshot":
            return snapshot(queue)
        if operation == "context":
            return queue.supervision_context(job["repository"])
        return queue.handle(feedback)

    with ThreadPoolExecutor(max_workers=1) as worker:
        with run_lock(queue.directory):
            future = worker.submit(read)
            assert started.wait(1)
            assert not future.done()
        view = future.result(timeout=3)
    if operation == "context":
        assert view["repository"] == job["repository"]
    else:
        assert view["status"] == "ok"
    if operation == "feedback":
        assert snapshot(queue)["proposals"][0]["feedback"][-1]["message"] == "Concurrent saved question"


def test_completed_reply_and_provenance_reach_real_bridge(status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]]) -> None:
    queue, jobs, job = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(job["job_id"])
    outgoing = io.BytesIO()
    assert exchange(queue, io.BytesIO(json.dumps(request()).encode()), outgoing) == 0
    view = json.loads(outgoing.getvalue())
    record = view["jobs"][0]
    assert record["current_context"] is True and record["provider_completion_observed"] is True
    assert record["provenance"]["model"] == "fixture-model"
    assert record["result"]["replies"][0]["request_id"] == record["messages"][0]["request_id"]
    assert record["messages"][0]["processing"] == "answered_current"
    receipt = record["messages"][0]
    original = job["context"]["proposals"][0]
    assert receipt["question"] == original["feedback"][0]["message"]
    assert receipt["created_at"] == original["feedback"][0]["created_at"]
    assert receipt["item"]["repository"] == job["repository"]
    assert receipt["item"]["kind"] == original["step"]["kind"]
    assert receipt["item"]["number"] == original["step"]["identity"]["number"]
    assert receipt["item"]["head_sha"] == original["step"]["identity"]["head_sha"]
    assert view["capabilities"] == {"github_mutations": False, "model_calls": False}
    assert snapshot(queue)["supervisor_context_digests"][job["repository"]] == view["context_digest"] == job["context_digest"]


@pytest.mark.parametrize("change", ["head", "base", "evidence"])
def test_reply_retains_original_item_and_question_across_recommendation_revisions(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]], change: str,
) -> None:
    queue, jobs, job = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(job["job_id"])
    before = supervision_status(queue, request())["jobs"][0]["messages"][0]
    report = saved_plan()
    step = report["repositories"][0]["recommended_order"][0]
    if change == "head":
        step["identity"]["head_sha"] = step["freshness"]["head_sha"] = "e" * 40
    elif change == "base":
        step["identity"]["observed_base"] = step["freshness"]["observed_base"] = "different-base"
    else:
        step["summary"] += " Refreshed evidence."
    persist_plan(queue.directory, refresh_plan_identities(report))
    current = snapshot(queue)["proposals"][0]
    assert current["proposal_id"] != before["proposal_id"]
    assert current["feedback"] == [] and current["handling"] == "pending"
    # A later status read and a new controller preserve the original attribution,
    # independently of the replacement proposal's head, base or feedback list.
    reopened = ReviewQueue(queue.config, queue.directory)
    outgoing = io.BytesIO()
    assert exchange(reopened, io.BytesIO(json.dumps(request()).encode()), outgoing) == 0
    receipt = json.loads(outgoing.getvalue())["jobs"][0]["messages"][0]
    assert receipt == {**before, "processing": "answered_historical"}


def test_status_orders_answers_by_attempt_time_not_random_job_id(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, jobs, first = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(first["job_id"])
    queue.handle(decision(queue, "feedback", message="A later question"))
    with monkeypatch.context() as patch:
        patch.setattr("dependabot_watcher.supervision.secrets.token_hex", lambda size: "0" * (size * 2))
        second = jobs.prepare(first["repository"])
    continued = FixtureProvider(queue)
    with monkeypatch.context() as patch:
        patch.setattr(continued, "resume_session_id", "session-123", raising=False)
        SupervisorRunner(jobs, continued).run(second["job_id"])
    assert second["job_id"] < first["job_id"]
    view = supervision_status(queue, request())
    assert [record["job_id"] for record in view["jobs"]] == [first["job_id"], second["job_id"]]


def test_snapshot_revision_changes_when_policy_keeps_same_proposal_and_version(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]],
) -> None:
    queue, jobs, job = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(job["job_id"])
    before = snapshot(queue)
    queue.config = replace(queue.config, path_policy=PathPolicyOverrides(sensitive_patterns=("src/**",)))
    after = snapshot(queue)
    assert before["proposals"] == after["proposals"]
    repository = job["repository"]
    assert before["supervisor_context_digests"][repository] != after["supervisor_context_digests"][repository]
    view = supervision_status(queue, request())
    assert view["context_digest"] == after["supervisor_context_digests"][repository]
    assert view["jobs"][0]["current_context"] is False


def test_feedback_acknowledgement_carries_its_post_save_context(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]],
) -> None:
    queue, _, job = status_case
    before = snapshot(queue)["supervisor_context_digests"][job["repository"]]
    response = queue.handle(decision(queue, "feedback", message="A new direction"))
    digest = response["supervisor_context_digests"][job["repository"]]
    assert digest != before and digest == supervision_status(queue, request())["context_digest"]


@pytest.mark.parametrize("change", ["message", "policy"])
def test_old_completion_is_not_presented_as_current_after_later_changes(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]], change: str,
) -> None:
    queue, jobs, job = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(job["job_id"])
    if change == "message":
        queue.handle(decision(queue, "feedback", message="New question"))
    else:
        queue.config = replace(queue.config, path_policy=PathPolicyOverrides(sensitive_patterns=("src/**",)))
    record = supervision_status(queue, request())["jobs"][0]
    assert record["status"] == "completed" and record["current_context"] is False
    assert record["messages"][0]["processing"] == "answered_historical"


def test_direct_job_completion_without_provider_evidence_does_not_claim_processing(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]],
) -> None:
    queue, jobs, job = status_case
    jobs.start(job["job_id"])
    jobs.complete(job["job_id"], result_for(job))
    record = supervision_status(queue, request())["jobs"][0]
    assert record["status"] == "completed" and record["provider_completion_observed"] is False
    assert record["messages"] == []


@pytest.mark.parametrize("change", ["session", "revision", "reply", "model"])
def test_mismatched_saved_observation_is_not_displayed(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]], change: str,
) -> None:
    queue, jobs, job = status_case
    SupervisorRunner(jobs, FixtureProvider(queue)).run(job["job_id"])
    path = Path("supervisor-observations") / f'{job["job_id"]}.json'
    observed = load_private_json(queue.directory, path)
    if change == "session":
        observed["session_id"] = "another-session"
    elif change == "revision":
        observed["context_digest"] = "f" * 64
    elif change == "reply":
        observed["analysis"]["replies"][0]["message"] = "different response"
    else:
        observed["model"] = {"not": "a model identity"}
    save_private_json(queue.directory, path, observed)
    with pytest.raises(ReviewQueueError):
        supervision_status(queue, request())


@pytest.mark.parametrize("extra", [{"repository": "unconfigured/other"}, {"command": "codex"}, {"request_id": "decision_1_notread"}])
def test_status_cannot_widen_scope_or_launch_provider(
    status_case: tuple[ReviewQueue, SupervisorJobs, dict[str, Any]], extra: dict[str, Any],
) -> None:
    queue, _, _ = status_case
    with pytest.raises(ReviewQueueError):
        supervision_status(queue, {**request(), **extra})
