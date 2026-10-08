from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import time
from pathlib import Path
from typing import Any, cast

import pytest

from dependabot_watcher.github import ExecutionGitHubClient, GitHubClient
from dependabot_watcher.review_actions import ActionReviews
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewQueueError, supervisor_context_digest
from dependabot_watcher.runtime_state import load_run_state, save_run_state
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_bridge import _Frames, _preview, launch_exchange
from dependabot_watcher.supervisor_permissions import SupervisorPermissions
from test_supervisor_launch import launch_case
from test_supervisor_workspace import git, workspace_case
from test_execution import refresh_plan_identities
from test_review_queue import persist_plan


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL live client pipes")


def request(case: dict[str, Any]) -> dict[str, Any]:
    repository = "example-org/example-app"
    return {"schema_version": 1, "request_id": "launch_fixture", "operation": "supervisor_start",
        "repository": repository, "context_digest": supervisor_context_digest(case["queue"].supervision_context(repository))}


def test_start_and_exact_feedback_use_configured_job_through_existing_bridge(launch_case: dict[str, Any]) -> None:
    case = launch_case
    client = cast(GitHubClient, object())
    actions = ActionReviews(case["queue"], cast(ExecutionGitHubClient, client), config_path=case["path"])
    for expected_reuse in (False, True):
        output = io.BytesIO()
        assert exchange(case["queue"], io.BytesIO(json.dumps(request(case)).encode()), output, actions=actions) == 0
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        assert [frame["event"] for frame in frames] == ["starting", "finished"]
        assert [frame["sequence"] for frame in frames] == [0, 1]
        assert frames[-1]["reused"] is expected_reuse
        assert SupervisorJobs(case["queue"]).read(frames[-1]["job_id"])["status"] == "completed"
    assert len(case["launches"]) == 1


def test_failed_launch_is_exposed_and_explicit_retry_crosses_the_actual_bridge(launch_case: dict[str, Any]) -> None:
    case = launch_case
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    assert launch_exchange(case["queue"], cast(GitHubClient, object()), request(case), io.BytesIO()) == 2
    status_request = {"schema_version": 1, "request_id": "read_retry", "operation": "supervisor_status",
        "repository": "example-org/example-app"}
    output = io.BytesIO()
    assert exchange(case["queue"], io.BytesIO(json.dumps(status_request).encode()), output) == 0
    failed = json.loads(output.getvalue())["jobs"][0]
    assert failed["status"] == "failed" and failed["failure"] == "provider_failed" and failed["retry_available"] is True
    incoming = {**request(case), "retry_job_id": failed["job_id"]}
    case["program"].write_text(case["program"].read_text().replace("FAULT='failed_turn'", "FAULT=''").replace("'thread1'", "'thread2'"))
    output = io.BytesIO()
    assert launch_exchange(case["queue"], cast(GitHubClient, object()), incoming, output) == 0
    terminal = json.loads(output.getvalue().splitlines()[-1])
    assert terminal["job_status"] == "completed" and terminal["job_id"] != failed["job_id"]
    assert SupervisorJobs(case["queue"]).read(terminal["job_id"])["retry_of"] == failed["job_id"]
    assert launch_exchange(case["queue"], cast(GitHubClient, object()), incoming, io.BytesIO()) == 2
    assert len(case["launches"]) == 2


