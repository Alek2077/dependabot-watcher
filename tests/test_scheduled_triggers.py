from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from dependabot_watcher import scheduled_triggers
from dependabot_watcher.config import load_config
from dependabot_watcher.github import GitHubClient
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewConfigurationChanged, ReviewQueue, ReviewQueueError, supervisor_context_digest
from dependabot_watcher.runtime_state import load_private_json, save_private_json
from dependabot_watcher.scheduled_triggers import claim_trigger, consume_slot, finish_slot
from dependabot_watcher.supervisor_bridge import launch_exchange
from test_review_queue import decision, file_bound_queue, persist_plan
from test_scheduled_supervisor import REPOSITORY, RUN_ID, complete, save_attempt, scheduled_queue
from test_supervisor_launch import launch_case
from test_supervisor_workspace import workspace_case


def receipt(queue: ReviewQueue) -> dict[str, Any]:
    return load_private_json(queue.directory, Path("scheduled-supervisor") / f"{RUN_ID}.json")


def selected(queue: ReviewQueue) -> str:
    return claim_trigger(queue, RUN_ID)["receipt"]["slots"][0]["context_digest"]


def test_claim_is_durable_and_single_use_without_model_launch(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    first = claim_trigger(queue, RUN_ID)
    assert first["claimed"] is True
    assert first["receipt"]["slots"][0]["phase"] == "reserved"
    assert claim_trigger(queue, RUN_ID) == {"claimed": False, "receipt": first["receipt"]}
    assert not (queue.directory / "supervisor-jobs.json").exists()


@pytest.mark.parametrize("initial", ["idle", "attention"])
def test_nonready_selection_does_not_consume_a_future_useful_reservation(
    scheduled_queue: tuple[ReviewQueue, Path], initial: str,
) -> None:
    queue, _ = scheduled_queue
    if initial == "idle":
        queue.handle(decision(queue, "reject"))
    else:
        complete(queue, fail=True)
    first = claim_trigger(queue, RUN_ID)
    assert first["claimed"] is False and first["receipt"]["selection"]["status"] == initial
    assert not (queue.directory / "scheduled-supervisor").exists()
    queue.handle(decision(queue, "feedback", message="A new explicit question for investigation."))
    second = claim_trigger(queue, RUN_ID)
    assert second["claimed"] is True and len(second["receipt"]["slots"]) == 1


def test_concurrent_claims_reserve_only_once(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim_trigger(queue, RUN_ID), range(2)))
    assert sorted(result["claimed"] for result in results) == [False, True]
    assert results[0]["receipt"] == results[1]["receipt"]


