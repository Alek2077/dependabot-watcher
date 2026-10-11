from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from dependabot_watcher import supervisor_runner
from dependabot_watcher.config import load_config, parse_config
from dependabot_watcher.review_queue import ReviewConfigurationChanged, ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_events import SupervisorEvents
from dependabot_watcher.supervisor_runner import SupervisorRunner, validate_observation
from dependabot_watcher.supervisor_status import supervision_status
from dependabot_watcher.supervisor_workspace import SupervisorAuthorityChanged, _instruction_fingerprint
from test_execution import saved_plan
from test_review_queue import decision, file_bound_queue, persist_plan, workspace_receipt
from test_supervision import result_for


class FixtureProvider:
    def __init__(self, queue: ReviewQueue, *, change: bool = False, fail: bool = False) -> None:
        self.queue = queue
        self.change = change
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def analyze(self, job: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(dict(job)))
        if self.fail:
            raise ReviewQueueError("Fixture provider failed")
        if self.change:
            self.queue.handle(decision(self.queue, "feedback", message="New direction while reasoning"))
        return {"session_id": "session-123", "analysis": result_for(dict(job)),
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}, "command_observations": [],
                "turn_completed": True, "provider": "fixture", "model": "fixture-model", "reasoning_effort": "high",
                "profile": None, "workspace": "/fixture/workspace"}


class ReceiptProvider(FixtureProvider):
    """Controlled model boundary with real backend runtime/connection evidence."""

    def analyze_with_events(self, job: Mapping[str, Any], events: SupervisorEvents) -> dict[str, Any]:
        proof = job["context"]["workspace_evidence"]
        policy = job["context"]["supervisor"]
        events.connected("session-123", {"session_id": "session-123", "workspace": proof["directory"],
            "instruction_sources": [pair[0] for pair in proof["instruction_fingerprints"]],
            "model": policy["model"], "reasoning_effort": policy["reasoning_effort"],
            "access": policy["access"], "account_type": policy["expected_account_type"],
            "validation_authorized": policy["validation_authorized"]})
        return {**self.analyze(job), "provider": policy["provider"], "model": policy["model"],
                "reasoning_effort": policy["reasoning_effort"], "workspace": proof["directory"]}


@pytest.fixture
def runner_case(tmp_path: Path) -> tuple[SupervisorJobs, FixtureProvider, dict[str, Any]]:
    config = parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]})
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, saved_plan())
    queue.handle(decision(queue, "feedback", message="Why this recommendation?"))
    jobs = SupervisorJobs(queue)
    return jobs, FixtureProvider(queue), jobs.prepare("example-org/example-app")


def test_runner_delivers_exact_saved_question_and_persists_reply_and_provenance(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]],
) -> None:
    jobs, provider, job = runner_case
    result = SupervisorRunner(jobs, provider).run(job["job_id"])
    delivered = provider.calls[0]
    assert delivered["context_digest"] == job["context_digest"]
    assert delivered["context"]["proposals"][0]["feedback"][0]["message"] == "Why this recommendation?"
    stored = load_private_json(jobs.queue.directory, Path("supervisor-observations") / f'{job["job_id"]}.json')
    assert stored["model"] == "fixture-model" and stored["turn_completed"] is True
    assert stored["analysis"]["replies"][0]["request_id"] == delivered["context"]["proposals"][0]["feedback"][0]["request_id"]
    assert result["job"]["status"] == "completed"
    assert SupervisorJobs(jobs.queue).read(job["job_id"])["result"] == stored["analysis"]
    with pytest.raises(ReviewQueueError, match="already started"):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    assert len(provider.calls) == 1


@pytest.mark.parametrize("status", ["unknown", "", None, True])
def test_declined_support_does_not_accept_unknown_command_outcomes(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], status: object,
) -> None:
    _, provider, job = runner_case
    observation = provider.analyze(job)
    observation["command_observations"] = [{"status": status, "exit_code": None}]
    with pytest.raises(ReviewQueueError, match="command observation"):
        validate_observation(observation, job)


