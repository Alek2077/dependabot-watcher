from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from dependabot_watcher import supervision, supervisor_permissions
from dependabot_watcher.config import parse_config
from dependabot_watcher.review_queue import ReviewConfigurationChanged, ReviewQueue, ReviewQueueError
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json, run_lock
from dependabot_watcher.supervisor_events import SupervisorEvents
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_permissions import SupervisorPermissions, permission_digest
from test_execution import saved_plan
from test_review_queue import decision, file_bound_queue, persist_plan, workspace_receipt
from test_supervision import refresh_multi_repository_plan


@pytest.fixture
def permissions_case(tmp_path: Path) -> Iterator[tuple[SupervisorJobs, SupervisorPermissions, str]]:
    queue = ReviewQueue(parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]}), tmp_path / "state")
    persist_plan(queue.directory, saved_plan())
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    jobs.attach_session(job["job_id"], "thread1")
    journal = SupervisorPermissions(jobs)
    try:
        with journal.live_connection(job["job_id"], "thread1") as connection:
            journal.bind_turn(connection, "turn1")
            yield jobs, journal, connection
    finally:
        jobs.release_owner(job["job_id"])


def ask(journal: SupervisorPermissions, connection: str, **changes: Any) -> dict[str, Any]:
    params = {"provider_request_id": "request1", "turn_id": "turn1", "item_id": "item1",
              "digest": permission_digest({"id": "request1", "scope": "synthetic workspace read"}),
              "description": "Read a synthetic denied file for this local test.",
              "choices": ["allow_once", "deny", "cancel"]}
    return journal.request(connection, **{**params, **changes})


@pytest.mark.parametrize("terminal", ["resolved", "closed", "orphaned"])
def test_backward_wall_clock_preserves_permission_resolution_order(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], terminal: str,
) -> None:
    jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    now = datetime.fromisoformat(requested["created_at"].replace("Z", "+00:00")) - timedelta(seconds=1)
    jobs.queue.clock = lambda: now
    if terminal == "resolved":
        journal.decide(connection, requested["permission_id"], requested["digest"], "deny")
        assert journal.consume(connection, requested["permission_id"], requested["digest"]) == "deny"
        journal.resolve(connection, requested["permission_id"])
    elif terminal == "orphaned":
        jobs.release_owner(journal._load()["connections"][connection]["job_id"])
    else:
        journal.close(connection)
    saved = journal.snapshot("example-org/example-app")["connections"][0]["requests"][requested["permission_id"]]
    assert datetime.fromisoformat(saved["resolved_at"]) > datetime.fromisoformat(requested["created_at"])
    assert saved["status"] == ("resolved" if terminal == "resolved" else "invalidated")


@pytest.mark.parametrize("historical_connections", [0, 24])
def test_permission_snapshot_reuses_validated_jobs_for_historical_bindings(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str],
    monkeypatch: pytest.MonkeyPatch, historical_connections: int,
) -> None:
    jobs, journal, connection = permissions_case
    state = journal._load()
    for index in range(historical_connections):
        identifier = f"connection_history_{index}"
        state["connections"][identifier] = {
            **state["connections"][connection], "connection_id": identifier,
            "status": "closed", "requests": {},
        }
    journal._save(state)
    reads = 0
    original = SupervisorJobs._load

    def counted(self: SupervisorJobs) -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return original(self)

    with monkeypatch.context() as patch:
        patch.setattr(SupervisorJobs, "_load", counted)
        result = journal.snapshot("example-org/example-app")
    assert len(result["connections"]) == historical_connections + 1
    assert reads == 2, "History projection reloaded the complete job journal for each connection"
    assert next(item for item in result["connections"] if item["connection_id"] == connection)["live_connection_confirmed"]


def test_permission_snapshot_still_rejects_a_closed_connection_with_wrong_job_binding(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str],
) -> None:
    _, journal, connection = permissions_case
    state = journal._load()
    identifier = "connection_history_wrong"
    state["connections"][identifier] = {
        **state["connections"][connection], "connection_id": identifier,
        "status": "closed", "requests": {}, "context_digest": "0" * 64,
    }
    journal._save(state)
    with pytest.raises(ReviewQueueError, match="bound to its job"):
        journal.snapshot("example-org/example-app")