def test_slot_remains_consumed_after_latest_run_advances(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    digest = selected(queue)
    save_attempt(queue, "run_" + "b" * 20)
    consume_slot(queue, RUN_ID, REPOSITORY, digest)
    assert receipt(queue)["slots"][0]["phase"] == "started"
    assert claim_trigger(queue, RUN_ID)["claimed"] is False
    with pytest.raises(ReviewQueueError, match="already consumed"):
        consume_slot(queue, RUN_ID, REPOSITORY, digest)


def test_stale_unclaimed_trigger_cannot_be_reserved(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    with pytest.raises(ReviewQueueError, match="latest saved run"):
        claim_trigger(queue, "run_" + "b" * 20)
    assert not (queue.directory / "scheduled-supervisor").exists()


def test_policy_change_cannot_rebind_reserved_budget(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, path = scheduled_queue
    selected(queue)
    path.write_text(path.read_text().replace('reasoning_effort = "xhigh"', 'reasoning_effort = "low"'))
    with pytest.raises(ReviewConfigurationChanged):
        claim_trigger(queue, RUN_ID)


def test_disabled_workspace_refresh_preserves_legacy_reservation_policy(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, path = scheduled_queue
    original = claim_trigger(queue, RUN_ID)
    legacy = asdict(queue.config)
    for registration in legacy["supervisor"]["workspaces"]:
        registration.pop("refresh_authorized")
    expected = supervisor_context_digest({"path": str(queue.config_path), "configuration": legacy})
    assert original["receipt"]["configuration_digest"] == expected
    assert claim_trigger(queue, RUN_ID)["receipt"] == original["receipt"]
    path.write_text(path.read_text().replace(", instruction_sources = [", ", refresh_authorized = true, instruction_sources = ["))
    updated = ReviewQueue(load_config(path), queue.directory, config_path=path)
    assert updated.config.supervisor.workspaces[0].refresh_authorized is True
    with pytest.raises(ReviewQueueError, match="different configuration"):
        claim_trigger(updated, RUN_ID)


def test_context_change_before_consumption_is_refused(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    digest = selected(queue)
    queue.handle(decision(queue, "feedback", message="Different direction."))
    with pytest.raises(ReviewQueueError, match="context changed"):
        consume_slot(queue, RUN_ID, REPOSITORY, digest)
    assert receipt(queue)["slots"][0]["phase"] == "reserved"
    assert claim_trigger(queue, RUN_ID)["claimed"] is False


def test_unknown_scope_and_digest_cannot_consume_slot(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    digest = selected(queue)
    for repository, revision in [("other/repository", digest), (REPOSITORY, "b" * 64)]:
        with pytest.raises(ReviewQueueError, match="absent, changed"):
            consume_slot(queue, RUN_ID, repository, revision)
    assert receipt(queue)["slots"][0]["phase"] == "reserved"


def test_completed_receipt_requires_real_saved_observation(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    consume_slot(queue, RUN_ID, REPOSITORY, selected(queue))
    with pytest.raises(ReviewQueueError, match="verified saved provider outcome"):
        finish_slot(queue, RUN_ID, REPOSITORY, job_id="job_" + "b" * 32)
    _, job = complete(queue)
    finish_slot(queue, RUN_ID, REPOSITORY, job_id=job["job_id"])
    assert receipt(queue)["slots"][0] == {"repository": REPOSITORY,
        "context_digest": job["context_digest"], "phase": "finished", "job_id": job["job_id"]}
    with pytest.raises(ReviewQueueError, match="already consumed"):
        consume_slot(queue, RUN_ID, REPOSITORY, job["context_digest"])


def test_failure_never_restores_launch_slot(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    digest = selected(queue)
    consume_slot(queue, RUN_ID, REPOSITORY, digest)
    finish_slot(queue, RUN_ID, REPOSITORY)
    assert receipt(queue)["slots"][0]["phase"] == "recovery_required"
    with pytest.raises(ReviewQueueError, match="already consumed"):
        consume_slot(queue, RUN_ID, REPOSITORY, digest)


def test_selection_race_does_not_reserve_old_context(
    scheduled_queue: tuple[ReviewQueue, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, _ = scheduled_queue
    original = scheduled_triggers.scheduled_supervisor_plan

    def changed(*args: Any) -> dict[str, Any]:
        plan = original(*args)
        queue.handle(decision(queue, "feedback", message="Changed during selection."))
        return plan

    monkeypatch.setattr(scheduled_triggers, "scheduled_supervisor_plan", changed)
    with pytest.raises(ReviewQueueError, match="changed before reservation"):
        claim_trigger(queue, RUN_ID)
    assert not (queue.directory / "scheduled-supervisor").exists()


@pytest.mark.parametrize("fault", ["boolean_version", "extra_field", "duplicate_slot", "invalid_phase", "changed_digest"])
def test_malformed_receipts_are_not_overwritten_or_consumed(
    scheduled_queue: tuple[ReviewQueue, Path], fault: str,
) -> None:
    queue, _ = scheduled_queue
    digest = selected(queue)
    value = receipt(queue)
    if fault == "boolean_version":
        value["schema_version"] = True
    elif fault == "extra_field":
        value["command"] = "never executed"
    elif fault == "duplicate_slot":
        value["slots"].append(value["slots"][0].copy())
    elif fault == "invalid_phase":
        value["slots"][0]["phase"] = []
    else:
        value["selection_digest"] = "b" * 64
    save_private_json(queue.directory, Path("scheduled-supervisor") / f"{RUN_ID}.json", value)
    with pytest.raises(ReviewQueueError):
        consume_slot(queue, RUN_ID, REPOSITORY, digest)
    assert receipt(queue) == value


@pytest.mark.parametrize("fault", ["omitted", "duplicate", "unknown", "reason", "extra", "type"])
def test_saved_receipt_requires_complete_valid_repository_partition(
    scheduled_queue: tuple[ReviewQueue, Path], fault: str,
) -> None:
    old_queue, path = scheduled_queue
    path.write_text(path.read_text() + '\n[[repositories]]\nname = "example-org/second-app"\n')
    queue = ReviewQueue(load_config(path), old_queue.directory, config_path=path)
    digest = selected(queue)
    value = receipt(queue)
    skipped = value["selection"]["skipped"]
    assert skipped == [{"repository": "example-org/second-app", "reason": "configuration_blocked"}]
    if fault == "omitted": skipped.clear()
    elif fault == "duplicate": skipped[0]["repository"] = REPOSITORY.upper()
    elif fault == "unknown": skipped[0]["repository"] = "unconfigured/repository"
    elif fault == "reason": skipped[0]["reason"] = "silently_ignored"
    elif fault == "extra": skipped[0]["command"] = "ignored"
    else: skipped[0]["reason"] = []
    value["selection_digest"] = supervisor_context_digest(value["selection"])
    save_private_json(queue.directory, Path("scheduled-supervisor") / f"{RUN_ID}.json", value)
    with pytest.raises(ReviewQueueError, match="partition|omits"):
        consume_slot(queue, RUN_ID, REPOSITORY, digest)
    assert receipt(queue) == value


def test_claim_bridge_returns_exact_private_receipt(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    request = {"schema_version": 1, "request_id": "read_claim", "operation": "scheduled_supervisor_claim", "run_id": RUN_ID}
    for claimed in (True, False):
        output = io.BytesIO()
        assert exchange(queue, io.BytesIO(json.dumps(request).encode()), output) == 0
        response = json.loads(output.getvalue())
        assert response["trigger"] == {"claimed": claimed, "receipt": receipt(queue)}


@pytest.mark.parametrize("operation", [[], {}, True, None])
def test_malformed_operation_returns_a_structured_bridge_error(
    scheduled_queue: tuple[ReviewQueue, Path], operation: Any,
) -> None:
    queue, _ = scheduled_queue
    output = io.BytesIO()
    request = {"schema_version": 1, "request_id": "read_malformed", "operation": operation, "run_id": RUN_ID}
    assert exchange(queue, io.BytesIO(json.dumps(request).encode()), output) == 2
    assert json.loads(output.getvalue())["status"] == "error"
    assert not (queue.directory / "scheduled-supervisor").exists()


def test_scheduled_stream_records_verified_completion_and_cannot_replay(launch_case: dict[str, Any]) -> None:
    case = launch_case
    queue = case["queue"]
    persist_plan(queue.directory, case["report"])
    save_attempt(queue)
    digest = selected(queue)
    request = {"schema_version": 1, "request_id": "launch_scheduled", "operation": "supervisor_start",
        "repository": REPOSITORY, "context_digest": digest, "scheduled_run_id": RUN_ID}
    output = io.BytesIO()
    assert launch_exchange(queue, cast(GitHubClient, object()), request, output) == 0
    terminal = json.loads(output.getvalue().splitlines()[-1])
    assert receipt(queue)["slots"][0]["job_id"] == terminal["job_id"]
    assert receipt(queue)["slots"][0]["phase"] == "finished"
    assert launch_exchange(queue, cast(GitHubClient, object()), request, io.BytesIO()) == 2
    assert len(case["launches"]) == 1


def test_scheduled_provider_failure_remains_recovery_not_automatic_retry(launch_case: dict[str, Any]) -> None:
    case = launch_case
    queue = case["queue"]
    persist_plan(queue.directory, case["report"])
    save_attempt(queue)
    digest = selected(queue)
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    request = {"schema_version": 1, "request_id": "launch_failed", "operation": "supervisor_start",
        "repository": REPOSITORY, "context_digest": digest, "scheduled_run_id": RUN_ID}
    assert launch_exchange(queue, cast(GitHubClient, object()), request, io.BytesIO()) == 2
    assert receipt(queue)["slots"][0]["phase"] == "recovery_required"
    assert claim_trigger(queue, RUN_ID)["claimed"] is False
    assert len(case["launches"]) == 1
