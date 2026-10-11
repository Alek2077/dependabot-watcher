from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dependabot_watcher.config import load_config
from dependabot_watcher.execution import ExecutionError, approval_token, execute_approved
from dependabot_watcher import review_actions, review_bridge
from dependabot_watcher.github import GhCliClient
from dependabot_watcher.review_actions import ActionReviews, prepare_review_action
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json, run_lock, save_private_json
from test_execution import ExecutionClient, refresh_plan_identities, saved_plan
from test_review_queue import decision, persist_plan, snapshot
from test_reconciliation import _inspection


@pytest.fixture
def action_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'schema_version = 1\n[operation]\nautonomy = "approval_required"\n'
        'mutation_enabled = true\n[[repositories]]\nname = "example-org/example-app"\n'
        'default_branch = "main"\n[repositories.workflow]\nmerge_method = "squash"\n'
        'required_checks = ["CI"]\n', encoding="utf-8",
    )
    config_path.chmod(0o600)
    queue = ReviewQueue(load_config(config_path), tmp_path / "state")
    report = saved_plan()
    persist_plan(queue.directory, report)
    selected = snapshot(queue)["proposals"][0]

    def fresh_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Live reads cannot occur under the local operation lock. The actual
        # executor must be able to acquire this same lock for confirmation.
        with run_lock(queue.directory):
            return copy.deepcopy(report)

    monkeypatch.setattr("dependabot_watcher.execution.plan", fresh_plan)
    return {"queue": queue, "client": ExecutionClient(), "config_path": config_path,
            "report": report, "selected": selected}