def test_reply_to_old_context_is_saved_but_not_promoted_after_new_feedback(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]],
) -> None:
    jobs, provider, job = runner_case
    provider.change = True
    result = SupervisorRunner(jobs, provider).run(job["job_id"])
    assert result["job"]["status"] == "superseded"
    assert result["job"]["result"] is not None
    fresh = jobs.prepare(job["repository"])
    assert len(fresh["context"]["proposals"][0]["feedback"]) == 2
    assert fresh["status"] == "prepared"


def test_provider_failure_is_recorded_without_fake_completion(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]],
) -> None:
    jobs, provider, job = runner_case
    provider.fail = True
    with pytest.raises(ReviewQueueError, match="Fixture"):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    stored = jobs.read(job["job_id"])
    assert stored["status"] == "failed" and stored["result"] is None and stored["session_id"] is None
    diagnostic = load_private_json(jobs.queue.directory, Path("supervisor-failures") / f'{job["job_id"]}.json')
    assert diagnostic["job_id"] == job["job_id"] and diagnostic["context_digest"] == job["context_digest"]
    assert diagnostic["locations"]
    assert all(set(item) == {"file", "function", "line"} and not Path(item["file"]).is_absolute()
               for item in diagnostic["locations"])
    assert "Fixture provider failed" not in json.dumps(diagnostic)
    assert str(jobs.queue.directory) not in json.dumps(diagnostic)