def test_new_instruction_receipt_blocks_an_already_saved_permission_decision(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = file_bound_queue
    evidence = workspace_receipt(queue)
    queue.record_workspace_evidence(evidence)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    jobs.attach_session(job["job_id"], "thread1")
    journal = SupervisorPermissions(jobs)
    try:
        with journal.live_connection(job["job_id"], "thread1") as connection:
            journal.bind_turn(connection, "turn1")
            requested = ask(journal, connection)
            journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
            evidence["instruction_fingerprints"][0][1] = "d" * 64
            queue.record_workspace_evidence(evidence)
            with pytest.raises(ReviewQueueError, match="context changed"):
                journal.consume(connection, requested["permission_id"], requested["digest"])
            assert journal.snapshot("example-org/example-app")["connections"][0]["current_context"] is False
        saved = journal._load()["connections"][connection]["requests"][requested["permission_id"]]
        assert saved["status"] == "invalidated"
    finally:
        jobs.release_owner(job["job_id"])


def test_case_alias_permission_request_routes_to_the_same_owned_connection(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str],
) -> None:
    _jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    response = journal.handle({"schema_version": 1, "request_id": "case_alias",
        "operation": "supervisor_permission_decide", "repository": "EXAMPLE-ORG/Example-App",
        "connection_id": connection, "permission_id": requested["permission_id"],
        "digest": requested["digest"], "decision": "deny"})
    record = response["connections"][0]
    assert record["connection_id"] == connection and record["current_context"] is True
    assert journal.consume(connection, requested["permission_id"], requested["digest"]) == "deny"
    journal.resolve(connection, requested["permission_id"])


@pytest.mark.parametrize("decided_before_change", [False, True])
def test_file_policy_change_blocks_permission_save_and_delivery(
    file_bound_queue: tuple[ReviewQueue, Path], decided_before_change: bool,
) -> None:
    queue, path = file_bound_queue
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    jobs.attach_session(job["job_id"], "thread1")
    journal = SupervisorPermissions(jobs)
    try:
        with journal.live_connection(job["job_id"], "thread1") as connection:
            journal.bind_turn(connection, "turn1")
            requested = ask(journal, connection)
            if decided_before_change:
                journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
            path.write_text(path.read_text().replace('access = "inherit"', 'access = "full"\nfull_access_acknowledged = true'))
            with pytest.raises(ReviewConfigurationChanged):
                if decided_before_change:
                    journal.consume(connection, requested["permission_id"], requested["digest"])
                else:
                    journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
            saved = journal._load()["connections"][connection]["requests"][requested["permission_id"]]
            assert saved["status"] == ("decided" if decided_before_change else "waiting")
        assert journal._load()["connections"][connection]["requests"][requested["permission_id"]]["status"] == "invalidated"
    finally:
        jobs.release_owner(job["job_id"])


def test_permission_snapshot_only_recovers_connections_in_the_requested_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    job = jobs.prepare(second_plan["repository"])
    jobs.start(job["job_id"])
    jobs.attach_session(job["job_id"], "other-thread")
    journal = SupervisorPermissions(jobs)
    with journal.live_connection(job["job_id"], "other-thread") as connection:
        journal.bind_turn(connection, "turn1")
        requested = ask(journal, connection)
        jobs.release_owner(job["job_id"])

        def unrelated_owner(_job_id: str) -> bool:
            raise RuntimeStateError("An unrelated repository owner is inaccessible")

        with monkeypatch.context() as patch:
            patch.setattr(jobs, "_owner_is_live", unrelated_owner)
            assert journal.snapshot("EXAMPLE-ORG/Example-App")["connections"] == []
            assert journal._load()["connections"][connection]["requests"][requested["permission_id"]]["status"] == "waiting"
            assert jobs._load()["jobs"][job["job_id"]]["status"] == "running"
        recovered = journal.snapshot(second_plan["repository"])["connections"][0]
        assert recovered["status"] == "closed"
        assert recovered["requests"][requested["permission_id"]]["status"] == "invalidated"
        assert jobs.read(job["job_id"])["status"] == "interrupted"


@pytest.mark.parametrize("choice", ["allow_once", "deny", "cancel"])
def test_exact_decision_persists_then_consumes_once_and_resolves(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], choice: str) -> None:
    jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    assert journal.consume(connection, requested["permission_id"], requested["digest"]) is None
    stored = SupervisorPermissions(jobs)
    assert stored.snapshot("example-org/example-app")["connections"][0]["live_connection_confirmed"] is True
    assert stored.snapshot("example-org/example-app")["connections"][0]["requests"][requested["permission_id"]]["status"] == "waiting"
    stored.decide(connection, requested["permission_id"], requested["digest"], choice)
    assert journal.consume(connection, requested["permission_id"], requested["digest"]) == choice
    with pytest.raises(ReviewQueueError, match="consumed"):
        journal.consume(connection, requested["permission_id"], requested["digest"])
    journal.resolve(connection, requested["permission_id"])
    assert journal.snapshot("example-org/example-app")["connections"][0]["requests"][requested["permission_id"]]["status"] == "resolved"


