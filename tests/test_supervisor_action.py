from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import execution, supervisor_action
from dependabot_watcher.execution import ExecutionError, execute_approved, preflight_execution
from dependabot_watcher.review_queue import ReviewQueue
from dependabot_watcher.runtime_state import load_private_json, save_private_json
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_runner import SupervisorRunner
from dependabot_watcher.supervisor_workspace import verify_supervisor_workspace
from test_execution import ExecutionClient, execution_config, refresh_plan_identities, saved_plan
from test_review_queue import decision, persist_plan
from test_supervisor_runner import ReceiptProvider
from test_supervisor_workspace import workspace_case


@pytest.fixture
def supervised_action(workspace_case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    workspace = workspace_case
    policy = replace(workspace["config"].supervisor, mode="approval_first", provider="codex_app_server",
        model="fixture-model", reasoning_effort="low", expected_account_type="chatgpt",
        access="inherit", model_use_acknowledged=True)
    config = replace(execution_config(), supervisor=policy)
    report = saved_plan()
    report["repositories"][0]["evidence_snapshot"]["base_sha"] = workspace["head"]
    report["repositories"][0]["recommended_order"][0]["freshness"]["base_sha"] = workspace["head"]
    report = refresh_plan_identities(report)
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, report)
    queue.record_workspace_evidence(asdict(verify_supervisor_workspace(config, "example-org/example-app", report["repositories"][0])))
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    provider = ReceiptProvider(queue)
    original = provider.analyze
    def recommend(current: dict[str, Any]) -> dict[str, Any]:
        observed = original(current)
        observed["analysis"]["recommendations"][0]["conclusion"] = "recommend"
        observed["analysis"]["recommendations"][0]["ready_for_action"] = True
        return observed
    monkeypatch.setattr(provider, "analyze", recommend)
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda *args, **kwargs: copy.deepcopy(report))
    return {**workspace, "queue": queue, "config": config, "report": report, "job": job,
            "jobs": jobs, "provider": provider, "client": ExecutionClient(),
            "step_id": report["repositories"][0]["recommended_order"][0]["step_id"]}


def complete(case: dict[str, Any]) -> None:
    SupervisorRunner(case["jobs"], case["provider"]).run(case["job"]["job_id"])


def preflight(case: dict[str, Any]) -> dict[str, Any]:
    return preflight_execution(case["config"], case["client"], case["report"], case["step_id"],
                               state_directory=case["queue"].directory)


def test_current_verified_recommendation_is_read_only_readiness_not_consent(supervised_action: dict[str, Any]) -> None:
    case = supervised_action
    complete(case)
    before = {path: path.read_bytes() for path in case["queue"].directory.rglob("*") if path.is_file()}
    result = preflight(case)
    assert result["status"] == "ready" and result["mutation_authorized"] is False
    assert result["decision"]["model_used"] is True
    intelligence = result["decision"]["supervisor"]
    assert intelligence["job_id"] == case["job"]["job_id"] and intelligence["conclusion"] == "recommend"
    assert intelligence["model"] == "fixture-model" and intelligence["mutation_authorized"] is False
    assert {path: path.read_bytes() for path in case["queue"].directory.rglob("*") if path.is_file()} == before
    assert len(case["provider"].calls) == 1 and case["client"].merges == []


@pytest.mark.parametrize("fault", ["missing", "hold", "investigate", "no_action", "no_recommendation",
                                  "missing_observation", "foreign_model", "feedback", "instructions", "dirty_workspace"])
