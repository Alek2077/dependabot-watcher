from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import scheduled_supervisor
from dependabot_watcher.config import load_config
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewConfigurationChanged, ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import load_run_state, save_run_state
from dependabot_watcher.scheduled_supervisor import scheduled_supervisor_plan
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_runner import SupervisorRunner
from test_execution import saved_plan
from test_review_queue import decision, file_bound_queue, persist_plan
from test_supervision import refresh_multi_repository_plan, result_for
from test_supervisor_runner import FixtureProvider


RUN_ID = "run_" + "a" * 20
REPOSITORY = "example-org/example-app"


def save_attempt(queue: ReviewQueue, run_id: str = RUN_ID) -> None:
    state = load_run_state(queue.directory)
    state["last_attempt"] = {"run_id": run_id}
    save_run_state(queue.directory, state)


@pytest.fixture
def scheduled_queue(file_bound_queue: tuple[ReviewQueue, Path]) -> tuple[ReviewQueue, Path]:
    queue, path = file_bound_queue
    save_attempt(queue)
    return queue, path


def test_selection_binds_current_context_without_creating_a_job(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "ready"
    assert plan["repositories"] == [{"repository": REPOSITORY,
        "context_digest": scheduled_supervisor.supervisor_context_digest(queue.supervision_context(REPOSITORY))}]
    assert plan["capabilities"] == {"model_calls": False, "github_mutations": False}
    assert not (queue.directory / "supervisor-jobs.json").exists()


@pytest.mark.parametrize("run_id", ["", "run_123", "run_" + "b" * 20])
def test_wrong_or_stale_run_is_refused(scheduled_queue: tuple[ReviewQueue, Path], run_id: str) -> None:
    queue, _ = scheduled_queue
    with pytest.raises(ReviewQueueError):
        scheduled_supervisor_plan(queue, run_id)
    assert not (queue.directory / "supervisor-jobs.json").exists()


def test_changed_configuration_is_not_used(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, path = scheduled_queue
    path.write_text(path.read_text().replace('reasoning_effort = "xhigh"', 'reasoning_effort = "low"'))
    with pytest.raises(ReviewConfigurationChanged):
        scheduled_supervisor_plan(queue, RUN_ID)


def test_unbound_configuration_cannot_trigger_scheduled_reasoning(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    with pytest.raises(ReviewQueueError, match="trusted profile"):
        scheduled_supervisor_plan(ReviewQueue(queue.config, queue.directory), RUN_ID)


def test_disabled_policy_stays_off(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    old, path = scheduled_queue
    path.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    queue = ReviewQueue(load_config(path), old.directory, config_path=path)
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "off" and plan["repositories"] == []


def complete(queue: ReviewQueue, *, fail: bool = False) -> tuple[SupervisorJobs, dict[str, Any]]:
    jobs = SupervisorJobs(queue)
    job = jobs.prepare(REPOSITORY)
    provider = FixtureProvider(queue, fail=fail)
    if fail:
        with pytest.raises(ReviewQueueError, match="Fixture"):
            SupervisorRunner(jobs, provider).run(job["job_id"])
    else:
        SupervisorRunner(jobs, provider).run(job["job_id"])
    return jobs, job


def test_verified_completion_is_quiet_but_new_feedback_selects_work(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    queue.handle(decision(queue, "feedback", message="Explain this recommendation."))
    complete(queue)
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "idle"
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "completed_current"}]
    next_run = "run_" + "c" * 20
    save_attempt(queue, next_run)
    assert scheduled_supervisor_plan(queue, next_run)["status"] == "idle"
    save_attempt(queue)
    queue.handle(decision(queue, "feedback", message="Please clarify the validation."))
    assert scheduled_supervisor_plan(queue, RUN_ID)["status"] == "ready"


def test_failed_attempt_requires_explicit_recovery(scheduled_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = scheduled_queue
    complete(queue, fail=True)
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "attention" and plan["repositories"] == []
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "recovery_required"}]


def test_live_owner_blocks_even_after_feedback_changes_context(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    jobs = SupervisorJobs(queue)
    job = jobs.prepare(REPOSITORY)
    jobs.start(job["job_id"])
    try:
        queue.handle(decision(queue, "feedback", message="New direction while running."))
        plan = scheduled_supervisor_plan(queue, RUN_ID)
        assert plan["repositories"] == []
        assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "already_running"}]
    finally:
        jobs.release_owner(job["job_id"])


def test_lost_owner_becomes_recovery_not_automatic_retry(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    jobs = SupervisorJobs(queue)
    job = jobs.prepare(REPOSITORY)
    jobs.start(job["job_id"])
    jobs.release_owner(job["job_id"])
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "recovery_required"}]
    assert jobs.read(job["job_id"])["status"] == "interrupted"