@pytest.mark.parametrize("decided", [False, True])
def test_event_poll_defers_contention_without_consuming_or_ignoring_later_revocation(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], decided: bool,
) -> None:
    jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    job = next(iter(jobs._load()["jobs"].values()))
    events = SupervisorEvents(jobs, job)
    events.connection_id = connection
    if decided:
        journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
    with run_lock(jobs.queue.directory):
        assert events.permission_decision(requested["permission_id"], requested["digest"]) is None
    saved = journal._load()["connections"][connection]["requests"][requested["permission_id"]]
    assert saved["status"] == ("decided" if decided else "waiting")
    if decided:
        assert events.permission_decision(requested["permission_id"], requested["digest"]) == "allow_once"
        with pytest.raises(ReviewQueueError, match="consumed"):
            events.permission_decision(requested["permission_id"], requested["digest"])
    else:
        jobs.queue.handle(decision(jobs.queue, "feedback", message="Changed investigation context"))
        with pytest.raises(ReviewQueueError, match="context changed"):
            events.permission_decision(requested["permission_id"], requested["digest"])


def test_event_poll_does_not_hide_storage_failure(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, _journal, connection = permissions_case
    events = SupervisorEvents(jobs, next(iter(jobs._load()["jobs"].values())))
    events.connection_id = connection
    def broken(*args: Any) -> str | None:
        raise RuntimeStateError("Controlled storage failure")
    monkeypatch.setattr(events.permissions, "consume", broken)
    with pytest.raises(RuntimeStateError, match="storage failure"):
        events.permission_decision("permission_fixture", "a" * 64)


@pytest.mark.parametrize("state", ["waiting", "decided", "consumed"])
def test_connection_exit_revokes_every_unresolved_grant_without_replay(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], state: str) -> None:
    _jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    if state != "waiting":
        journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
    if state == "consumed":
        journal.consume(connection, requested["permission_id"], requested["digest"])
    journal.close(connection)
    saved = journal.snapshot("example-org/example-app")["connections"][0]
    assert saved["status"] == "closed"
    assert saved["requests"][requested["permission_id"]]["status"] == "invalidated"
    assert saved["live_connection_confirmed"] is False
    with pytest.raises(ReviewQueueError, match="no longer active"):
        journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")


def test_changed_feedback_revokes_decision_before_or_after_human_response(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str]) -> None:
    jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Changed direction while approval was pending"))
    with pytest.raises(ReviewQueueError, match="context changed"):
        journal.consume(connection, requested["permission_id"], requested["digest"])
    next_request = ask(journal, connection, provider_request_id="request2")
    with pytest.raises(ReviewQueueError, match="context changed"):
        journal.decide(connection, next_request["permission_id"], next_request["digest"], "allow_once")
    assert journal.snapshot("example-org/example-app")["connections"][0]["current_context"] is False


def test_changed_payload_or_extra_grant_and_duplicate_request_are_refused(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str]) -> None:
    _jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    with pytest.raises(ReviewQueueError, match="scope"):
        journal.decide(connection, requested["permission_id"], "0" * 64, "allow_once")
    with pytest.raises(ReviewQueueError, match="Session-wide"):
        journal.decide(connection, requested["permission_id"], requested["digest"], "allow_session")
    with pytest.raises(ReviewQueueError, match="duplicated"):
        ask(journal, connection)
    with pytest.raises(ReviewQueueError, match="payload changed"):
        journal.consume(connection, requested["permission_id"], "0" * 64)


@pytest.mark.parametrize("decided", [False, True])
def test_resolution_cannot_claim_delivery_before_consumption(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], decided: bool) -> None:
    _jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    if decided:
        journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
    with pytest.raises(ReviewQueueError, match="consumed decision"):
        journal.resolve(connection, requested["permission_id"])
    assert journal.snapshot("example-org/example-app")["connections"][0]["requests"][requested["permission_id"]]["status"] == ("decided" if decided else "waiting")


def test_new_session_grants_refused_and_historical_decision_not_replayed(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str]) -> None:
    _jobs, journal, connection = permissions_case
    with pytest.raises(ReviewQueueError, match="Session-wide"):
        ask(journal, connection, choices=["allow_session", "deny"])
    requested = ask(journal, connection)
    state = journal._load()
    old = state["connections"][connection]["requests"][requested["permission_id"]]
    old.update(status="decided", choices=["allow_session", "deny"], decision="allow_session")
    journal._save(state)
    assert journal.snapshot("example-org/example-app")["connections"][0]["requests"][requested["permission_id"]]["decision"] == "allow_session"
    with pytest.raises(ReviewQueueError, match="cannot be replayed"):
        journal.consume(connection, requested["permission_id"], requested["digest"])