def test_supervised_preflight_refuses_unverified_or_conservative_context(
    supervised_action: dict[str, Any], fault: str,
) -> None:
    case = supervised_action
    if fault != "missing":
        complete(case)
    if fault in {"hold", "investigate", "no_action", "no_recommendation", "foreign_model"}:
        observation_path = Path("supervisor-observations") / f'{case["job"]["job_id"]}.json'
        observation = load_private_json(case["queue"].directory, observation_path)
        if fault == "foreign_model": observation["model"] = "another-model"
        else:
            analysis = observation["analysis"]
            if fault == "no_recommendation": analysis["recommendations"] = []
            else: analysis["recommendations"][0]["conclusion"] = fault
            if analysis["recommendations"]:
                analysis["recommendations"][0]["ready_for_action"] = False
            state = case["jobs"]._load()
            state["jobs"][case["job"]["job_id"]]["result"] = copy.deepcopy(analysis)
            save_private_json(case["queue"].directory, Path("supervisor-jobs.json"), state)
        save_private_json(case["queue"].directory, observation_path, observation)
    elif fault == "missing_observation":
        (case["queue"].directory / "supervisor-observations" / f'{case["job"]["job_id"]}.json').unlink()
    elif fault == "feedback":
        case["queue"].handle(decision(case["queue"], "feedback", message="Change this proposed order first."))
    elif fault == "instructions":
        (case["worktree"] / "AGENTS.md").write_text("Changed trusted policy after investigation.\n")
    elif fault == "dirty_workspace":
        (case["worktree"] / "app.py").write_text("Preserve unfinished local work.\n")
    with pytest.raises(ExecutionError):
        preflight(case)
    assert case["client"].merges == [] and not (case["queue"].directory / "execution-audit.jsonl").exists()


def test_final_executor_rechecks_receipt_after_preparation(supervised_action: dict[str, Any]) -> None:
    case = supervised_action
    complete(case)
    prepared = preflight(case)
    (case["queue"].directory / "supervisor-observations" / f'{case["job"]["job_id"]}.json').unlink()
    with pytest.raises(ExecutionError):
        execute_approved(case["config"], case["client"], case["report"], case["step_id"],
                         prepared["approval_token"], state_directory=case["queue"].directory)
    assert case["client"].merges == [] and not (case["queue"].directory / "execution-audit.jsonl").exists()


