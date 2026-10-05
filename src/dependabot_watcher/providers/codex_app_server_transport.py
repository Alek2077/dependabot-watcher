"""Bounded local Codex protocol transport; no approval or repository policy.

One caller owns the connection and dispatches frames. Server requests remain
pending while progress events continue; no callback runs on a reader thread.
This POSIX transport is also used by the Windows interface through WSL.
"""
from __future__ import annotations

import copy
import json
import os
import selectors
import signal
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..review_queue import ReviewQueueError


class CodexAppServerTransport:
    """Own one stdio process group, with bounded input, output and lifetime.

    The trusted backend supplies argv, never a window message. Consumers must
    validate thread/turn/job identity and select permitted methods/decisions.
    Raw frames are transient private data, not log or journal contents.
    """

    MAX_FRAME = 2_000_000
    MAX_BUFFER = 8_000_000
    MAX_STREAM = 32_000_000
    MAX_PENDING = 128

    def __init__(self, command: list[str], workspace: Path, timeout_seconds: int = 900,
                 *, consumer_check: Callable[[], None] | None = None) -> None:
        if (not command or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)
                or not workspace.is_absolute() or not workspace.is_dir()
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600):
            raise ReviewQueueError("Codex transport requires a workspace, command and bounded lifetime.")
        self._command = list(command)
        self._workspace = workspace
        self._timeout = timeout_seconds
        self._consumer_check = consumer_check
        self._process: subprocess.Popen[bytes] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._deadline = 0.0
        self._input = bytearray()
        self._partial = bytearray()
        self._frames: deque[tuple[dict[str, Any], int]] = deque()
        self._queued_bytes = 0
        self._counts = {"stdin": 0, "stdout": 0, "stderr": 0}
        self._next_id = 0
        self._responses: set[str] = set()
        self._requests: dict[str | int, dict[str, Any]] = {}
        self._seen_requests: set[str | int] = set()
        self._stdout_closed = False
        self._started = False

    def __enter__(self) -> CodexAppServerTransport:
        self.start()
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        self.close(check_exit=exc_type is None)

    def start(self) -> None:
        if self._started:
            raise ReviewQueueError("A Codex transport cannot be restarted or shared between attempts.")
        if os.name != "posix":
            raise ReviewQueueError("Direct Windows supervisor execution is not supported yet; use the WSL backend.")
        self._require_consumer()
        self._started = True
        try:
            self._process = subprocess.Popen(self._command, cwd=self._workspace,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
            self._selector = selectors.DefaultSelector()
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(self._process, name)
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                if name != "stdin":
                    self._selector.register(stream, selectors.EVENT_READ, name)
            self._deadline = time.monotonic() + self._timeout
        except (OSError, ValueError) as exc:
            self.close(check_exit=False)
            raise ReviewQueueError("Codex app-server could not be launched.") from exc

    def send_request(self, method: str, params: dict[str, Any]) -> str:
        if len(self._responses) >= self.MAX_PENDING:
            self._fail("Too many pending Codex requests.")
        self._next_id += 1
        identifier = f"watcher_{self._next_id}"
        self._enqueue({"id": identifier, "method": method, "params": params})
        self._responses.add(identifier)
        return identifier

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._enqueue({"method": method, "params": params or {}})

    def respond(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        """Consume exactly the live request received on this connection.

        This method does not select approval decisions. The caller must apply
        the separately persisted capability/consent policy before responding.
        """
        identifier = request.get("id")
        if (type(identifier) not in (str, int) or identifier not in self._requests
                or request != self._requests[identifier]):
            raise ReviewQueueError("Codex permission request is stale or does not match this connection.")
        self._enqueue({"id": identifier, "result": result})
        del self._requests[identifier]

    def _enqueue(self, frame: dict[str, Any]) -> None:
        self._require_consumer()
        process, selector = self._connected()
        if self._stdout_closed:
            self._fail("Codex app-server disconnected; pending requests are invalid.")
        if time.monotonic() >= self._deadline:
            self._fail("Codex app-server exceeded its configured lifetime.")
        if "method" in frame and (not isinstance(frame["method"], str) or not frame["method"]):
            raise ReviewQueueError("Codex protocol method must be explicit.")
        try:
            encoded = (json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ReviewQueueError("Codex protocol input is not valid JSON.") from exc
        if (len(encoded) > self.MAX_FRAME or len(self._input) + len(encoded) > self.MAX_BUFFER
                or self._counts["stdin"] + len(encoded) > self.MAX_STREAM):
            self._fail("Codex protocol input exceeded its bounded contract.")
        self._counts["stdin"] += len(encoded)
        if not self._input:
            assert process.stdin is not None
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        self._input.extend(encoded)

    def receive(self, wait_seconds: float | None = None) -> dict[str, Any] | None:
        """Pump all pipes until one frame arrives; a finite poll may return None.

        The overall lifetime still applies while awaiting a user permission.
        No server request is automatically answered, including unknown methods.
        """
        if wait_seconds is not None and (not isinstance(wait_seconds, (int, float))
                                         or not 0 < wait_seconds <= 3600):
            raise ReviewQueueError("Codex polling wait must be positive and bounded.")
        process, selector = self._connected()
        poll_deadline = self._deadline if wait_seconds is None else min(
            self._deadline, time.monotonic() + wait_seconds)
        while True:
            self._require_consumer()
            if time.monotonic() >= self._deadline:
                self._fail("Codex app-server exceeded its configured lifetime.")
            if process.poll() not in (None, 0):
                self._fail("Codex app-server exited unsuccessfully; its connection is invalid.")
            if self._frames:
                frame, size = self._frames.popleft()
                self._queued_bytes -= size
                return self._route(frame)
            if self._stdout_closed:
                self._fail("Codex app-server disconnected; pending requests are invalid.")
            remaining = poll_deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                # A quiet provider or unanswered permission must not keep an
                # abandoned native owner's connection alive until its timeout.
                wait = min(remaining, .25) if self._consumer_check is not None else remaining
                for key, _events in selector.select(wait):
                    if time.monotonic() >= self._deadline:
                        self._fail("Codex app-server exceeded its configured lifetime.")
                    self._pump(key)
            except (OSError, ValueError) as exc:
                self.close(check_exit=False)
                raise ReviewQueueError("Codex app-server communication failed.") from exc

    def _pump(self, key: selectors.SelectorKey) -> None:
        assert self._selector is not None
        if key.data == "stdin":
            try:
                count = os.write(key.fd, self._input[:65_536])
            except BlockingIOError:
                return
            del self._input[:count]
            if not self._input:
                self._selector.unregister(key.fileobj)
            return
        try:
            block = os.read(key.fd, 65_536)
        except BlockingIOError:
            return
        if not block:
            self._selector.unregister(key.fileobj)
            if key.data == "stdout":
                self._stdout_closed = True
                if self._partial:
                    self._fail("Codex app-server ended with an incomplete frame.")
            return
        self._counts[key.data] += len(block)
        if self._counts[key.data] > self.MAX_STREAM:
            self._fail("Codex app-server output exceeded its bounded contract.")
        if key.data == "stderr":
            return  # Drain diagnostics without retaining secrets or tool output.
        self._partial.extend(block)
        while (end := self._partial.find(b"\n")) >= 0:
            size = end + 1
            if size > self.MAX_FRAME:
                self._fail("Codex app-server frame exceeded its bounded contract.")
            raw = self._partial[:end]
            del self._partial[:size]
            try:
                frame = json.loads(raw, parse_constant=self._reject_constant,
                                   object_pairs_hook=self._unique_keys)
                if not isinstance(frame, dict):
                    raise ValueError
            except (ValueError, UnicodeError, RecursionError) as exc:
                self.close(check_exit=False)
                raise ReviewQueueError("Codex app-server returned an invalid frame.") from exc
            self._queued_bytes += size
            if len(self._frames) >= self.MAX_PENDING or self._queued_bytes > self.MAX_BUFFER:
                self._fail("Codex app-server event buffer exceeded its bounded contract.")
            self._frames.append((frame, size))
        if len(self._partial) >= self.MAX_FRAME:
            self._fail("Codex app-server frame exceeded its bounded contract.")

    @staticmethod
    def _reject_constant(_value: str) -> None:
        raise ValueError("Non-finite JSON number")

    @staticmethod
    def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate JSON key")
            result[name] = value
        return result

    def _route(self, frame: dict[str, Any]) -> dict[str, Any]:
        if "method" in frame:
            if (not isinstance(frame["method"], str) or not frame["method"]
                    or "result" in frame or "error" in frame or not isinstance(frame.get("params", {}), dict)):
                self._fail("Codex app-server returned a malformed request or event.")
            if "id" in frame:
                identifier = frame["id"]
                if (type(identifier) not in (int, str) or identifier in self._seen_requests
                        or len(self._requests) >= self.MAX_PENDING or len(self._seen_requests) >= 4096):
                    self._fail("Codex app-server request identity is invalid or duplicated.")
                self._seen_requests.add(identifier)
                self._requests[identifier] = copy.deepcopy(frame)
            elif frame["method"] == "serverRequest/resolved":
                identifier = frame.get("params", {}).get("requestId")
                if type(identifier) not in (int, str):
                    self._fail("Codex app-server request resolution is malformed.")
                self._requests.pop(identifier, None)
        elif (type(frame.get("id")) is not str or frame["id"] not in self._responses
              or ("result" in frame) == ("error" in frame)):
            self._fail("Codex app-server response does not match a pending request.")
        else:
            self._responses.remove(frame["id"])
        return frame

    def _connected(self) -> tuple[subprocess.Popen[bytes], selectors.BaseSelector]:
        if self._process is None or self._selector is None:
            raise ReviewQueueError("Codex app-server connection is not active.")
        return self._process, self._selector

    def _require_consumer(self) -> None:
        if self._consumer_check is not None:
            try:
                self._consumer_check()
            except Exception as exc:
                self.close(check_exit=False)
                raise ReviewQueueError("Live review client disconnected; the owned investigation was stopped.") from exc

    def _fail(self, message: str) -> None:
        self.close(check_exit=False)
        raise ReviewQueueError(message)

    def close(self, *, check_exit: bool = True) -> None:
        process, self._process = self._process, None
        selector, self._selector = self._selector, None
        self._requests.clear()
        self._responses.clear()
        self._frames.clear()
        self._input.clear()
        self._partial.clear()
        self._queued_bytes = 0
        unsuccessful_exit = False
        if process is not None:
            # EOF permits normal runtime shutdown and exposes a crash racing
            # its last response. Only our deliberate timeout/kill is excluded
            # from a failed-exit observation, never a pre-existing failure.
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=.2)
                unsuccessful_exit = process.returncode != 0
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if selector is not None:
            selector.close()
        if unsuccessful_exit and check_exit:
            raise ReviewQueueError("Codex app-server exited unsuccessfully; no clean connection completion was established.")