@pytest.mark.parametrize("operation,fields", [("reject", {}), ("defer", {"seconds": 86400})])
def test_handled_item_stays_quiet_until_user_leaves_feedback(
    scheduled_queue: tuple[ReviewQueue, Path], operation: str, fields: dict[str, Any],
) -> None:
    queue, _ = scheduled_queue
    queue.handle(decision(queue, operation, **fields))
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "no_work"}]
    queue.handle(decision(queue, "feedback", message="Explain what happens next."))
    assert scheduled_supervisor_plan(queue, RUN_ID)["status"] == "ready"


def test_unobserved_completion_is_not_scheduled_success(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    jobs = SupervisorJobs(queue)
    job = jobs.prepare(REPOSITORY)
    jobs.start(job["job_id"])
    try:
        jobs.complete(job["job_id"], result_for(job))
    finally:
        jobs.release_owner(job["job_id"])
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "attention"
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "completion_unverified"}]


@pytest.mark.parametrize("change", ["run", "feedback"])
def test_selection_race_cannot_return_an_old_launch_scope(
    scheduled_queue: tuple[ReviewQueue, Path], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    queue, _ = scheduled_queue
    original = scheduled_supervisor.supervision_status

    def changing_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        if change == "run":
            save_attempt(queue, "run_" + "b" * 20)
        else:
            queue.handle(decision(queue, "feedback", message="A changed request."))
        return result

    monkeypatch.setattr(scheduled_supervisor, "supervision_status", changing_status)
    with pytest.raises(ReviewQueueError, match="changed|latest saved run"):
        scheduled_supervisor_plan(queue, RUN_ID)


def test_completed_turn_with_missing_reply_needs_attention_not_repeat(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    queue.handle(decision(queue, "feedback", message="Please answer this question."))

    class MissingReplyProvider(FixtureProvider):
        def analyze(self, job: Any) -> dict[str, Any]:
            observation = super().analyze(job)
            observation["analysis"]["replies"] = []
            return observation

    jobs = SupervisorJobs(queue)
    job = jobs.prepare(REPOSITORY)
    SupervisorRunner(jobs, MissingReplyProvider(queue)).run(job["job_id"])
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "attention" and plan["repositories"] == []
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": "unanswered_feedback"}]


def test_partial_evidence_can_receive_reasoning_without_mutation_authority(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    report = saved_plan()
    report["status"] = "partial"
    report["repositories"][0]["status"] = "partial"
    report["repositories"][0]["unresolved_evidence"] = {
        "manifest_status": "partial", "errors": [{"message": "Collection unavailable"}]}
    persist_plan(queue.directory, refresh_multi_repository_plan(report))
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "ready"
    assert plan["capabilities"]["github_mutations"] is False


@pytest.mark.parametrize("phase,reason", [("new", "evidence_incomplete"),
    ("completed", "evidence_incomplete"), ("failed", "recovery_required"),
    ("interrupted", "recovery_required"), ("unverified", "completion_unverified")])
def test_partial_collection_without_proposals_is_not_silent_no_work(
    scheduled_queue: tuple[ReviewQueue, Path], phase: str, reason: str,
) -> None:
    queue, _ = scheduled_queue
    report = saved_plan()
    report["status"] = "partial"
    repository = report["repositories"][0]
    repository["status"] = "partial"
    repository["recommended_order"] = []
    repository["unresolved_evidence"] = {
        "manifest_status": "partial", "errors": [{"message": "Collection unavailable"}]}
    persist_plan(queue.directory, refresh_multi_repository_plan(report))
    if phase in {"completed", "failed"}:
        complete(queue, fail=phase == "failed")
    elif phase in {"interrupted", "unverified"}:
        jobs = SupervisorJobs(queue)
        job = jobs.prepare(REPOSITORY)
        jobs.start(job["job_id"])
        try:
            if phase == "unverified":
                jobs.complete(job["job_id"], result_for(job))
        finally:
            jobs.release_owner(job["job_id"])
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["status"] == "attention" and plan["repositories"] == []
    assert plan["skipped"] == [{"repository": REPOSITORY, "reason": reason}]


def test_job_limit_defers_second_repository_until_first_is_answered(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    old, path = scheduled_queue
    second_name = "example-org/other-app"
    path.write_text(path.read_text().replace('workspaces = [', 'max_jobs_per_run = 1\nworkspaces = [').replace(
        ']}]\n', ']}, {repository = "example-org/other-app", directory = '
        + json.dumps(str(path.parent / "other")) + ', instruction_sources = []}]\n')
        + '\n[[repositories]]\nname = "example-org/other-app"\n')
    queue = ReviewQueue(load_config(path), old.directory, config_path=path)
    report = saved_plan()
    second = copy.deepcopy(report["repositories"][0])
    second["repository"] = second_name
    for step in second["recommended_order"]:
        step["identity"]["repository"] = second_name
    report["repositories"].append(second)
    persist_plan(queue.directory, refresh_multi_repository_plan(report))
    first = scheduled_supervisor_plan(queue, RUN_ID)
    assert [item["repository"] for item in first["repositories"]] == [REPOSITORY]
    assert first["skipped"] == [{"repository": second_name, "reason": "budget_deferred"}]
    complete(queue)
    next_plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert [item["repository"] for item in next_plan["repositories"]] == [second_name]


def test_missing_workspace_registration_is_visible_not_silently_ignored(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    old, path = scheduled_queue
    path.write_text(path.read_text() + '\n[[repositories]]\nname = "example-org/other-app"\n')
    queue = ReviewQueue(load_config(path), old.directory, config_path=path)
    plan = scheduled_supervisor_plan(queue, RUN_ID)
    assert plan["skipped"] == [{"repository": "example-org/other-app", "reason": "configuration_blocked"}]
    assert [item["repository"] for item in plan["repositories"]] == [REPOSITORY]


def test_bridge_selection_is_scoped_and_never_launches_a_model(
    scheduled_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = scheduled_queue
    output = io.BytesIO()
    request = {"schema_version": 1, "request_id": "read_schedule", "operation": "scheduled_supervisor_plan",
               "run_id": RUN_ID}
    assert exchange(queue, io.BytesIO(json.dumps(request).encode()), output) == 0
    response = json.loads(output.getvalue())
    assert response["status"] == "ok" and response["request_id"] == request["request_id"]
    assert response["plan"] == scheduled_supervisor_plan(queue, RUN_ID)
    assert not (queue.directory / "supervisor-jobs.json").exists()


@pytest.mark.parametrize("field,value", [("command", "gh pr merge"), ("schema_version", True),
    ("request_id", "decision_1_wrong"), ("run_id", "run_" + "b" * 20)])
def test_bridge_rejects_untrusted_or_stale_selection_request(
    scheduled_queue: tuple[ReviewQueue, Path], field: str, value: Any,
) -> None:
    queue, _ = scheduled_queue
    request = {"schema_version": 1, "request_id": "read_schedule", "operation": "scheduled_supervisor_plan",
               "run_id": RUN_ID, field: value}
    output = io.BytesIO()
    assert exchange(queue, io.BytesIO(json.dumps(request).encode()), output) == 2
    assert json.loads(output.getvalue())["status"] == "error"
    assert not (queue.directory / "supervisor-jobs.json").exists()