@pytest.mark.parametrize("changed_base", [False, True])
def test_post_merge_observation_reaches_actual_protocol_and_completed_context_is_reused(launch_case: dict[str, Any], changed_base: bool) -> None:
    case = launch_case
    client = cast(GitHubClient, object())
    first = io.BytesIO()
    assert launch_exchange(case["queue"], client, request(case), first) == 0
    before = json.loads(first.getvalue().splitlines()[-1])
    state = load_run_state(case["queue"].directory)
    state["last_attempt"] = {"execution_observation": {"observations": [{
        "action_id": "action_" + "a" * 24, "repository": "example-org/example-app", "pull_request": 42,
        "head_sha": "a" * 40, "status": "success_observed", "recovery_required": False,
    }]}}
    save_run_state(case["queue"].directory, state)
    # Simulate refreshed evidence with another exact head, not an actual merge.
    step = case["report"]["repositories"][0]["recommended_order"][0]
    step["identity"]["head_sha"] = "c" * 40
    step["freshness"]["head_sha"] = "c" * 40
    if changed_base:
        git(case["worktree"], "commit", "--allow-empty", "-m", "Synthetic post-merge base")
        head = git(case["worktree"], "rev-parse", "HEAD")
        case["report"]["repositories"][0]["evidence_snapshot"]["base_sha"] = head
        step["freshness"]["base_sha"] = head
    case["report"].update(refresh_plan_identities(case["report"]))
    persist_plan(case["queue"].directory, case["report"])
    code = case["program"].read_text().replace("proposals=data['context']['proposals']",
        "assert data['context']['recent_actions'][0]['pull_request']==42\n"
        "        assert data['context']['recent_actions'][0]['status']=='success_observed'\n"
        "        assert data['context']['previous_assessment']['applicability']=='historical_recheck_before_use'\n"
        "        assert data['context']['previous_assessment']['recommendations'][0]['item']['head_sha']=='" + "a" * 40 + "'\n"
        "        assert data['context']['mutation_authorized'] is False\n"
        "        proposals=data['context']['proposals']")
    case["program"].write_text(code)
    for reused in (False, True):
        output = io.BytesIO()
        assert launch_exchange(case["queue"], client, request(case), output) == 0, output.getvalue().decode()
        terminal = json.loads(output.getvalue().splitlines()[-1])
        assert terminal["job_status"] == "completed" and terminal["reused"] is reused
        assert terminal["job_id"] != before["job_id"]
        job = SupervisorJobs(case["queue"]).read(terminal["job_id"])
        assert job["context"]["recent_actions"][0]["pull_request"] == 42
        assert job["context"]["previous_assessment"]["source_job_id"] == before["job_id"]
        assert job["session_id"] == "thread1"
    assert len(case["launches"]) == 2


def test_session_only_access_has_no_supported_preview() -> None:
    with pytest.raises(ReviewQueueError, match="no supported"):
        _preview({"command": "cat fixture.txt", "cwd": "/worktree", "availableDecisions": ["acceptForSession"]})


def test_explicit_local_environment_preserves_the_exact_command_preview() -> None:
    params = {"command": "gh pr view 42", "cwd": "/worktree", "environmentId": "local"}
    assert _preview(params) == _preview({key: value for key, value in params.items() if key != "environmentId"})
    for environment in ("remote1", "LOCAL", "", 42, {}):
        with pytest.raises(ReviewQueueError):
            _preview({**params, "environmentId": environment})


def test_preview_follows_saved_exact_permission_and_reaches_deny_without_persisting_command(
    launch_case: dict[str, Any],
) -> None:
    case = launch_case
    code = case["program"].read_text().replace("PERMISSION=False", "PERMISSION=True")
    code = code.replace("'sensitive_marker':'PRIVATE_TEST_ONLY'", "'cwd':params['cwd']")
    code = code.replace("cat synthetic-fixture.txt", "cat PRIVATE_PREVIEW_ONLY.txt")
    case["program"].write_text(code)
    journal = SupervisorPermissions(SupervisorJobs(case["queue"]))

    class Client(io.BytesIO):
        def write(self, payload: Any) -> int:
            frame = json.loads(payload)
            if frame["event"] == "permission_preview":
                target = frame["request"]
                saved = journal.snapshot(frame["repository"])["connections"][0]
                assert saved["connection_id"] == target["connection_id"]
                assert saved["requests"][target["permission_id"]]["digest"] == target["digest"]
                assert "PRIVATE_PREVIEW_ONLY" in frame["preview"]["command"]
                outgoing = io.BytesIO()
                decision = {"schema_version": 1, "request_id": "permission_fixture", "operation": "supervisor_permission_decide",
                    "repository": frame["repository"], "connection_id": target["connection_id"],
                    "permission_id": target["permission_id"], "digest": target["digest"], "decision": "deny"}
                assert exchange(case["queue"], io.BytesIO(json.dumps(decision).encode()), outgoing) == 0
                assert json.loads(outgoing.getvalue())["mode"] == "supervisor_permission_decide"
            return super().write(payload)

    output = Client()
    assert launch_exchange(case["queue"], cast(GitHubClient, object()), request(case), output) == 0
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [frame["event"] for frame in frames] == ["starting", "permission_preview", "finished"]
    assert [frame["sequence"] for frame in frames] == [0, 1, 2]
    permission = next(iter(journal._load()["connections"].values()))
    assert next(iter(permission["requests"].values()))["status"] == "resolved"
    for path in case["queue"].directory.rglob("*.json"):
        assert "PRIVATE_PREVIEW_ONLY" not in path.read_text(), f"Raw command leaked to {path.name}"


