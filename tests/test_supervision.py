from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import supervision
from dependabot_watcher.config import PathPolicyOverrides, SupervisorConfig, SupervisorWorkspaceConfig, parse_config
from dependabot_watcher.planning import _overall_plan_identifier, _repository_plan_identifier, _step_identifier
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json, save_private_json
from dependabot_watcher.supervision import SupervisorJobs
from test_execution import refresh_plan_identities, saved_plan
from test_review_queue import decision, file_bound_queue, persist_plan, snapshot, workspace_receipt


@pytest.fixture
def jobs_case(tmp_path: Path) -> tuple[SupervisorJobs, dict[str, Any]]:
    report = saved_plan()
    config = parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]})
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, report)
    return SupervisorJobs(queue), report


def result_for(job: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "Further investigation is needed before any action.",
            "recommendations": [{"proposal_id": item["proposal_id"], "conclusion": "investigate",
                                 "explanation": "Inspect the exact dependency change and validate it."}
                                for item in job["context"]["proposals"]],
            "replies": [{"proposal_id": item["proposal_id"], "request_id": message["request_id"],
                         "message": "This question belongs to this saved revision."}
                        for item in job["context"]["proposals"] for message in item["feedback"]]}


def test_instruction_fingerprint_change_invalidates_prepared_job_and_preserves_saved_feedback(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = file_bound_queue
    queue.handle(decision(queue, "feedback", message="Check compatibility before recommending this."))
    evidence = workspace_receipt(queue)
    queue.record_workspace_evidence(evidence)
    jobs = SupervisorJobs(queue)
    first = jobs.prepare("example-org/example-app")
    assert jobs.prepare("EXAMPLE-ORG/Example-App")["job_id"] == first["job_id"]
    evidence["instruction_fingerprints"][0][1] = "d" * 64
    queue.record_workspace_evidence(evidence)
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(first["job_id"])
    second = jobs.prepare("example-org/example-app")
    assert second["context_digest"] != first["context_digest"]
    assert second["context"]["proposals"] == first["context"]["proposals"]
    assert jobs.read(first["job_id"])["context"]["workspace_evidence"] != second["context"]["workspace_evidence"]


def test_completed_analysis_is_historical_after_a_new_workspace_receipt(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = file_bound_queue
    evidence = workspace_receipt(queue)
    queue.record_workspace_evidence(evidence)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    try:
        evidence["instruction_fingerprints"][0][1] = "d" * 64
        queue.record_workspace_evidence(evidence)
        completed = jobs.complete(job["job_id"], result_for(job))
        assert completed["status"] == "superseded" and completed["failure"] == "context_changed"
        assert completed["result"] == result_for(job)
        assert completed["context"]["workspace_evidence"]["instruction_fingerprints"][0][1] == "c" * 64
    finally:
        jobs.release_owner(job["job_id"])


@pytest.mark.parametrize("change", ["directory", "source", "mode", "scope", "missing_policy"])
def test_job_workspace_receipt_cannot_be_rebound_even_with_a_recomputed_digest(
    file_bound_queue: tuple[ReviewQueue, Path], change: str,
) -> None:
    queue, _ = file_bound_queue
    queue.record_workspace_evidence(workspace_receipt(queue))
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    state = jobs._load()
    context = state["jobs"][job["job_id"]]["context"]
    if change == "missing_policy":
        context.pop("supervisor")
    elif change == "mode":
        context["supervisor"]["mode"] = "off"
    elif change == "scope":
        context["supervisor"]["workspaces"][0]["repository"] = "other/repository"
    elif change == "source":
        context["supervisor"]["workspaces"][0]["instruction_sources"] = ["/other/AGENTS.md"]
    else:
        context["supervisor"]["workspaces"][0]["directory"] = "/other/worktree"
    state["jobs"][job["job_id"]]["context_digest"] = supervision._digest(context)
    save_private_json(queue.directory, Path("supervisor-jobs.json"), state)
    with pytest.raises(ReviewQueueError, match="mixes workspace"):
        jobs.read(job["job_id"])
    assert load_private_json(queue.directory, Path("supervisor-jobs.json")) == state


def test_workspace_receipt_remains_scoped_and_legacy_jobs_are_readable(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    original, _ = file_bound_queue
    original.record_workspace_evidence(workspace_receipt(original))
    queue = ReviewQueue(replace(original.config, repositories=(original.config.repositories[0],
        replace(original.config.repositories[0], name="example-org/other-app"))), original.directory)
    assert queue.supervision_context("example-org/other-app")["workspace_evidence"] is None
    assert queue.supervision_context("example-org/other-app")["supervisor"]["workspaces"] == []
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    state = jobs._load()
    context = state["jobs"][job["job_id"]]["context"]
    context.pop("workspace_evidence")
    state["jobs"][job["job_id"]]["context_digest"] = supervision._digest(context)
    jobs._save(state)
    assert jobs.read(job["job_id"])["context"] == context
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(job["job_id"])


def test_repository_case_alias_resolves_workspace_plan_feedback_and_one_job(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, report = jobs_case
    config = jobs.queue.config
    jobs.queue.config = replace(config, repositories=(replace(config.repositories[0], name="EXAMPLE-ORG/Example-App"),),
        supervisor=SupervisorConfig(workspaces=(
            SupervisorWorkspaceConfig("EXAMPLE-ORG/Example-App", "/owned/current", ("/trusted/AGENTS.md",)),
            SupervisorWorkspaceConfig("example-org/other-app", "/private/other", ("/private/AGENTS.md",)))))
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Explain this exact update."))
    job = jobs.prepare("Example-Org/EXAMPLE-APP")
    assert job["repository"] == report["repositories"][0]["repository"]
    assert job["context"]["repository_plan"] == report["repositories"][0]
    assert job["context"]["supervisor"]["workspaces"] == [{
        "repository": "EXAMPLE-ORG/Example-App", "directory": "/owned/current",
        "instruction_sources": ("/trusted/AGENTS.md",)}]
    assert "/private/other" not in json.dumps(job)
    assert job["context"]["proposals"][0]["feedback"][0]["message"] == "Explain this exact update."
    assert jobs.prepare("example-org/example-app")["job_id"] == job["job_id"]
    jobs.start(job["job_id"])
    jobs.complete(job["job_id"], result_for(job))
    assert jobs.read(job["job_id"])["status"] == "completed"


def test_case_changed_plan_keeps_retired_feedback_and_serializes_session_continuation(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, report = jobs_case
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Question about the previous saved evidence."))
    first = jobs.prepare("example-org/example-app")
    jobs.start(first["job_id"])
    jobs.attach_session(first["job_id"], "owned-case-session")
    repository = report["repositories"][0]
    repository["repository"] = "EXAMPLE-ORG/Example-App"
    for step in repository["recommended_order"]:
        step["identity"]["repository"] = repository["repository"]
    persist_plan(jobs.queue.directory, refresh_multi_repository_plan(report))
    second = jobs.prepare("example-org/example-app")
    assert second["repository"] == repository["repository"]
    assert second["context"]["retired_proposals"][0]["feedback"][0]["message"] == "Question about the previous saved evidence."
    assert second["context"]["proposals"][0]["feedback"] == []
    with pytest.raises(ReviewQueueError, match="already running"):
        jobs.start(second["job_id"])
    assert jobs.complete(first["job_id"], result_for(first))["status"] == "superseded"
    jobs.start(second["job_id"], resume_session_id="owned-case-session")
    jobs.complete(second["job_id"], result_for(second))
    assert SupervisorJobs(jobs.queue).read(second["job_id"])["status"] == "completed"


def test_supervisor_policy_changes_invalidate_job_and_other_workspaces_are_not_transmitted(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, _ = jobs_case
    policy = SupervisorConfig(mode="approval_first", provider="codex_app_server", model="fixture-model",
        reasoning_effort="low", expected_account_type="chatgpt", model_use_acknowledged=True,
        workspaces=(SupervisorWorkspaceConfig("example-org/example-app", "/owned/current", ("/trusted/AGENTS.md",)),
                    SupervisorWorkspaceConfig("example-org/other-app", "/private/other", ("/private/instructions",))))
    jobs.queue.config = replace(jobs.queue.config, supervisor=policy)
    job = jobs.prepare("example-org/example-app")
    assert len(job["context"]["supervisor"]["workspaces"]) == 1
    assert "/private/other" not in json.dumps(job["context"])
    jobs.queue.config = replace(jobs.queue.config, supervisor=replace(policy, access="full", full_access_acknowledged=True))
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(job["job_id"])


def test_legacy_context_is_readable_but_missing_supervisor_policy_cannot_be_current(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, _ = jobs_case
    job = jobs.prepare("example-org/example-app")
    state = jobs._load()
    old = state["jobs"][job["job_id"]]
    old["context"].pop("supervisor")
    old["context_digest"] = supervision._digest(old["context"])
    jobs._save(state)
    assert jobs.read(job["job_id"])["status"] == "prepared"
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(job["job_id"])


def refresh_multi_repository_plan(report: dict[str, Any]) -> dict[str, Any]:
    for repository in report["repositories"]:
        for step in repository["recommended_order"]:
            core = {key: value for key, value in step.items() if key not in {"step_id", "order"}}
            step["step_id"] = _step_identifier(repository["repository"], repository, core)
        repository["plan_id"] = _repository_plan_identifier(
            {key: value for key, value in repository.items() if key != "plan_id"})
    report["plan_id"] = _overall_plan_identifier(report["status"], [item["plan_id"] for item in report["repositories"]])
    return report


def test_session_cannot_cross_repository_on_attach_or_resume(tmp_path: Path) -> None:
    report = saved_plan()
    second = copy.deepcopy(report["repositories"][0])
    second["repository"] = "example-org/other-app"
    for step in second["recommended_order"]:
        step["identity"]["repository"] = second["repository"]
    report["repositories"].append(second)
    config = parse_config({"schema_version": 1, "repositories": [
        {"name": "example-org/example-app"}, {"name": "example-org/other-app"}]})
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, refresh_multi_repository_plan(report))
    jobs = SupervisorJobs(queue)
    owner = jobs.prepare("example-org/example-app")
    jobs.start(owner["job_id"])
    jobs.attach_session(owner["job_id"], "owned-session")
    jobs.complete(owner["job_id"], result_for(owner))
    other = jobs.prepare("example-org/other-app")
    with pytest.raises(ReviewQueueError, match="completed owner"):
        jobs.start(other["job_id"], resume_session_id="owned-session")
    assert jobs.read(other["job_id"])["status"] == "prepared"
    jobs.start(other["job_id"])
    with pytest.raises(ReviewQueueError, match="already owned"):
        jobs.attach_session(other["job_id"], "owned-session")


def test_prepared_context_preserves_exact_evidence_and_feedback_without_delivery_claim(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, report = jobs_case
    message = "Please explain the order — café 日本語 🙂"
    request = decision(jobs.queue, "feedback", message=message)
    jobs.queue.handle(request)
    job = jobs.prepare("example-org/example-app")
    context = job["context"]
    assert context["repository_plan"] == report["repositories"][0]
    item = context["proposals"][0]
    assert item["step"]["identity"]["head_sha"] == "a" * 40
    assert item["feedback"][0]["request_id"] == request["request_id"]
    assert item["feedback"][0]["message"] == message
    assert item["feedback"][0]["status"] == "saved_not_submitted"
    assert context["mutation_authorized"] is False
    assert job["status"] == "prepared" and job["session_id"] is None
    job["context"]["proposals"][0]["feedback"][0]["message"] = "modified returned copy"
    assert jobs.read(job["job_id"])["context"]["proposals"][0]["feedback"][0]["message"] == message
    assert not (jobs.queue.directory / "execution-audit.jsonl").exists()


def test_scope_excludes_other_repository_and_unknown_scope_is_rejected(tmp_path: Path) -> None:
    report = saved_plan()
    second = copy.deepcopy(report["repositories"][0])
    second["repository"] = "example-org/other-app"
    for step in second["recommended_order"]:
        step["identity"]["repository"] = second["repository"]
    report["repositories"].append(second)
    report = refresh_multi_repository_plan(report)
    config = parse_config({"schema_version": 1, "repositories": [
        {"name": "example-org/example-app"}, {"name": "example-org/other-app"}]})
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, report)
    view = snapshot(queue)
    item = next(item for item in view["proposals"] if item["repository"] == second["repository"])
    queue.handle({"schema_version": 1, "request_id": "decision_1_private", "operation": "feedback",
                  "proposal_id": item["proposal_id"], "version": item["version"], "message": "private second repo"})
    jobs = SupervisorJobs(queue)
    first = jobs.prepare("example-org/example-app")
    assert "private second repo" not in json.dumps(first)
    assert all(item["repository"] == "example-org/example-app" for item in first["context"]["proposals"])
    second["recommended_order"][0]["identity"]["head_sha"] = "e" * 40
    second["recommended_order"][0]["freshness"]["head_sha"] = "e" * 40
    report["repositories"][1] = second
    persist_plan(queue.directory, refresh_multi_repository_plan(report))
    assert jobs.prepare("example-org/example-app")["job_id"] == first["job_id"]
    with pytest.raises(ReviewQueueError, match="outside"):
        jobs.prepare("unconfigured/repository")


def test_retired_message_does_not_move_to_new_head(jobs_case: tuple[SupervisorJobs, dict[str, Any]]) -> None:
    jobs, report = jobs_case
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Question about the old revision"))
    step = report["repositories"][0]["recommended_order"][0]
    step["identity"]["head_sha"] = step["freshness"]["head_sha"] = "e" * 40
    persist_plan(jobs.queue.directory, refresh_plan_identities(report))
    context = jobs.prepare("example-org/example-app")["context"]
    assert context["proposals"][0]["feedback"] == []
    assert context["retired_proposals"][0]["feedback"][0]["message"] == "Question about the old revision"
    assert context["retired_proposals"][0]["step"]["identity"]["head_sha"] == "a" * 40


def test_start_is_single_use_and_restart_preserves_session(jobs_case: tuple[SupervisorJobs, dict[str, Any]]) -> None:
    jobs, _ = jobs_case
    job = jobs.prepare("example-org/example-app")
    assert jobs.prepare(job["repository"])["job_id"] == job["job_id"]
    jobs.start(job["job_id"])
    jobs.attach_session(job["job_id"], "session-123")
    restarted = SupervisorJobs(jobs.queue)
    assert restarted.read(job["job_id"])["session_id"] == "session-123"
    with pytest.raises(ReviewQueueError, match="already started"):
        restarted.start(job["job_id"])
    with pytest.raises(ReviewQueueError, match="does not match"):
        restarted.attach_session(job["job_id"], "other-session")
    completed = restarted.complete(job["job_id"], result_for(job))
    assert completed["status"] == "completed"
    assert restarted.complete(job["job_id"], result_for(job)) == completed


@pytest.mark.parametrize("change", ["feedback", "reject", "head", "config", "path_policy"])
def test_changed_context_rejects_start_and_marks_late_analysis_historical(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]], change: str,
) -> None:
    jobs, report = jobs_case
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    if change in {"feedback", "reject"}:
        extra = {"message": "Reconsider this"} if change == "feedback" else {}
        jobs.queue.handle(decision(jobs.queue, change, **extra))
    elif change == "head":
        step = report["repositories"][0]["recommended_order"][0]
        step["identity"]["head_sha"] = step["freshness"]["head_sha"] = "e" * 40
        persist_plan(jobs.queue.directory, refresh_plan_identities(report))
    elif change == "path_policy":
        jobs.queue.config = replace(jobs.queue.config, path_policy=PathPolicyOverrides(sensitive_patterns=("src/**",)))
    else:
        jobs.queue.config = replace(jobs.queue.config, collection=replace(jobs.queue.config.collection, command_timeout_seconds=30))
    stale = jobs.complete(job["job_id"], result_for(job))
    assert stale["status"] == "superseded" and stale["result"] == result_for(job)
    fresh = jobs.prepare("example-org/example-app")
    assert fresh["job_id"] != job["job_id"]
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Yet another change"))
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(fresh["job_id"])


def test_changed_queue_cannot_launch_second_supervisor_until_first_is_accounted_for(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, _ = jobs_case
    first = jobs.prepare("example-org/example-app")
    jobs.start(first["job_id"])
    jobs.queue.handle(decision(jobs.queue, "feedback", message="New direction"))
    second = jobs.prepare("example-org/example-app")
    with pytest.raises(ReviewQueueError, match="already running"):
        jobs.start(second["job_id"])
    jobs.fail(first["job_id"], interrupted=True)
    assert jobs.start(second["job_id"])["status"] == "running"


def test_global_path_policy_change_invalidates_prepared_job(jobs_case: tuple[SupervisorJobs, dict[str, Any]]) -> None:
    jobs, _ = jobs_case
    job = jobs.prepare("example-org/example-app")
    jobs.queue.config = replace(jobs.queue.config, path_policy=PathPolicyOverrides(dependency_patterns=("custom.lock",)))
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(job["job_id"])
    fresh = jobs.prepare(job["repository"])
    assert fresh["context"]["path_policy"]["dependency_patterns"] == ("custom.lock",)
    assert fresh["context_digest"] != job["context_digest"]


@pytest.mark.parametrize("fault", ["wrong_proposal", "wrong_message", "command", "duplicate", "oversize"])
def test_agent_results_cannot_redirect_identity_or_add_authority(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]], fault: str,
) -> None:
    jobs, _ = jobs_case
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Why?"))
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    result = result_for(job)
    if fault == "wrong_proposal":
        result["recommendations"][0]["proposal_id"] = "proposal_" + "f" * 16
    elif fault == "wrong_message":
        result["replies"][0]["request_id"] = "decision_999_another"
    elif fault == "command":
        result["command"] = "git push"
    elif fault == "duplicate":
        result["recommendations"] *= 2
    else:
        result["summary"] = "🙂" * 2049
    with pytest.raises(ReviewQueueError, match="Supervisor"):
        jobs.complete(job["job_id"], result)
    assert jobs.read(job["job_id"])["status"] == "running"


def test_failed_or_interrupted_job_is_not_mistaken_for_completed_analysis(
    jobs_case: tuple[SupervisorJobs, dict[str, Any]],
) -> None:
    jobs, _ = jobs_case
    job = jobs.prepare("example-org/example-app")
    with pytest.raises(ReviewQueueError, match="running"):
        jobs.complete(job["job_id"], result_for(job))
    jobs.start(job["job_id"])
    jobs.fail(job["job_id"], interrupted=True)
    assert jobs.read(job["job_id"])["result"] is None
    with pytest.raises(ReviewQueueError, match="recovery"):
        jobs.prepare(job["repository"])
    assert jobs.prepare(job["repository"], retry_job_id=job["job_id"])["job_id"] != job["job_id"]


def test_retry_cannot_resume_even_a_previous_completed_owner(jobs_case: tuple[SupervisorJobs, dict[str, Any]]) -> None:
    jobs, _ = jobs_case
    first = jobs.prepare("example-org/example-app")
    jobs.start(first["job_id"])
    jobs.attach_session(first["job_id"], "completed-session")
    jobs.complete(first["job_id"], result_for(first))
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Continue"))
    second = jobs.prepare(first["repository"])
    jobs.start(second["job_id"], resume_session_id="completed-session")
    jobs.fail(second["job_id"])
    retry = jobs.prepare(first["repository"], retry_job_id=second["job_id"])
    with pytest.raises(ReviewQueueError, match="fresh session"):
        jobs.start(retry["job_id"], resume_session_id="completed-session")
    assert jobs.read(retry["job_id"])["status"] == "prepared"
    assert jobs.start(retry["job_id"])["session_id"] is None
    jobs.fail(retry["job_id"])


def test_save_failure_is_not_acknowledged_as_started(jobs_case: tuple[SupervisorJobs, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, _ = jobs_case
    job = jobs.prepare("example-org/example-app")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeStateError("Disk write failed")

    monkeypatch.setattr(supervision, "save_private_json", fail)
    with pytest.raises(RuntimeStateError, match="Disk"):
        jobs.start(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "prepared"


@pytest.mark.parametrize("fault", ["digest", "status", "schema", "identity"])
def test_malformed_journal_is_rejected(jobs_case: tuple[SupervisorJobs, dict[str, Any]], fault: str) -> None:
    jobs, _ = jobs_case
    job = jobs.prepare("example-org/example-app")
    state = load_private_json(jobs.queue.directory, Path("supervisor-jobs.json"))
    if fault == "digest":
        state["jobs"][job["job_id"]]["context"]["mutation_authorized"] = True
    elif fault == "status":
        state["jobs"][job["job_id"]]["status"] = "completed"
    elif fault == "schema":
        state["schema_version"] = True
    else:
        state["jobs"][job["job_id"]]["job_id"] = "job_" + "f" * 32
    save_private_json(jobs.queue.directory, Path("supervisor-jobs.json"), state)
    with pytest.raises(ReviewQueueError, match="Supervisor|supervisor"):
        jobs.read(job["job_id"])


def test_symlink_job_file_cannot_be_followed(jobs_case: tuple[SupervisorJobs, dict[str, Any]], tmp_path: Path) -> None:
    jobs, _ = jobs_case
    snapshot(jobs.queue)
    outside = tmp_path / "outside.json"
    outside.write_text('{}', encoding="utf-8")
    (jobs.queue.directory / "supervisor-jobs.json").symlink_to(outside)
    with pytest.raises(RuntimeStateError):
        jobs.prepare("example-org/example-app")
    assert outside.read_text(encoding="utf-8") == '{}'