def prepare(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    selected = case["selected"]
    arguments = {"queue": case["queue"], "client": case["client"],
                 "config_path": case["config_path"],
                 "proposal_id": selected["proposal_id"], "version": selected["version"]}
    return prepare_review_action(**{**arguments, **overrides})


def test_preparation_uses_real_executor_and_backend_identity_without_authorizing(
    action_case: dict[str, Any],
) -> None:
    result = prepare(action_case)
    assert result["status"] == "ready"
    assert result["proposal_id"] == action_case["selected"]["proposal_id"]
    assert result["plan_id"] == action_case["report"]["plan_id"]
    assert result["binding"]["repository"] == "example-org/example-app"
    assert result["binding"]["pull_request"] == 42
    assert result["binding"]["head_sha"] == "a" * 40
    assert result["binding"]["expected_base"] == "main"
    assert result["binding"]["merge_method"] == "squash"
    assert result["decision"]["required_checks"] == [{"name": "CI", "state": "passing"}]
    assert result["capabilities"] == {"github_mutations": False, "model_calls": False}
    assert result["mutation_performed"] is result["mutation_authorized"] is False
    assert "approval_token" not in json.dumps(result)
    assert action_case["client"].fetches == 1
    assert action_case["client"].merges == []
    assert not (action_case["queue"].directory / "execution-audit.jsonl").exists()


@pytest.mark.parametrize("change", ["read_only", "required_checks", "merge_method", "missing"])
@pytest.mark.parametrize("during_collection", [False, True])
def test_configuration_revocation_blocks_preparation(
    action_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    change: str, during_collection: bool,
) -> None:
    def revoke() -> None:
        path = action_case["config_path"]
        if change == "missing":
            path.unlink()
        else:
            text = path.read_text()
            if change == "read_only":
                text = text.replace('"approval_required"', '"read_only"').replace(
                    "mutation_enabled = true", "mutation_enabled = false")
            elif change == "required_checks":
                text = text.replace('["CI"]', '["CI", "Integration"]')
            else:
                text = text.replace('"squash"', '"merge"')
            path.write_text(text)

    if during_collection:
        original = action_case["client"].fetch_pull_request_state

        def fetch(*args: Any) -> dict[str, Any]:
            revoke()
            return original(*args)

        monkeypatch.setattr(action_case["client"], "fetch_pull_request_state", fetch)
    else:
        revoke()
    with pytest.raises(ReviewQueueError, match="Configuration"):
        prepare(action_case)
    assert action_case["client"].merges == []
    assert action_case["client"].fetches == int(during_collection)


@pytest.mark.parametrize("choice", ["reject", "defer"])
def test_rejected_or_deferred_selection_cannot_prepare(
    action_case: dict[str, Any], choice: str,
) -> None:
    queue = action_case["queue"]
    request = decision(queue, choice, seconds=60) if choice == "defer" else decision(queue, choice)
    selected = queue.handle(request)["proposals"][0]
    with pytest.raises(ReviewQueueError, match="Undo rejection or deferral"):
        prepare(action_case, version=selected["version"])
    assert action_case["client"].fetches == 0


@pytest.mark.parametrize("change", ["feedback", "reject", "plan"])
def test_changes_during_collection_never_produce_a_confirmable_context(
    action_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    queue = action_case["queue"]

    def changed_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original = copy.deepcopy(action_case["report"])
        if change == "plan":
            changed = copy.deepcopy(original)
            step = changed["repositories"][0]["recommended_order"][0]
            step["identity"]["head_sha"] = "c" * 40
            step["freshness"]["head_sha"] = "c" * 40
            persist_plan(queue.directory, refresh_plan_identities(changed))
        else:
            fields = {"message": "Please reconsider this specific PR."} if change == "feedback" else {}
            queue.handle(decision(queue, change, **fields))
        return original

    monkeypatch.setattr("dependabot_watcher.execution.plan", changed_plan)
    with pytest.raises(ReviewQueueError, match="superseded|handling changed|context changed"):
        prepare(action_case)
    assert action_case["client"].merges == []


def test_already_merged_exact_head_needs_no_action(action_case: dict[str, Any]) -> None:
    action_case["client"].state = "MERGED"
    result = prepare(action_case)
    assert result["status"] == "already_applied"
    assert result["mutation_authorized"] is False
    assert action_case["client"].merges == []


def test_read_only_profile_is_not_implicitly_enabled(action_case: dict[str, Any]) -> None:
    path = action_case["config_path"]
    path.write_text(path.read_text().replace('"approval_required"', '"read_only"').replace(
        "mutation_enabled = true", "mutation_enabled = false"))
    queue = ReviewQueue(load_config(path), action_case["queue"].directory)
    item = snapshot(queue)["proposals"][0]
    with pytest.raises(ExecutionError, match="Execution is disabled"):
        prepare(action_case, queue=queue, proposal_id=item["proposal_id"], version=item["version"])
    assert action_case["client"].fetches == 0


def test_selection_returns_copies_and_never_accepts_stale_versions(action_case: dict[str, Any]) -> None:
    queue, selected = action_case["queue"], action_case["selected"]
    plan, item = queue.current_selection(selected["proposal_id"], selected["version"])
    plan["repositories"][0]["recommended_order"][0]["identity"]["number"] = 999
    item["step"]["identity"]["number"] = 888
    assert snapshot(queue)["proposals"][0]["step"]["identity"]["number"] == 42
    queue.handle(decision(queue, "feedback", message="A new question"))
    with pytest.raises(ReviewQueueError, match="handling changed"):
        prepare(action_case)
    assert action_case["client"].fetches == 0


def test_incomplete_evidence_blocks_before_live_access(action_case: dict[str, Any]) -> None:
    report = action_case["report"]
    report["status"] = "partial"
    report["repositories"][0]["status"] = "partial"
    persist_plan(action_case["queue"].directory, refresh_plan_identities(report))
    selected = snapshot(action_case["queue"])["proposals"][0]
    with pytest.raises(ReviewQueueError, match="complete current evidence"):
        prepare(action_case, proposal_id=selected["proposal_id"], version=selected["version"])
    assert action_case["client"].fetches == 0


def test_failed_local_persistence_blocks_before_live_access(
    action_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeStateError("diagnostic disk failure")

    monkeypatch.setattr("dependabot_watcher.review_queue.save_private_json", fail)
    with pytest.raises(RuntimeStateError, match="disk failure"):
        prepare(action_case)
    assert action_case["client"].fetches == 0


@pytest.mark.parametrize("fields", [{"proposal_id": "/tmp/arbitrary-plan"}, {"version": True}, {"version": -1}])
def test_selection_rejects_malformed_fields_before_live_access(
    action_case: dict[str, Any], fields: dict[str, Any],
) -> None:
    with pytest.raises(ReviewQueueError, match="identity and local version"):
        prepare(action_case, **fields)
    assert action_case["client"].fetches == 0


@pytest.fixture
def reviews_case(action_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Unlike read-only preparation, actual execution deliberately owns run_lock
    # during collection. Replace only transport evidence, not that lock.
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda *args: copy.deepcopy(action_case["report"]))
    action_case["reviews"] = ActionReviews(
        action_case["queue"], action_case["client"], config_path=action_case["config_path"],
    )
    return action_case


def saved_review(case: dict[str, Any]) -> dict[str, Any]:
    item = snapshot(case["queue"])["proposals"][0]
    return case["reviews"].prepare(item["proposal_id"], item["version"])


def test_confirmation_is_durable_separate_and_single_use(reviews_case: dict[str, Any]) -> None:
    review = saved_review(reviews_case)
    assert review["status"] == "awaiting_confirmation"
    assert review["consented_at"] is None
    assert review["completion_stage"] == "merge_outcome"
    assert review["completion"]["policy"] == "remote_only"
    assert reviews_case["client"].merges == []
    assert "approval_token" not in (reviews_case["queue"].directory / "review-actions.json").read_text()
    restarted = ActionReviews(reviews_case["queue"], reviews_case["client"], config_path=reviews_case["config_path"])
    assert restarted.snapshot()[0] == review
    result = restarted.confirm(review["review_id"])
    assert result["status"] == "completed"
    assert result["result"]["status"] == "merged"
    assert result["result"]["mutation_performed"] is True
    assert result["completion"]["merge_commit_sha"] == result["result"]["result"]["merge_commit_sha"]
    assert result["completion_stage"] == "refresh"
    assert result["result"]["post_action_state"]["status"] == "complete"
    assert result["consented_at"] is not None
    assert result["outcome_recorded"] is True
    assert restarted.confirm(review["review_id"]) == result
    assert len(reviews_case["client"].merges) == 1
    assert restarted.snapshot()[0] == result


def test_post_merge_purpose_is_action_bound_and_excludes_its_own_progress(reviews_case: dict[str, Any]) -> None:
    queue = reviews_case["queue"]
    assert "maintenance_completion" not in queue.supervision_context("example-org/example-app")
    review = saved_review(reviews_case)
    assert "maintenance_completion" not in queue.supervision_context("example-org/example-app")
    result = reviews_case["reviews"].confirm(review["review_id"])
    purpose = queue.supervision_context("example-org/example-app")["maintenance_completion"]
    assert purpose["review_id"] == review["review_id"]
    assert purpose["obligation"]["action_id"] == result["preparation"]["action_id"]
    assert purpose["obligation"]["merge_commit_sha"] == result["completion"]["merge_commit_sha"]
    state = load_private_json(queue.directory, Path("review-actions.json"))
    state["reviews"][review["review_id"]]["completion"]["evidence"] = {
        "context_digest": "a" * 64, "base_sha": result["completion"]["merge_commit_sha"]}
    save_private_json(queue.directory, Path("review-actions.json"), state)
    assert queue.supervision_context("example-org/example-app")["maintenance_completion"] == purpose


def test_legacy_action_history_remains_readable_without_invented_completion(reviews_case: dict[str, Any]) -> None:
    review = saved_review(reviews_case)
    queue = reviews_case["queue"]
    state = load_private_json(queue.directory, Path("review-actions.json"))
    del state["reviews"][review["review_id"]]["completion"]
    legacy = asdict(queue.config)
    for repository in legacy["repositories"]:
        repository.pop("completion_policy")
    state["reviews"][review["review_id"]]["config_digest"] = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    preparation = state["reviews"][review["review_id"]]["preparation"]
    preparation["decision"].pop("completion_scope")
    state["reviews"][review["review_id"]]["preparation_digest"] = hashlib.sha256(
        json.dumps(preparation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    save_private_json(queue.directory, Path("review-actions.json"), state)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "completed"
    assert "completion" not in result
    assert "maintenance_completion" not in queue.supervision_context("example-org/example-app")


def test_missing_merge_revision_remains_observable_without_a_second_merge(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    original = reviews_case["client"].merge_pull_request

    def missing_sha(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        result.pop("merge_commit_sha", None)
        return result

    monkeypatch.setattr(reviews_case["client"], "merge_pull_request", missing_sha)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "completed"  # Remote success is not erased.
    assert result["completion_stage"] == "merge_outcome"
    assert "maintenance_completion" not in reviews_case["queue"].supervision_context("example-org/example-app")
    fetch = reviews_case["client"].fetch_pull_request_state
    monkeypatch.setattr(reviews_case["client"], "fetch_pull_request_state",
                        lambda *args: {**fetch(*args), "mergeCommit": {"oid": "c" * 40}})
    observed = reviews_case["reviews"].check_outcome(review["review_id"])
    assert observed["result"] == result["result"]
    assert observed["completion_stage"] == "refresh"
    assert len(reviews_case["client"].merges) == 1


@pytest.mark.parametrize("finalized", [False, True])
def test_bounded_history_preserves_pending_obligations_and_retires_only_verified_ones(
    reviews_case: dict[str, Any], finalized: bool,
) -> None:
    review = saved_review(reviews_case)
    reviews_case["reviews"].confirm(review["review_id"])
    queue = reviews_case["queue"]
    state = load_private_json(queue.directory, Path("review-actions.json"))
    template = state["reviews"][review["review_id"]]
    state["reviews"] = {}
    for index in range(101):
        key = f"review_{index:032x}"
        record = copy.deepcopy(template)
        record["review_id"] = key
        if finalized:
            obligation = record["completion"]
            obligation["evidence"] = {"context_digest": "a" * 64, "base_sha": obligation["merge_commit_sha"]}
            obligation["finalization"] = {**obligation["evidence"], "verified_at": queue.clock().isoformat()}
        state["reviews"][key] = record
    keep = f"review_{100:032x}"
    if not finalized:
        with pytest.raises(ReviewQueueError, match="history is full"):
            review_actions._save_reviews(queue.directory, state, keep=keep)
        assert len(reviews_case["reviews"].snapshot()) == 1
    else:
        review_actions._save_reviews(queue.directory, state, keep=keep)
        records = reviews_case["reviews"].snapshot()
        assert len(records) == 100 and any(item["review_id"] == keep for item in records)
        assert all(item["completion_stage"] == "completed" for item in records)


def test_unconsumed_review_cannot_claim_finalized_maintenance(reviews_case: dict[str, Any]) -> None:
    review = saved_review(reviews_case)
    queue = reviews_case["queue"]
    state = load_private_json(queue.directory, Path("review-actions.json"))
    obligation = state["reviews"][review["review_id"]]["completion"]
    obligation["merge_commit_sha"] = "c" * 40
    obligation["evidence"] = {"context_digest": "a" * 64, "base_sha": "c" * 40}
    obligation["finalization"] = {**obligation["evidence"], "verified_at": queue.clock().isoformat()}
    save_private_json(queue.directory, Path("review-actions.json"), state)
    with pytest.raises(ReviewQueueError, match="Uncompleted action"):
        reviews_case["reviews"].snapshot()
    assert reviews_case["client"].merges == []


def test_pending_completion_blocks_preparation_and_direct_execution_in_the_backend(reviews_case: dict[str, Any]) -> None:
    review = saved_review(reviews_case)
    reviews_case["reviews"].confirm(review["review_id"])
    # Simulate another open action rather than an already-merged no-op.
    reviews_case["client"].state = "OPEN"
    with pytest.raises(ReviewQueueError, match="selected completion"):
        saved_review(reviews_case)
    with pytest.raises(ReviewQueueError, match="selected completion"):
        execute_approved(reviews_case["queue"].config, reviews_case["client"], reviews_case["report"],
            review["preparation"]["binding"]["step_id"], approval_token(review["preparation"]["binding"]),
            state_directory=reviews_case["queue"].directory, config_path=reviews_case["config_path"])
    assert len(reviews_case["client"].merges) == 1


def test_consumption_precedes_live_reads_and_blocks_local_choices_atomically(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    client = reviews_case["client"]
    original = client.fetch_pull_request_state

    def fetch(*args: Any) -> dict[str, Any]:
        data = load_private_json(reviews_case["queue"].directory, Path("review-actions.json"))
        assert data["reviews"][review["review_id"]]["status"] == "executing"
        assert data["reviews"][review["review_id"]]["consented_at"] is not None
        with pytest.raises(RuntimeStateError, match="Saved state is busy"):
            snapshot(reviews_case["queue"])
        # Durable result reads do not take the execution lock or auto-retry.
        assert reviews_case["reviews"].snapshot()[0]["status"] == "recovery_required"
        return original(*args)

    monkeypatch.setattr(client, "fetch_pull_request_state", fetch)
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "completed"
    assert len(client.merges) == 1


@pytest.mark.parametrize("change", ["feedback", "reject", "plan", "config"])
def test_local_context_change_between_read_and_executor_lock_blocks_the_action(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    review = saved_review(reviews_case)
    actual_execute = review_actions.execute_approved

    def execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if change in {"feedback", "reject"}:
            fields = {"message": "Do not use my earlier confirmation."} if change == "feedback" else {}
            reviews_case["queue"].handle(decision(reviews_case["queue"], change, **fields))
        elif change == "plan":
            changed = copy.deepcopy(reviews_case["report"])
            changed["repositories"][0]["recommended_order"][0]["identity"]["title"] = "Changed context"
            persist_plan(reviews_case["queue"].directory, refresh_plan_identities(changed))
        else:
            path = reviews_case["config_path"]
            path.write_text(path.read_text().replace("mutation_enabled = true", "mutation_enabled = false"))
        return actual_execute(*args, **kwargs)

    monkeypatch.setattr(review_actions, "execute_approved", execute)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "invalidated"
    assert result["result"]["mutation_performed"] is False
    assert reviews_case["client"].merges == []
    assert not (reviews_case["queue"].directory / "execution-audit.jsonl").exists()


def test_expiry_at_executor_lock_invalidates_without_consuming_or_merging(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    reviews_case["queue"].clock = lambda: now[0]
    review = saved_review(reviews_case)
    actual_execute = review_actions.execute_approved

    def execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        now[0] += timedelta(minutes=15)
        return actual_execute(*args, **kwargs)

    monkeypatch.setattr(review_actions, "execute_approved", execute)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "expired"
    assert result["consented_at"] is None
    assert reviews_case["client"].merges == []


def test_failure_to_save_consumption_prevents_merge(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    real_save = review_actions.save_private_json

    def save(directory: Path, path: Path, value: Any, **kwargs: Any) -> Path:
        if any(item["status"] == "executing" for item in value["reviews"].values()):
            raise RuntimeStateError("diagnostic consumption save failure")
        return real_save(directory, path, value, **kwargs)

    monkeypatch.setattr(review_actions, "save_private_json", save)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "invalidated"
    assert reviews_case["client"].merges == []
    assert result["consented_at"] is None


def test_confirmed_merge_survives_outcome_write_failure(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    real_save = review_actions.save_private_json

    def save(directory: Path, path: Path, value: Any, **kwargs: Any) -> Path:
        if any(item["status"] == "completed" for item in value["reviews"].values()):
            raise RuntimeStateError("diagnostic terminal save failure")
        return real_save(directory, path, value, **kwargs)

    monkeypatch.setattr(review_actions, "save_private_json", save)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["result"]["status"] == "merged"
    assert result["result"]["mutation_performed"] is True
    assert result["outcome_recorded"] is False
    assert len(reviews_case["client"].merges) == 1
    assert reviews_case["reviews"].snapshot()[0]["status"] == "recovery_required"
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "recovery_required"
    assert len(reviews_case["client"].merges) == 1


def test_lost_remote_reply_is_unknown_and_never_automatically_retried(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    client = reviews_case["client"]
    original = client.merge_pull_request

    def merge(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("diagnostic lost reply after remote success")

    monkeypatch.setattr(client, "merge_pull_request", merge)
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "recovery_required"
    assert result["result"]["mutation_performed"] is None
    assert reviews_case["reviews"].confirm(review["review_id"]) == result
    assert len(client.merges) == 1
    # Exact already-merged observation is a no-op, not a new executable review.
    assert saved_review(reviews_case)["status"] == "already_applied"
    recovered = reviews_case["reviews"].check_outcome(review["review_id"])
    assert recovered["status"] == "completed"
    assert len(client.merges) == 1


def test_unresolved_open_action_blocks_another_confirmation_in_its_repository(
    reviews_case: dict[str, Any],
) -> None:
    review = saved_review(reviews_case)
    reviews_case["client"].merge_error = True
    result = reviews_case["reviews"].confirm(review["review_id"])
    assert result["status"] == "recovery_required"
    with pytest.raises(ReviewQueueError, match="earlier action"):
        saved_review(reviews_case)
    assert len(reviews_case["client"].merges) == 1


def _reload_case_only_repository(case: dict[str, Any], *, replan: bool = False) -> None:
    path = case["config_path"]
    path.write_text(path.read_text().replace("example-org/example-app", "EXAMPLE-ORG/Example-App"))
    directory = case["queue"].directory
    if replan:
        report = case["report"]
        repository = report["repositories"][0]
        repository["repository"] = "EXAMPLE-ORG/Example-App"
        for step in repository["recommended_order"]:
            step["identity"]["repository"] = repository["repository"]
        case["report"] = refresh_plan_identities(report)
        persist_plan(directory, case["report"])
    case["queue"] = ReviewQueue(load_config(path), directory)
    case["reviews"] = ActionReviews(case["queue"], case["client"], config_path=path)


def test_case_alias_configuration_preserves_exact_preparation_binding(reviews_case: dict[str, Any]) -> None:
    original_plan = copy.deepcopy(reviews_case["report"])
    _reload_case_only_repository(reviews_case)
    review = saved_review(reviews_case)
    assert review["preparation"]["binding"]["repository"] == "example-org/example-app"
    assert review["preparation"]["plan_id"] == original_plan["plan_id"]
    assert reviews_case["report"] == original_plan
    assert reviews_case["client"].merges == []


@pytest.mark.parametrize("status", ["executing", "recovery_required"])
def test_case_only_replan_keeps_uncertain_action_blocking_new_approval(
    reviews_case: dict[str, Any], status: str,
) -> None:
    review = saved_review(reviews_case)
    reviews_case["client"].merge_error = True
    reviews_case["reviews"].confirm(review["review_id"])
    state = load_private_json(reviews_case["queue"].directory, Path("review-actions.json"))
    record = state["reviews"][review["review_id"]]
    record.update(status=status, result=None)
    save_private_json(reviews_case["queue"].directory, Path("review-actions.json"), state)
    binding = copy.deepcopy(record["preparation"]["binding"])
    _reload_case_only_repository(reviews_case, replan=True)
    with pytest.raises(ReviewQueueError, match="earlier action"):
        saved_review(reviews_case)
    assert reviews_case["reviews"].snapshot()[0]["preparation"]["binding"] == binding
    assert len(reviews_case["client"].merges) == 1


def test_case_alias_scope_can_reconcile_historical_exact_action_without_replay(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    original_merge = reviews_case["client"].merge_pull_request

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original_merge(*args, **kwargs)
        raise RuntimeError("diagnostic lost reply")

    monkeypatch.setattr(reviews_case["client"], "merge_pull_request", lose_reply)
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "recovery_required"
    _reload_case_only_repository(reviews_case)
    outcome = reviews_case["reviews"].check_outcome(review["review_id"])
    assert outcome["status"] == "completed"
    assert outcome["result"]["mutation_performed"] is False
    assert outcome["preparation"]["binding"] == review["preparation"]["binding"]
    assert len(reviews_case["client"].merges) == 1


def test_duplicate_confirmation_during_execution_only_reads_pending_outcome(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    entered, release = threading.Event(), threading.Event()
    actual_fetch = reviews_case["client"].fetch_pull_request_state
    results: list[dict[str, Any]] = []

    def fetch(*args: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return actual_fetch(*args)

    monkeypatch.setattr(reviews_case["client"], "fetch_pull_request_state", fetch)
    worker = threading.Thread(target=lambda: results.append(reviews_case["reviews"].confirm(review["review_id"])))
    worker.start()
    try:
        assert entered.wait(5)
        duplicate = reviews_case["reviews"].confirm(review["review_id"])
        assert duplicate["status"] == "recovery_required"
        assert duplicate["result"] is None
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0]["status"] == "completed"
    assert len(reviews_case["client"].merges) == 1


@pytest.mark.parametrize("corruption", ["digest", "schema", "consent", "timestamp", "binding"])
def test_corrupt_action_history_fails_closed_before_executor(
    reviews_case: dict[str, Any], corruption: str,
) -> None:
    review = saved_review(reviews_case)
    directory = reviews_case["queue"].directory
    state = load_private_json(directory, Path("review-actions.json"))
    record = state["reviews"][review["review_id"]]
    if corruption == "digest":
        record["preparation"]["decision"]["title"] = "False display context"
    elif corruption == "schema":
        state["schema_version"] = True
    elif corruption == "consent":
        record["status"] = "executing"
    elif corruption == "timestamp":
        record["expires_at"] = "invalid"
    else:
        record["preparation"]["binding"]["head_sha"] = "short"
        record["preparation_digest"] = review_actions._digest(record["preparation"])
    save_private_json(directory, Path("review-actions.json"), state)
    with pytest.raises(ReviewQueueError):
        reviews_case["reviews"].confirm(review["review_id"])
    assert reviews_case["client"].merges == []


def test_read_only_outcome_check_resolves_lost_reply_without_retry(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    original = reviews_case["client"].merge_pull_request
    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("lost reply")
    monkeypatch.setattr(reviews_case["client"], "merge_pull_request", lose_reply)
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "recovery_required"
    outcome = reviews_case["reviews"].check_outcome(review["review_id"])
    assert outcome["status"] == "completed"
    assert outcome["result"]["status"] == "already_applied"
    assert outcome["result"]["watcher_success_recorded"] is False
    assert outcome["result"]["mutation_performed"] is False
    assert reviews_case["reviews"].snapshot()[0] == outcome
    assert reviews_case["reviews"].confirm(review["review_id"]) == outcome
    assert len(reviews_case["client"].merges) == 1


def test_recovered_merge_retains_exact_completion_revision_without_replaying(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    original = reviews_case["client"].merge_pull_request

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("lost reply")

    monkeypatch.setattr(reviews_case["client"], "merge_pull_request", lose_reply)
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "recovery_required"
    fetch = reviews_case["client"].fetch_pull_request_state
    monkeypatch.setattr(reviews_case["client"], "fetch_pull_request_state",
                        lambda *args: {**fetch(*args), "mergeCommit": {"oid": "c" * 40}})
    outcome = reviews_case["reviews"].check_outcome(review["review_id"])
    assert outcome["completion"]["merge_commit_sha"] == "c" * 40
    assert outcome["completion_stage"] == "refresh"
    assert len(reviews_case["client"].merges) == 1


@pytest.mark.parametrize("change", ["head", "base", "number", "open", "removed_scope"])
def test_outcome_check_never_assigns_another_identity_or_replays_consent(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    review = saved_review(reviews_case)
    reviews_case["client"].merge_error = True
    reviews_case["reviews"].confirm(review["review_id"])
    def fetch(*args: Any) -> dict[str, Any]:
        return {"number": 43 if change == "number" else 42,
                "state": "OPEN" if change == "open" else "MERGED",
                "headRefOid": "c" * 40 if change == "head" else "a" * 40,
                "baseRefName": "other" if change == "base" else "main"}
    monkeypatch.setattr(reviews_case["client"], "fetch_pull_request_state", fetch)
    if change == "removed_scope":
        path = reviews_case["config_path"]
        path.write_text(path.read_text().replace("example-org/example-app", "example-org/another-app"))
        controller = ActionReviews(ReviewQueue(load_config(path), reviews_case["queue"].directory),
                                   reviews_case["client"], config_path=path)
        with pytest.raises(ReviewQueueError, match="scope"):
            controller.check_outcome(review["review_id"])
    else:
        assert reviews_case["reviews"].check_outcome(review["review_id"])["status"] == "recovery_required"
    assert len(reviews_case["client"].merges) == 1


def action_exchange(case: dict[str, Any], operation: str, **fields: Any) -> tuple[int, dict[str, Any]]:
    request = {"schema_version": 1, "request_id": "action_" + "f" * 32,
               "operation": operation, **fields}
    output = io.BytesIO()
    code = exchange(case["queue"], io.BytesIO(json.dumps(request).encode()), output, actions=case["reviews"])
    return code, json.loads(output.getvalue())


def test_real_bridge_action_roundtrip_and_restart(reviews_case: dict[str, Any]) -> None:
    item = reviews_case["selected"]
    code, prepared = action_exchange(reviews_case, "prepare_action", proposal_id=item["proposal_id"], version=item["version"])
    assert code == 0 and prepared["request_id"] == "action_" + "f" * 32
    assert reviews_case["client"].merges == []
    reviews_case["reviews"] = ActionReviews(reviews_case["queue"], reviews_case["client"], config_path=reviews_case["config_path"])
    code, confirmed = action_exchange(reviews_case, "confirm_action", review_id=prepared["review"]["review_id"])
    assert code == 0 and confirmed["review"]["result"]["status"] == "merged"
    code, history = action_exchange(reviews_case, "action_history")
    assert code == 0 and history["reviews"] == [confirmed["review"]]
    assert "approval_token" not in json.dumps(history)
    assert len(reviews_case["client"].merges) == 1


@pytest.mark.parametrize("timeout_seconds", [1, 120, 3600])
def test_bridge_entrypoint_honors_configured_github_command_timeout(
    action_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, timeout_seconds: int,
) -> None:
    path = action_case["config_path"]
    path.write_text(path.read_text() + f"\n[collection]\ncommand_timeout_seconds = {timeout_seconds}\n")
    calls: list[tuple[list[str], int]] = []

    def runner(command: Sequence[str], timeout: int) -> str:
        calls.append((list(command), timeout))
        return ""

    def client(*, timeout_seconds: int = 120) -> GhCliClient:
        return GhCliClient(timeout_seconds=timeout_seconds, runner=runner)

    real_exchange = review_bridge.exchange

    def checked_exchange(*args: Any, actions: ActionReviews, **kwargs: Any) -> int:
        # Exercise the real GitHub command boundary, replacing only subprocess
        # transport. The native entrypoint must configure this actual client.
        assert isinstance(actions.client, GhCliClient)
        actions.client.validate_auth()
        return real_exchange(*args, actions=actions, **kwargs)

    output = io.BytesIO()
    request = {"schema_version": 1, "request_id": "read_timeout", "operation": "snapshot"}
    monkeypatch.setattr(review_bridge, "GhCliClient", client)
    monkeypatch.setattr(review_bridge, "exchange", checked_exchange)
    monkeypatch.setattr(review_bridge, "sys", SimpleNamespace(
        stdin=SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())),
        stdout=SimpleNamespace(buffer=output),
    ))
    assert review_bridge.main(["--config", str(path), "--state-dir", str(action_case["queue"].directory)]) == 0
    assert calls == [(["gh", "auth", "status", "--active"], timeout_seconds)]
    assert json.loads(output.getvalue())["status"] == "ok"
    assert action_case["client"].merges == []


@pytest.mark.parametrize("operation", ["prepare_action", "confirm_action", "check_action", "action_history", "collect_queue"])
def test_action_wire_rejects_extra_authority_before_any_side_effect(reviews_case: dict[str, Any], operation: str) -> None:
    code, result = action_exchange(reviews_case, operation, binding={"repository": "outside/scope"}, approval_token="injected")
    assert code == 2 and result["status"] == "error"
    assert reviews_case["client"].fetches == 0
    assert reviews_case["client"].merges == []


def test_fresh_collection_replaces_the_plan_without_replaying_an_approved_action(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "completed"
    fresh = _inspection()
    fresh["repositories"][0]["pull_requests"] = []
    fresh["summary"]["open_pull_request_count"] = 0
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: fresh)
    code, view = action_exchange(reviews_case, "collect_queue")
    assert code == 0 and view["collection_status"] == "complete"
    assert view["proposals"] == []
    assert view["reviews"][0]["review_id"] == review["review_id"]
    assert view["reviews"][0]["status"] == "completed"
    assert view["capabilities"] == {"github_mutations": False, "model_calls": False}
    assert len(reviews_case["client"].merges) == 1


def test_fresh_collection_and_historical_outcome_survive_retired_proposal(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = saved_review(reviews_case)
    original_merge = reviews_case["client"].merge_pull_request
    def lost_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original_merge(*args, **kwargs)
        raise RuntimeError("lost reply")
    monkeypatch.setattr(reviews_case["client"], "merge_pull_request", lost_reply)
    assert reviews_case["reviews"].confirm(review["review_id"])["status"] == "recovery_required"
    fresh = _inspection()
    fresh["repositories"][0]["pull_requests"] = []
    fresh["summary"]["open_pull_request_count"] = 0
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: fresh)
    code, view = action_exchange(reviews_case, "collect_queue")
    assert code == 0 and view["proposals"] == []
    assert view["reviews"][0]["status"] == "recovery_required"
    code, outcome = action_exchange(reviews_case, "check_action", review_id=review["review_id"])
    assert code == 0 and outcome["review"]["status"] == "completed"
    assert len(reviews_case["client"].merges) == 1


def test_fresh_collection_does_not_silently_launch_configured_ai(
    reviews_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = reviews_case["config_path"]
    path.write_text(path.read_text() + '\n[advisory]\nmode = "economy"\nprovider = "codex_cli"\n'
                    '[advisory.codex_cli.triage]\nmodel = "test-model"\nreasoning_effort = "high"\n')
    queue = ReviewQueue(load_config(path), reviews_case["queue"].directory)
    reviews_case["queue"] = queue
    reviews_case["reviews"] = ActionReviews(queue, reviews_case["client"], config_path=path)
    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("No collector/provider may be invoked for this unsupported AI-enabled refresh.")
    monkeypatch.setattr(review_actions, "reconcile", unexpected)
    code, view = action_exchange(reviews_case, "collect_queue")
    assert code == 2 and "no model calls" in view["message"]
    assert reviews_case["client"].merges == []