def test_failed_live_preview_invalidates_request_without_grant_or_raw_diagnostics(launch_case: dict[str, Any]) -> None:
    case = launch_case
    code = case["program"].read_text().replace("PERMISSION=False", "PERMISSION=True")
    code = code.replace("'sensitive_marker':'PRIVATE_TEST_ONLY'", "'cwd':params['cwd']")
    case["program"].write_text(code)

    class DisconnectedClient(io.BytesIO):
        def write(self, payload: Any) -> int:
            if json.loads(payload)["event"] == "permission_preview":
                raise ReviewQueueError("PRIVATE_PREVIEW_FAILURE")
            return super().write(payload)

    output = DisconnectedClient()
    assert launch_exchange(case["queue"], cast(GitHubClient, object()), request(case), output) == 2
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [frame["event"] for frame in frames] == ["starting", "error"]
    assert b"PRIVATE_PREVIEW_FAILURE" not in output.getvalue()
    jobs = SupervisorJobs(case["queue"])
    assert next(iter(jobs._load()["jobs"].values()))["status"] == "failed"
    permission = next(iter(SupervisorPermissions(jobs)._load()["connections"].values()))
    saved = next(iter(permission["requests"].values()))
    assert permission["status"] == "closed" and saved["status"] == "invalidated" and saved["decision"] is None
    assert not list((case["queue"].directory / "supervisor-observations").glob("*.json"))


@pytest.mark.parametrize("fault", ["model", "digest", "request_id"])
def test_invalid_or_stale_start_does_not_spend_inference(launch_case: dict[str, Any], fault: str) -> None:
    case = launch_case
    incoming = request(case)
    if fault == "model":
        incoming["model"] = "unselected-model"
    elif fault == "digest":
        incoming["context_digest"] = "f" * 64
    else:
        incoming["request_id"] = "read_not_a_launch"
    output = io.BytesIO()
    assert launch_exchange(case["queue"], cast(GitHubClient, object()), incoming, output) == 2
    frame = json.loads(output.getvalue())
    assert frame["event"] == "error" and frame["sequence"] == 0
    assert frame["mode"] == "supervisor_launch" and "unselected-model" not in frame["message"]
    assert case["launches"] == []


@pytest.mark.parametrize("fault", ["writeStdin", "remote", "missing_command", "missing_cwd", "unknown_field",
    "bad_network", "entry_overlay", "relative_path", "bad_network_overlay", "empty_choices"])
def test_undisclosed_or_unsupported_permission_scope_is_not_actionable(fault: str) -> None:
    params: dict[str, Any] = {"command": "cat fixture.txt", "cwd": "/worktree"}
    if fault == "writeStdin":
        params["kind"] = "writeStdin"
    elif fault == "remote":
        params["environmentId"] = "remote1"
    elif fault == "missing_command":
        params.pop("command")
    elif fault == "missing_cwd":
        params.pop("cwd")
    elif fault == "unknown_field":
        params["futureScope"] = "unpresented"
    elif fault == "bad_network":
        params["networkApprovalContext"] = {"host": "example.test", "protocol": []}
    elif fault == "entry_overlay":
        params["additionalPermissions"] = {"fileSystem": {"entries": []}}
    elif fault == "relative_path":
        params["additionalPermissions"] = {"fileSystem": {"read": ["relative"]}}
    elif fault == "bad_network_overlay":
        params["additionalPermissions"] = {"network": {"enabled": "yes"}}
    else:
        params["availableDecisions"] = []
    with pytest.raises(ReviewQueueError):
        _preview(params)