def test_exact_approved_supervised_action_uses_existing_single_executor(supervised_action: dict[str, Any]) -> None:
    case = supervised_action
    complete(case)
    prepared = preflight(case)
    result = execute_approved(case["config"], case["client"], case["report"], case["step_id"],
                              prepared["approval_token"], state_directory=case["queue"].directory)
    assert result["status"] == "merged" and len(case["client"].merges) == 1
    assert result["decision"]["supervisor"]["job_id"] == case["job"]["job_id"]
    assert len(case["provider"].calls) == 1
    events = [json.loads(line) for line in (case["queue"].directory / "execution-audit.jsonl").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["execution_started", "execution_succeeded"]
    assert all(event["supervisor"] == result["decision"]["supervisor"] for event in events)


def test_already_applied_observation_needs_no_new_agent(supervised_action: dict[str, Any]) -> None:
    case = supervised_action
    case["client"].state = "MERGED"
    result = preflight(case)
    assert result["status"] == "already_applied" and result["approval_token"] is None
    assert case["provider"].calls == [] and case["client"].merges == []


def test_feedback_during_readiness_check_cannot_use_the_old_answer(
    supervised_action: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = supervised_action
    complete(case)
    original = supervisor_action.verify_supervisor_workspace
    def changed(*args: Any) -> Any:
        proof = original(*args)
        case["queue"].handle(decision(case["queue"], "feedback", message="New instruction while preparing."))
        return proof
    monkeypatch.setattr(supervisor_action, "verify_supervisor_workspace", changed)
    with pytest.raises(ExecutionError, match="changed while checking"):
        preflight(case)
    assert case["client"].merges == []


@pytest.mark.parametrize("fault", ["missing", "job", "context", "session", "account", "access", "validation"])
def test_supervised_action_requires_exact_effective_runtime_receipt(supervised_action: dict[str, Any], fault: str) -> None:
    case = supervised_action
    complete(case)
    path = Path("supervisor-runtime") / f'{case["job"]["job_id"]}.json'
    if fault == "missing":
        (case["queue"].directory / path).unlink()
    else:
        receipt = load_private_json(case["queue"].directory, path)
        if fault in {"job", "context"}:
            receipt[f"{fault}_id" if fault == "job" else "context_digest"] = "foreign"
        else:
            key = {"session": "session_id", "account": "account_type", "validation": "validation_authorized"}.get(fault, fault)
            receipt["effective_settings"][key] = True if fault == "validation" else "foreign"
        save_private_json(case["queue"].directory, path, receipt)
    with pytest.raises(ExecutionError):
        preflight(case)
    assert case["client"].merges == []


def test_missing_supervised_state_is_not_created(supervised_action: dict[str, Any], tmp_path: Path) -> None:
    case = supervised_action
    missing = tmp_path / "absent-state"
    with pytest.raises(ExecutionError):
        preflight_execution(case["config"], case["client"], case["report"], case["step_id"], state_directory=missing)
    assert not missing.exists() and case["client"].merges == []


@pytest.mark.parametrize("readiness", [None, False])
def test_recommended_candidate_without_explicit_action_readiness_is_not_executable(
    supervised_action: dict[str, Any], readiness: bool | None,
) -> None:
    case = supervised_action
    complete(case)
    path = Path("supervisor-observations") / f'{case["job"]["job_id"]}.json'
    observation = load_private_json(case["queue"].directory, path)
    recommendation = observation["analysis"]["recommendations"][0]
    if readiness is None:
        recommendation.pop("ready_for_action")
    else:
        recommendation["ready_for_action"] = readiness
    recommendation["explanation"] = "Recommend this candidate for fresh validation; compatibility remains unproven."
    jobs = case["jobs"]._load()
    jobs["jobs"][case["job"]["job_id"]]["result"] = copy.deepcopy(observation["analysis"])
    save_private_json(case["queue"].directory, Path("supervisor-jobs.json"), jobs)
    save_private_json(case["queue"].directory, path, observation)
    with pytest.raises(ExecutionError, match="explicitly marked"):
        preflight(case)
    assert case["client"].merges == []


@pytest.mark.parametrize("checkpoint", ["gate", "audit"])
def test_policy_revocation_after_gate_or_audit_prevents_merge(
    supervised_action: dict[str, Any], monkeypatch: pytest.MonkeyPatch, checkpoint: str,
) -> None:
    case = supervised_action
    complete(case)
    prepared = preflight(case)
    revoked = False
    def configuration_check(*args: Any) -> None:
        if revoked:
            raise ExecutionError("Configuration changed during execution.")
    monkeypatch.setattr(execution, "_require_unchanged_configuration", configuration_check)
    if checkpoint == "gate":
        original = execution._require_supervised_action
        def gate(*args: Any) -> Any:
            nonlocal revoked
            result = original(*args)
            revoked = True
            return result
        monkeypatch.setattr(execution, "_require_supervised_action", gate)
    else:
        original_audit = execution._append_audit
        def audit(*args: Any, **kwargs: Any) -> Any:
            nonlocal revoked
            result = original_audit(*args, **kwargs)
            if args[1]["event_type"] == "execution_started":
                revoked = True
            return result
        monkeypatch.setattr(execution, "_append_audit", audit)
    with pytest.raises(ExecutionError, match="Configuration changed"):
        execute_approved(case["config"], case["client"], case["report"], case["step_id"],
                         prepared["approval_token"], state_directory=case["queue"].directory)
    assert case["client"].merges == []
    if checkpoint == "audit":
        events = [json.loads(line) for line in (case["queue"].directory / "execution-audit.jsonl").read_text().splitlines()]
        assert events[-1]["event_type"] == "execution_failed" and events[-1]["outcome"] == "configuration_revoked"
        assert all(event["supervisor"]["job_id"] == case["job"]["job_id"] for event in events)