def test_session_and_job_are_required_before_permissions_are_published(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str]) -> None:
    jobs, journal, connection = permissions_case
    job_id = journal.snapshot("example-org/example-app")["connections"][0]["job_id"]
    with pytest.raises(ReviewQueueError, match="persisted running session"):
        with journal.live_connection(job_id, "wrong-thread"):
            pass
    with pytest.raises(ReviewQueueError, match="active connection"):
        with journal.live_connection(job_id, "thread1"):
            pass
    jobs.fail(job_id)
    with pytest.raises(ReviewQueueError, match="no longer active"):
        ask(journal, connection)
    journal.close(connection)  # Terminal jobs still permit consent invalidation.


def test_failed_decision_write_never_becomes_deliverable(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    original = supervisor_permissions.save_private_json
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeStateError("Synthetic journal failure")
    monkeypatch.setattr(supervisor_permissions, "save_private_json", fail)
    with pytest.raises(RuntimeStateError):
        journal.decide(connection, requested["permission_id"], requested["digest"], "allow_once")
    monkeypatch.setattr(supervisor_permissions, "save_private_json", original)
    assert SupervisorPermissions(jobs).consume(connection, requested["permission_id"], requested["digest"]) is None


def test_only_digest_not_raw_provider_credentials_is_saved(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str]) -> None:
    jobs, journal, connection = permissions_case
    raw = {"scope": "synthetic", "sensitive_test_marker": "NOT_A_REAL_SECRET"}
    ask(journal, connection, digest=permission_digest(raw))
    saved = load_private_json(jobs.queue.directory, Path("supervisor-permissions.json"))
    assert "NOT_A_REAL_SECRET" not in json.dumps(saved)
    assert "sensitive_test_marker" not in json.dumps(saved)


def test_owner_lock_inspection_error_is_not_reported_as_a_live_connection(
    permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, journal, connection = permissions_case
    job_id = journal._load()["connections"][connection]["job_id"]
    original = supervision.run_lock

    @contextmanager
    def broken_lease(directory: Path, **kwargs: Any) -> Iterator[None]:
        if directory == jobs._owner_directory(job_id):
            raise RuntimeStateError("Synthetic owner-lock inspection failure")
        with original(directory, **kwargs):
            yield

    monkeypatch.setattr(supervision, "run_lock", broken_lease)
    with pytest.raises(RuntimeStateError, match="inspection failure"):
        journal.snapshot("example-org/example-app")
    assert jobs._load()["jobs"][job_id]["status"] == "running"


@pytest.mark.parametrize("changes", [{"choices": []}, {"choices": ["allow_persistently"]}, {"description": ""},
                                      {"turn_id": "bad turn"}, {"provider_request_id": True}, {"digest": "bad"}])
def test_malformed_permission_cannot_create_waiting_state(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], changes: dict[str, Any]) -> None:
    _jobs, journal, connection = permissions_case
    with pytest.raises(ReviewQueueError):
        ask(journal, connection, **changes)
    assert journal.snapshot("example-org/example-app")["connections"][0]["requests"] == {}


def test_native_bridge_reads_and_decides_without_model_or_merge_authority(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str]) -> None:
    jobs, journal, connection = permissions_case
    requested = ask(journal, connection)
    read = {"schema_version": 1, "request_id": "read_permissions", "operation": "supervisor_permissions",
            "repository": "example-org/example-app"}
    outgoing = io.BytesIO()
    assert exchange(jobs.queue, io.BytesIO(json.dumps(read).encode()), outgoing) == 0
    result = json.loads(outgoing.getvalue())
    assert result["connections"][0]["requests"][requested["permission_id"]]["status"] == "waiting"
    assert result["capabilities"] == {"github_mutations": False, "model_calls": False}
    decide_request = {**read, "operation": "supervisor_permission_decide", "connection_id": connection,
                      "permission_id": requested["permission_id"], "digest": requested["digest"], "decision": "deny"}
    outgoing = io.BytesIO()
    assert exchange(jobs.queue, io.BytesIO(json.dumps(decide_request).encode()), outgoing) == 0
    assert journal.consume(connection, requested["permission_id"], requested["digest"]) == "deny"
    assert not (jobs.queue.directory / "execution-audit.jsonl").exists()


