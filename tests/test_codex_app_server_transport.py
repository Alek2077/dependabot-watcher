from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dependabot_watcher.providers import codex_app_server_transport as transport_module
from dependabot_watcher.providers.codex_app_server_transport import CodexAppServerTransport
from dependabot_watcher.review_queue import ReviewQueueError


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL owned-process transport")


def child(tmp_path: Path, program: str, timeout: int = 10) -> CodexAppServerTransport:
    return CodexAppServerTransport([sys.executable, "-u", "-c", program], tmp_path, timeout)


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_pending_approval_does_not_block_progress_or_implicitly_accept(tmp_path: Path, decision: str) -> None:
    program = """
import json, select, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({'id':request['id'], 'result': {'ready':True}}), flush=True)
print(json.dumps({'id':'permission1', 'method':'item/commandExecution/requestApproval',
                  'params': {'threadId':'thread1', 'turnId':'turn1', 'itemId':'item1'}}), flush=True)
auto = bool(select.select([sys.stdin], [], [], .2)[0])
print(json.dumps({'method':'item/progress', 'params': {'automatically_answered':auto}}), flush=True)
reply = json.loads(sys.stdin.readline())
print(json.dumps({'method':'decision/observed', 'params':reply}), flush=True)
"""
    with child(tmp_path, program) as connection:
        identifier = connection.send_request("initialize", {})
        response = connection.receive()
        assert response == {"id": identifier, "result": {"ready": True}}
        permission = connection.receive()
        assert permission is not None and permission["id"] == "permission1"
        assert connection.receive() == {"method": "item/progress", "params": {"automatically_answered": False}}
        connection.respond(permission, {"decision": decision})
        observed = connection.receive()
        assert observed is not None and observed["params"] == {"id": "permission1", "result": {"decision": decision}}
        with pytest.raises(ReviewQueueError, match="stale"):
            connection.respond(permission, {"decision": "accept"})


def test_server_resolution_invalidates_an_unanswered_permission(tmp_path: Path) -> None:
    program = """
import json,time
print(json.dumps({'id':7, 'method':'item/fileChange/requestApproval', 'params':{'threadId':'t'}}),flush=True)
print(json.dumps({'method':'serverRequest/resolved', 'params':{'requestId':7, 'threadId':'t'}}),flush=True)
time.sleep(5)
"""
    with child(tmp_path, program) as connection:
        permission = connection.receive()
        assert permission is not None
        connection.receive()
        with pytest.raises(ReviewQueueError, match="stale"):
            connection.respond(permission, {"decision": "accept"})


def test_changed_request_and_disconnect_cannot_consume_original_permission(tmp_path: Path) -> None:
    program = "import json,time; print(json.dumps({'id':1,'method':'permission','params':{'scope':'original'}}),flush=True);time.sleep(5)"
    connection = child(tmp_path, program)
    with connection:
        permission = connection.receive()
        assert permission is not None
        with pytest.raises(ReviewQueueError, match="stale"):
            connection.respond({**permission, "params": {"scope": "changed"}}, {})
    with pytest.raises(ReviewQueueError, match="stale"):
        connection.respond(permission, {})
    with pytest.raises(ReviewQueueError, match="restarted"):
        connection.start()


@pytest.mark.parametrize("raw", [
    'not-json', '[]', '{"method":"event","params":{"value":NaN}}',
    '{"method":"event","method":"other"}', '{"id":true,"method":"permission"}',
    '{"method":null}', '{"method":"event","result":{}}',
    '{"id":"unknown","result":{}}', '{"id":"unknown","result":{},"error":{}}',
])
def test_malformed_frames_fail_without_disclosing_raw_private_data(tmp_path: Path, raw: str) -> None:
    with child(tmp_path, f"print({raw!r}, flush=True)") as connection:
        with pytest.raises(ReviewQueueError) as error:
            connection.receive()
        assert raw not in str(error.value)


def test_duplicate_server_identity_is_rejected(tmp_path: Path) -> None:
    frame = {"id": 4, "method": "permission", "params": {}}
    program = f"print({json.dumps(frame)!r},flush=True);print({json.dumps(frame)!r},flush=True)"
    with child(tmp_path, program) as connection:
        connection.receive()
        with pytest.raises(ReviewQueueError, match="duplicated"):
            connection.receive()