def test_failure_location_storage_failure_does_not_mask_the_original_failure(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, provider, job = runner_case
    provider.fail = True
    save = supervisor_runner.save_private_json

    def unavailable(directory, path, *args, **kwargs):
        if path.parts[0] == "supervisor-failures":
            raise RuntimeStateError("Diagnostic storage unavailable")
        return save(directory, path, *args, **kwargs)

    monkeypatch.setattr(supervisor_runner, "save_private_json", unavailable)
    with pytest.raises(ReviewQueueError, match="Fixture provider failed"):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"


def test_file_policy_change_before_start_makes_no_provider_call(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, path = file_bound_queue
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    provider = FixtureProvider(queue)
    path.write_text(path.read_text().replace('reasoning_effort = "xhigh"', 'reasoning_effort = "low"'))
    with pytest.raises(ReviewConfigurationChanged):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    assert provider.calls == [] and jobs.read(job["job_id"])["status"] == "prepared"


@pytest.mark.parametrize("after_call", [False, True])
def test_instruction_revocation_makes_no_initial_call_or_preserves_late_observation_without_analysis(
    file_bound_queue: tuple[ReviewQueue, Path], monkeypatch: pytest.MonkeyPatch, after_call: bool,
) -> None:
    queue, _ = file_bound_queue
    source = Path(queue.config.supervisor.workspaces[0].instruction_sources[0])
    source.write_text("First trusted policy.\n")
    receipt = workspace_receipt(queue)
    receipt["instruction_fingerprints"][0][1] = _instruction_fingerprint(source)
    queue.record_workspace_evidence(receipt)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    provider = ReceiptProvider(queue)
    original = provider.analyze
    def revoke(value: Mapping[str, Any]) -> dict[str, Any]:
        observed = original(value)
        source.write_text("Changed trusted policy.\n")
        return observed
    if after_call:
        monkeypatch.setattr(provider, "analyze", revoke)
    else:
        source.write_text("Changed trusted policy.\n")
    with pytest.raises(SupervisorAuthorityChanged):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    saved = jobs.read(job["job_id"])
    assert saved["result"] is None
    assert saved["status"] == ("interrupted" if after_call else "prepared")
    observation = queue.directory / "supervisor-observations" / f'{job["job_id"]}.json'
    assert observation.exists() is after_call and len(provider.calls) == int(after_call)
    if after_call:
        assert load_private_json(queue.directory, Path("supervisor-observations") / observation.name)["turn_completed"] is True
        status = supervision_status(queue, {"schema_version": 1, "request_id": "read_instructions",
            "operation": "supervisor_status", "repository": job["repository"]})
        assert status["jobs"][0]["messages"] == []
        assert status["jobs"][0]["provider_completion_observed"] is True


@pytest.mark.parametrize("fault", ["legacy", "no_runtime", "model", "effort", "workspace", "provider", "profile"])
def test_receipt_job_cannot_acknowledge_legacy_unconnected_or_misidentified_delivery(
    file_bound_queue: tuple[ReviewQueue, Path], monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    queue, _ = file_bound_queue
    source = Path(queue.config.supervisor.workspaces[0].instruction_sources[0])
    source.write_text("Selected trusted policy.\n")
    receipt = workspace_receipt(queue)
    receipt["instruction_fingerprints"][0][1] = _instruction_fingerprint(source)
    queue.record_workspace_evidence(receipt)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    provider = FixtureProvider(queue) if fault == "legacy" else ReceiptProvider(queue)
    if fault == "no_runtime":
        monkeypatch.setattr(provider, "analyze_with_events", lambda value, events: provider.analyze(value))
    elif fault != "legacy":
        original = getattr(provider, "analyze_with_events")
        def redirect(value: Mapping[str, Any], events: SupervisorEvents) -> dict[str, Any]:
            observed = original(value, events)
            key = "reasoning_effort" if fault == "effort" else fault
            observed[key] = "/another/workspace" if fault == "workspace" else "another-value"
            return observed
        monkeypatch.setattr(provider, "analyze_with_events", redirect)
    with pytest.raises(ReviewQueueError):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    saved = jobs.read(job["job_id"])
    assert saved["status"] == ("prepared" if fault == "legacy" else "failed") and saved["result"] is None
    assert len(provider.calls) == int(fault != "legacy")
    assert not (queue.directory / "supervisor-observations" / f'{job["job_id"]}.json').exists()


def test_completed_provider_observation_is_retained_but_not_promoted_after_file_policy_change(
    file_bound_queue: tuple[ReviewQueue, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, path = file_bound_queue
    queue.handle(decision(queue, "feedback", message="An exact question before revocation."))
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    provider = FixtureProvider(queue)
    original = provider.analyze

    def revoke(value: Mapping[str, Any]) -> dict[str, Any]:
        observation = original(value)
        path.write_text(path.read_text().replace('access = "inherit"', 'access = "full"\nfull_access_acknowledged = true'))
        return observation

    monkeypatch.setattr(provider, "analyze", revoke)
    with pytest.raises(ReviewConfigurationChanged):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    stored = jobs.read(job["job_id"])
    assert stored["status"] == "interrupted" and stored["result"] is None
    observed = load_private_json(queue.directory, Path("supervisor-observations") / f'{job["job_id"]}.json')
    assert observed["turn_completed"] is True
    refreshed = ReviewQueue(load_config(path), queue.directory, config_path=path)
    status = supervision_status(refreshed, {"schema_version": 1, "request_id": "read_revoked",
        "operation": "supervisor_status", "repository": job["repository"]})
    assert status["jobs"][0]["current_context"] is False
    record = status["jobs"][0]
    assert record["result"] is None
    assert record["observed_result"] == observed["analysis"]
    assert record["messages"][0]["question"] == "An exact question before revocation."
    assert record["messages"][0]["processing"] == "answered_historical"
    assert status["jobs"][0]["provider_completion_observed"] is True
    # Reading historical display data does not accept or rewrite the attempt.
    assert jobs.read(job["job_id"])["result"] is None


@pytest.mark.parametrize("fault", ["no_completion", "redirected_reply", "extra_authority", "session_space", "not_mapping"])
def test_invalid_observation_cannot_be_acknowledged(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, provider, job = runner_case
    original = provider.analyze

    def invalid(value: Mapping[str, Any]) -> Any:
        response = original(value)
        if fault == "no_completion":
            response["turn_completed"] = False
        elif fault == "redirected_reply":
            response["analysis"]["replies"][0]["request_id"] = "decision_999_wrong"
        elif fault == "extra_authority":
            response["approval_token"] = "not authority"
        elif fault == "session_space":
            response["session_id"] = "invalid session"
        else:
            return None
        return response

    monkeypatch.setattr(provider, "analyze", invalid)
    with pytest.raises(ReviewQueueError):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"


def test_continuation_requires_completed_owner_and_preserves_same_session(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, provider, first = runner_case
    monkeypatch.setattr(provider, "resume_session_id", "unknown-session", raising=False)
    with pytest.raises(ReviewQueueError, match="completed owner"):
        SupervisorRunner(jobs, provider).run(first["job_id"])
    assert jobs.read(first["job_id"])["status"] == "prepared" and provider.calls == []
    monkeypatch.setattr(provider, "resume_session_id", None)
    SupervisorRunner(jobs, provider).run(first["job_id"])
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Follow up on the same repository"))
    followup = jobs.prepare(first["repository"])
    monkeypatch.setattr(provider, "resume_session_id", "session-123")
    completed = SupervisorRunner(jobs, provider).run(followup["job_id"])
    assert provider.calls[-1]["session_id"] == "session-123"
    assert completed["job"]["session_id"] == "session-123"
    assert completed["job"]["status"] == "completed"


def test_unexpected_session_reuse_is_failed_not_stranded(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]],
) -> None:
    jobs, provider, first = runner_case
    SupervisorRunner(jobs, provider).run(first["job_id"])
    jobs.queue.handle(decision(jobs.queue, "feedback", message="New question"))
    followup = jobs.prepare(first["repository"])
    with pytest.raises(ReviewQueueError, match="already owned"):
        SupervisorRunner(jobs, provider).run(followup["job_id"])
    assert jobs.read(followup["job_id"])["status"] == "failed"


def test_lost_provider_observation_write_leaves_accountable_attempt_not_false_delivery(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, provider, job = runner_case

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeStateError("Observation write failed")

    monkeypatch.setattr(supervisor_runner, "save_private_json", fail)
    with pytest.raises(RuntimeStateError, match="write failed"):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    stored = jobs.read(job["job_id"])
    assert stored["status"] == "interrupted" and stored["session_id"] == "session-123" and stored["result"] is None
    with pytest.raises(ReviewQueueError, match="already started"):
        SupervisorRunner(jobs, provider).run(job["job_id"])
    assert len(provider.calls) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL actual startup crash")
@pytest.mark.parametrize("provider_route", ["eventful_startup", "legacy"])
def test_owner_death_before_session_or_permission_connection_is_recoverable(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], provider_route: str,
) -> None:
    jobs, _provider, job = runner_case
    program = '''
import sys
from pathlib import Path
from dependabot_watcher.config import parse_config
from dependabot_watcher.review_queue import ReviewQueue
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_runner import SupervisorRunner
config=parse_config({"schema_version":1,"repositories":[{"name":"example-org/example-app"}]})
jobs=SupervisorJobs(ReviewQueue(config,Path(sys.argv[1])))
class Legacy:
    def analyze(self,job):
        print("provider_started",flush=True)
        sys.stdin.read()
        raise RuntimeError("Unexpected release")
class Eventful(Legacy):
    def analyze_with_events(self,job,events):
        return self.analyze(job) # No connected() or session yet.
SupervisorRunner(jobs,Eventful() if sys.argv[3]=="eventful_startup" else Legacy()).run(sys.argv[2])
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", program, str(jobs.queue.directory), job["job_id"], provider_route],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if jobs.read(job["job_id"])["status"] == "running":
                break
            assert child.poll() is None, "Owner exited before provider startup"
            time.sleep(.02)
        assert jobs.read(job["job_id"])["status"] == "running"
        assert not (jobs.queue.directory / "supervisor-permissions.json").exists()
        child.kill()
        child.wait(timeout=5)
        status = supervision_status(jobs.queue, {"schema_version":1,"request_id":"read_owner_crash",
            "operation":"supervisor_status","repository":job["repository"]})
        assert status["jobs"][0]["status"] == "interrupted"
        assert status["jobs"][0]["provider_completion_observed"] is False
        assert jobs.read(job["job_id"])["session_id"] is None
        assert status["jobs"][0]["retry_available"] is True
        with pytest.raises(ReviewQueueError, match="recovery"):
            jobs.prepare(job["repository"])
        assert jobs.prepare(job["repository"], retry_job_id=job["job_id"])["job_id"] != job["job_id"]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