@pytest.mark.parametrize("extra", [{"command": "arbitrary"}, {"profile": "full"}, {"approval_token": "fake"}])
def test_permission_wire_cannot_supply_executable_or_other_authority(permissions_case: tuple[SupervisorJobs, SupervisorPermissions, str], extra: dict[str, str]) -> None:
    jobs, journal, _connection = permissions_case
    outgoing = io.BytesIO()
    request = {"schema_version": 1, "request_id": "read_permissions", "operation": "supervisor_permissions",
               "repository": "example-org/example-app", **extra}
    assert exchange(jobs.queue, io.BytesIO(json.dumps(request).encode()), outgoing) == 2
    assert journal.snapshot(request["repository"])["connections"][0]["requests"] == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL process-crash integration")
@pytest.mark.parametrize("permission_state", ["waiting", "decided", "consumed", "decision_ack_race"])
def test_actual_owner_death_invalidates_consent_and_interrupts_job_without_replay(
    tmp_path: Path, permission_state: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "state"
    config = parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]})
    queue = ReviewQueue(config, directory)
    persist_plan(directory, saved_plan())
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    program = '''
import sys
import time
from pathlib import Path
from dependabot_watcher.config import parse_config
from dependabot_watcher.review_queue import ReviewQueue
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_permissions import SupervisorPermissions, permission_digest
from dependabot_watcher.runtime_state import RuntimeLockBusyError
config=parse_config({"schema_version":1,"repositories":[{"name":"example-org/example-app"}]})
jobs=SupervisorJobs(ReviewQueue(config,Path(sys.argv[1])))
jobs.start(sys.argv[2])
jobs.attach_session(sys.argv[2],"thread1")
journal=SupervisorPermissions(jobs)
with journal.live_connection(sys.argv[2],"thread1") as connection:
    journal.bind_turn(connection,"turn1")
    # Mirror the provider's nonblocking admission retry. The parent polls the
    # same journal; failed lock acquisition has not published any request.
    deadline = time.monotonic() + 5
    while True:
        try:
            journal.request(connection,provider_request_id=1,turn_id="turn1",item_id="item1",
                digest=permission_digest({"id":1,"scope":"synthetic"}),description="Synthetic read only",choices=["allow_once","deny"])
            break
        except RuntimeLockBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.02)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, "-c", program, str(directory), job["job_id"]],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    journal = SupervisorPermissions(jobs)
    try:
        deadline = time.monotonic() + 5
        connection: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            records = journal.snapshot(job["repository"])["connections"]
            if records and records[0]["requests"]:
                connection = records[0]
                break
            if child.poll() is not None:
                _, diagnostic = child.communicate(timeout=5)
                pytest.fail(f"Owner exited before publishing a permission: {diagnostic}")
            time.sleep(.02)
        assert connection is not None and connection["live_connection_confirmed"] is True
        requested = next(iter(connection["requests"].values()))
        if permission_state == "decision_ack_race":
            original = journal.decide

            def die_after_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
                result = original(*args, **kwargs)
                child.kill()
                child.wait(timeout=5)
                return result

            monkeypatch.setattr(journal, "decide", die_after_save)
            receipt = journal.handle({"schema_version":1,"request_id":"decision_owner_race",
                "operation":"supervisor_permission_decide","repository":job["repository"],
                "connection_id":connection["connection_id"],"permission_id":requested["permission_id"],
                "digest":requested["digest"],"decision":"allow_once"})
            acknowledged = receipt["connections"][0]
            assert acknowledged["status"] == "closed" and acknowledged["job_status"] == "interrupted"
            assert acknowledged["requests"][requested["permission_id"]]["status"] == "invalidated"
            assert acknowledged["requests"][requested["permission_id"]]["decision"] == "allow_once"
        elif permission_state != "waiting":
            journal.decide(connection["connection_id"], requested["permission_id"], requested["digest"], "allow_once")
        if permission_state == "consumed":
            assert journal.consume(connection["connection_id"], requested["permission_id"], requested["digest"]) == "allow_once"
        if child.poll() is None:
            child.kill()  # No Python finally: the OS must release the lease.
        child.wait(timeout=5)
        recovered = journal.snapshot(job["repository"])["connections"][0]
        assert recovered["status"] == "closed" and recovered["live_connection_confirmed"] is False
        assert recovered["requests"][requested["permission_id"]]["status"] == "invalidated"
        assert jobs.read(job["job_id"])["status"] == "interrupted"
        with pytest.raises(ReviewQueueError, match="no longer active"):
            journal.decide(connection["connection_id"], requested["permission_id"], requested["digest"], "allow_once")
        with pytest.raises(ReviewQueueError, match="recovery"):
            jobs.prepare(job["repository"])
        assert jobs.prepare(job["repository"], retry_job_id=job["job_id"])["job_id"] != job["job_id"]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