def test_valid_response_followed_by_failed_child_exit_is_not_clean_completion(tmp_path: Path) -> None:
    program = "import sys,json; f=json.loads(sys.stdin.readline());print(json.dumps({'id':f['id'],'result':{}}),flush=True);sys.exit(1)"
    with pytest.raises(ReviewQueueError, match="exited unsuccessfully"):
        with child(tmp_path, program) as connection:
            connection.send_request("initialize", {})
            # A response may legitimately arrive before the exit is observed;
            # either receive or normal teardown must surface the failed child.
            connection.receive()


def test_failed_exit_does_not_replace_an_original_caller_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="original failure"):
        with child(tmp_path, "import sys;sys.exit(1)"):
            raise ValueError("original failure")


def test_fragmented_utf8_and_buffered_frames_before_eof_are_preserved(tmp_path: Path) -> None:
    frame = json.dumps({"method": "progress", "params": {"text": "zażółć"}}, ensure_ascii=False).encode() + b"\n"
    program = f"import sys; data={frame!r}; sys.stdout.buffer.write(data[:50]);sys.stdout.flush();sys.stdout.buffer.write(data[50:]+data);sys.stdout.flush()"
    with child(tmp_path, program) as connection:
        for _ in range(2):
            assert connection.receive() == {"method": "progress", "params": {"text": "zażółć"}}
        with pytest.raises(ReviewQueueError, match="disconnected"):
            connection.receive()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_oversized_output_is_drained_and_terminated(tmp_path: Path, stream: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CodexAppServerTransport, "MAX_STREAM", 4096)
    program = f"import sys,time;sys.{stream}.buffer.write(b'x'*200000);sys.{stream}.flush();time.sleep(30)"
    with child(tmp_path, program) as connection:
        with pytest.raises(ReviewQueueError, match="bounded"):
            connection.receive()


def test_unterminated_oversized_frame_and_event_backlog_fail_bounded(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CodexAppServerTransport, "MAX_FRAME", 100)
    with child(tmp_path, "import sys,time;sys.stdout.write('x'*200);sys.stdout.flush();time.sleep(5)") as connection:
        with pytest.raises(ReviewQueueError, match="frame exceeded"):
            connection.receive()
    monkeypatch.setattr(CodexAppServerTransport, "MAX_PENDING", 2)
    program = "import sys;sys.stdout.write('{\"method\":\"progress\"}\\n'*100);sys.stdout.flush()"
    with child(tmp_path, program) as connection:
        with pytest.raises(ReviewQueueError, match="event buffer"):
            connection.receive()


def test_stalled_input_and_process_lifetime_have_a_hard_bound(tmp_path: Path) -> None:
    started = time.monotonic()
    with child(tmp_path, "import time;time.sleep(30)", timeout=1) as connection:
        connection.send_request("large", {"data": "x" * 1_000_000})
        assert connection.receive(.1) is None
        with pytest.raises(ReviewQueueError, match="lifetime"):
            connection.receive()
    assert time.monotonic() - started < 5


def test_close_terminates_descendant_holding_the_transport_pipes(tmp_path: Path) -> None:
    program = """
import json,subprocess,sys,time
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
print(json.dumps({'method':'child','params':{'pid':p.pid}}),flush=True)
time.sleep(30)
"""
    with child(tmp_path, program) as connection:
        frame = connection.receive()
        assert frame is not None
        identifier = frame["params"]["pid"]
    # A killed orphan can remain briefly as an init-owned zombie; it is no
    # longer executing and cannot hold the transport open.
    state = Path(f"/proc/{identifier}/stat")
    if state.exists():
        assert state.read_text().split()[2] == "Z"


def test_input_limits_unknown_request_and_finite_poll_are_explicit(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    program = "import json,time;print(json.dumps({'id':'custom','method':'unknown/request','params':{}}),flush=True);time.sleep(5)"
    with child(tmp_path, program) as connection:
        assert connection.receive() == {"id": "custom", "method": "unknown/request", "params": {}}
        assert connection.receive(.01) is None
        monkeypatch.setattr(CodexAppServerTransport, "MAX_FRAME", 100)
        with pytest.raises(ReviewQueueError, match="input exceeded"):
            connection.send_request("work", {"data": "x" * 200})


def test_unsupported_windows_refuses_before_spawning(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module, "os", SimpleNamespace(name="nt"))
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Unsupported transport must not spawn")
    monkeypatch.setattr(transport_module.subprocess, "Popen", unexpected)
    with pytest.raises(ReviewQueueError, match="WSL backend"):
        child(tmp_path, "print('unused')").start()