def test_network_and_additional_scope_are_preserved_without_persistent_amendments() -> None:
    params = {"networkApprovalContext": {"host": "example.test:443", "protocol": "https"},
        "additionalPermissions": {"fileSystem": {"read": ["/read"], "write": ["/write"]}, "network": {"enabled": True}},
        "availableDecisions": ["accept", "decline", {"applyNetworkPolicyAmendment": {"host": "example.test"}}]}
    preview = _preview(params)
    assert preview["kind"] == "network" and preview["network"] == params["networkApprovalContext"]
    assert preview["additional_permissions"] == params["additionalPermissions"]
    assert preview["choices"] == ["allow_once", "deny"]


def test_real_pipe_frames_are_bounded_and_regular_log_destinations_are_refused(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb", buffering=0) as output:
        frames = _Frames(output, {"request_id": "launch_pipe", "repository": "example-org/example-app"})
        try:
            frames.emit("starting", status="ok")
            assert os.get_blocking(write_fd) is False
            with pytest.raises(ReviewQueueError, match="output limit"):
                frames.emit("permission_preview", preview="x" * 65_536)
        finally:
            frames.close()
        assert os.get_blocking(write_fd) is True
    with os.fdopen(read_fd, "rb") as incoming:
        assert json.loads(incoming.read())["sequence"] == 0
    with (tmp_path / "not-a-live-pipe.log").open("wb") as output:
        with pytest.raises(ReviewQueueError, match="pipe"):
            _Frames(output, {"request_id": "launch_log", "repository": "example-org/example-app"})
    assert (tmp_path / "not-a-live-pipe.log").read_bytes() == b""


def test_nonreading_and_disconnected_clients_cannot_block_frame_delivery_indefinitely(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb", buffering=0) as output:
        frames = _Frames(output, {"request_id": "launch_pipe", "repository": "example-org/example-app"})
        monkeypatch.setattr(_Frames, "WRITE_TIMEOUT_SECONDS", .05)
        try:
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            started = time.monotonic()
            with pytest.raises(ReviewQueueError, match="not receiving"):
                frames.emit("starting")
            assert time.monotonic() - started < 1
            os.close(read_fd)
            with pytest.raises(ReviewQueueError, match="disconnected"):
                frames.emit("starting")
        finally:
            frames.close()


@pytest.mark.parametrize("fault", ["stale_request", "starting", "permission_preview"])
def test_actual_bridge_never_uses_blocking_legacy_error_writer_for_live_launch(
    launch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    case = launch_case
    incoming = request(case)
    if fault == "stale_request":
        incoming["context_digest"] = "f" * 64
    if fault == "permission_preview":
        code = case["program"].read_text().replace("PERMISSION=False", "PERMISSION=True")
        case["program"].write_text(code.replace("'sensitive_marker':'PRIVATE_TEST_ONLY'", "'cwd':params['cwd']"))
    actions = ActionReviews(case["queue"], cast(ExecutionGitHubClient, object()), config_path=case["path"])
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(_Frames, "WRITE_TIMEOUT_SECONDS", .05)
    original_emit = _Frames.emit

    def fill_pipe() -> None:
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break

    def emit(self: _Frames, event: str, **details: Any) -> None:
        if (fault == "stale_request" and event == "error") or event == fault:
            fill_pipe()
        original_emit(self, event, **details)

    monkeypatch.setattr(_Frames, "emit", emit)
    with os.fdopen(write_fd, "wb", buffering=0) as output:
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(exchange, case["queue"], io.BytesIO(json.dumps(incoming).encode()), output, actions=actions)
            try:
                assert future.result(timeout=3) == 2
                assert os.get_blocking(write_fd) is True
            finally:
                # A broken regression cannot strand pytest on a blocked pipe.
                os.close(read_fd)
    assert len(case["launches"]) == int(fault == "permission_preview")
    if fault == "permission_preview":
        jobs = SupervisorJobs(case["queue"])
        assert next(iter(jobs._load()["jobs"].values()))["status"] == "failed"
        connection = next(iter(SupervisorPermissions(jobs)._load()["connections"].values()))
        assert connection["status"] == "closed"
        assert next(iter(connection["requests"].values()))["status"] == "invalidated"


def test_unconnected_start_returns_only_a_stream_error_frame(launch_case: dict[str, Any]) -> None:
    case = launch_case
    output = io.BytesIO()
    assert exchange(case["queue"], io.BytesIO(json.dumps(request(case)).encode()), output) == 2
    frame = json.loads(output.getvalue())
    assert frame["mode"] == "supervisor_launch" and frame["event"] == "error" and frame["sequence"] == 0
    assert case["launches"] == []


@pytest.mark.parametrize("stage", ["startup", "quiet_turn", "permission"])
def test_reader_exit_stops_actual_configured_child_without_waiting_for_another_frame(
    launch_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    """Actual OS pipe and protocol child; only GitHub/model service is replaced."""
    case = launch_case
    marker = case["program"].parent / "provider-stage.txt"
    code = case["program"].read_text().replace("import json, sys", "import json, sys, os, time\nfrom pathlib import Path")
    signal = f"Path({str(marker)!r}).write_text(str(os.getpid()))"
    if stage == "startup":
        code = code.replace("if method=='initialize':", f"if method=='initialize':\n        {signal}\n        time.sleep(5)")
    elif stage == "quiet_turn":
        code = code.replace("proposals=data['context']['proposals']", f"{signal}\n        time.sleep(5)\n        proposals=data['context']['proposals']")
    else:
        code = code.replace("PERMISSION=False", "PERMISSION=True")
        code = code.replace("'sensitive_marker':'PRIVATE_TEST_ONLY'", "'cwd':params['cwd']")
        # A broken implementation can fail the timing assertion, not strand
        # the suite forever on an unanswered provider stdin request.
        code = code.replace("grant=json.loads(sys.stdin.readline())", "time.sleep(5)\n            grant=json.loads(sys.stdin.readline())")
        original_emit = _Frames.emit

        def emit(self: _Frames, event: str, **details: Any) -> None:
            original_emit(self, event, **details)
            if event == "permission_preview":
                connection = SupervisorPermissions(SupervisorJobs(case["queue"])).snapshot(
                    "example-org/example-app")["connections"][0]
                assert next(iter(connection["requests"].values()))["status"] == "waiting"
                marker.write_text("permission recorded and emitted")

        monkeypatch.setattr(_Frames, "emit", emit)
    case["program"].write_text(code)
    incoming = request(case)
    read_fd, write_fd = os.pipe()
    reader_closed = False
    provider_pid: int | None = None
    with os.fdopen(write_fd, "wb", buffering=0) as output, ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(launch_exchange, case["queue"], cast(GitHubClient, object()), incoming, output)
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and not future.done() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists(), "The real configured provider did not reach the tested stage"
            if stage != "permission":
                provider_pid = int(marker.read_text())
                os.kill(provider_pid, 0)
            started = time.monotonic()
            os.close(read_fd)
            reader_closed = True
            assert future.result(timeout=2) == 2
            assert time.monotonic() - started < 2
            if stage != "permission":
                assert provider_pid is not None
                with pytest.raises(ProcessLookupError):
                    os.kill(provider_pid, 0)
        finally:
            if not reader_closed:
                os.close(read_fd)
    jobs = SupervisorJobs(case["queue"])
    job = next(iter(jobs._load()["jobs"].values()))
    assert job["status"] == "failed" and not jobs._owner_is_live(job["job_id"])
    assert not list((case["queue"].directory / "supervisor-observations").glob("*.json"))
    if stage == "permission":
        connection = SupervisorPermissions(jobs).snapshot("example-org/example-app")["connections"][0]
        assert connection["status"] == "closed" and connection["live_connection_confirmed"] is False
        saved = next(iter(connection["requests"].values()))
        assert saved["status"] == "invalidated" and saved["decision"] is None


def test_consumer_already_gone_prevents_collection_or_provider_launch(launch_case: dict[str, Any]) -> None:
    case = launch_case
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    with os.fdopen(write_fd, "wb", buffering=0) as output:
        assert launch_exchange(case["queue"], cast(GitHubClient, object()), request(case), output) == 2
    assert case["collections"] == [] and case["launches"] == []


def test_pipe_consumer_check_is_nonwriting_and_detects_reader_exit() -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb", buffering=0) as output:
        frames = _Frames(output, {"request_id": "launch_pipe", "repository": "example-org/example-app"})
        try:
            frames.require_consumer()
            assert frames.sequence == 0 and frames.total == 0
            os.close(read_fd)
            with pytest.raises(ReviewQueueError, match="disconnected"):
                frames.require_consumer()
            assert frames.sequence == 0 and frames.total == 0
        finally:
            frames.close()
