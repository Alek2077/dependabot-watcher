"""Bounded live launch frames for the native client; no listener or worker.

Permission previews are ephemeral and may be sensitive. Journals retain only
safe descriptions and exact request digests. Decisions still travel through
the existing independently checked permission bridge, never through frames.
"""
from __future__ import annotations

import io
import json
import os
import re
import select
import selectors
import stat
import time
from typing import Any, BinaryIO, Mapping

from .github import GitHubClient
from .providers.codex_app_server_supervisor import _available_decisions
from .review_queue import ReviewQueue, ReviewQueueError, supervisor_context_digest
from .supervisor_launch import run_configured_supervisor
from .scheduled_triggers import consume_slot, finish_slot


class _Frames:
    """Write bounded frames without stranding the owner on a nonreading client."""

    WRITE_TIMEOUT_SECONDS = 5

    def __init__(self, output: BinaryIO, request: Mapping[str, Any]) -> None:
        self.output = output
        self.request = request
        self.sequence = 0
        self.total = 0
        self.descriptor: int | None = None
        self.blocking = True
        if not isinstance(output, io.BytesIO):
            try:
                self.descriptor = output.fileno()
                if os.name != "posix" or not stat.S_ISFIFO(os.fstat(self.descriptor).st_mode):
                    raise ReviewQueueError("Live investigation previews require the owned POSIX/WSL client pipe, not a log file.")
                self.blocking = os.get_blocking(self.descriptor)
                os.set_blocking(self.descriptor, False)
            except (OSError, ValueError, io.UnsupportedOperation) as exc:
                raise ReviewQueueError("Live investigation requires an owned output pipe.") from exc

    def close(self) -> None:
        if self.descriptor is not None:
            os.set_blocking(self.descriptor, self.blocking)

    def require_consumer(self) -> None:
        """Check the owned pipe without sending heartbeats or private output.

        This detects reader exit, not user visibility or a reader's ability to
        make progress. Bounded writes separately detect nonreading clients.
        """
        if self.output.closed:
            raise ReviewQueueError("Live review client disconnected.")
        if self.descriptor is not None:
            poll = select.poll()
            poll.register(self.descriptor, select.POLLERR | select.POLLHUP)
            if any(events & (select.POLLERR | select.POLLHUP | select.POLLNVAL)
                   for _descriptor, events in poll.poll(0)):
                raise ReviewQueueError("Live review client disconnected.")

    def emit(self, event: str, **details: Any) -> None:
        frame = {"schema_version": 1, "request_id": self.request["request_id"],
            "mode": "supervisor_launch", "repository": self.request["repository"],
            "sequence": self.sequence, "event": event, **details}
        try:
            payload = (json.dumps(frame, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ReviewQueueError("Live investigation frame is invalid.") from exc
        if len(payload) > 65_536 or self.total + len(payload) > 16_000_000:
            raise ReviewQueueError("Live investigation exceeded its output limit.")
        if self.descriptor is None:
            self.output.write(payload)
        else:
            deadline = time.monotonic() + self.WRITE_TIMEOUT_SECONDS
            remaining = memoryview(payload)
            with selectors.DefaultSelector() as selector:
                selector.register(self.descriptor, selectors.EVENT_WRITE)
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not selector.select(wait):
                        raise ReviewQueueError("Live review client is not receiving investigation frames.")
                    try:
                        count = os.write(self.descriptor, remaining)
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        raise ReviewQueueError("Live review client disconnected.") from exc
                    if count <= 0:
                        raise ReviewQueueError("Live review client disconnected.")
                    remaining = remaining[count:]
        self.sequence += 1
        self.total += len(payload)


def _preview(params: Mapping[str, Any]) -> dict[str, Any]:
    """Display requested access, not inferred command intent or persistent rules.

    This first local route supports command/network requests and legacy explicit
    additional filesystem paths. Terminal input, remote environments and newer
    entry/glob overlays need their own complete presentation before support.
    """
    fields = {"additionalPermissions", "approvalId", "availableDecisions", "command", "commandActions", "cwd",
        "environmentId", "itemId", "kind", "networkApprovalContext", "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments", "reason", "startedAtMs", "threadId", "turnId"}
    if set(params) - fields:
        raise ReviewQueueError("Agent access request contains unsupported preview fields.")
    if params.get("kind", "command") != "command" or params.get("environmentId") not in (None, "local"):
        raise ReviewQueueError("This live review cannot present the requested terminal input or remote environment.")
    details = {name: params.get(name) for name in ("command", "cwd", "reason")}
    for name, value in details.items():
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or len(value.encode("utf-8")) > 16_384 or "\0" in value):
            raise ReviewQueueError("Agent access preview lacks valid bounded text.")
    network = params.get("networkApprovalContext")
    if network is not None:
        if (not isinstance(network, dict) or set(network) != {"host", "protocol"}
                or not isinstance(network["host"], str) or not network["host"].strip()
                or len(network["host"].encode()) > 1024
                or not isinstance(network["protocol"], str)
                or network["protocol"] not in {"http", "https", "socks5Tcp", "socks5Udp"}):
            raise ReviewQueueError("Agent network access lacks an exact supported destination.")
    elif details["command"] is None or details["cwd"] is None:
        raise ReviewQueueError("Agent command access requires its exact command and working directory.")
    extra = params.get("additionalPermissions")
    if extra is not None:
        if not isinstance(extra, dict) or set(extra) - {"fileSystem", "network"}:
            raise ReviewQueueError("Agent access overlay is unsupported.")
        file_system = extra.get("fileSystem")
        if file_system is not None:
            if (not isinstance(file_system, dict) or set(file_system) - {"read", "write", "entries", "globScanMaxDepth"}
                    or file_system.get("entries") is not None or file_system.get("globScanMaxDepth") is not None):
                raise ReviewQueueError("Agent filesystem overlay needs a supported complete preview.")
            for access in ("read", "write"):
                paths = file_system.get(access)
                if paths is not None and (not isinstance(paths, list) or len(paths) > 32
                        or any(not isinstance(path, str) or not path.startswith("/") or "\0" in path
                               or len(path.encode()) > 4096 for path in paths)):
                    raise ReviewQueueError("Agent filesystem access paths are invalid.")
        additional_network = extra.get("network")
        if additional_network is not None and (not isinstance(additional_network, dict)
                or set(additional_network) - {"enabled"}
                or additional_network.get("enabled") is not None and type(additional_network["enabled"]) is not bool):
            raise ReviewQueueError("Agent network overlay is invalid.")
    available = _available_decisions(params)
    choices = [local for local, wire in (("allow_once", "accept"),
        ("deny", "decline"), ("cancel", "cancel")) if wire in available]
    if not choices:
        raise ReviewQueueError("Agent access has no supported ordinary decision.")
    # Do not persist this object. Encoding the complete frame applies the final
    # combined size bound; never truncate requested scope to enable a grant.
    return {"kind": "network" if network is not None else "command", **details,
        "network": network, "additional_permissions": extra, "choices": choices}


def launch_exchange(queue: ReviewQueue, client: GitHubClient | None, request: Mapping[str, Any], output: BinaryIO) -> int:
    """Start one explicitly selected investigation and stream its live previews.

    The caller closes the one-request input frame as in the existing bridge.
    The backend enforces provider lifetime and bounded writes; the native owner
    must drain frames and leave its independent decision controls usable.
    """
    # Even prelaunch/refusal errors use this bounded channel, never the legacy
    # writer after blocking mode has been restored. Keep malformed input out of
    # diagnostic envelopes; a dead client simply receives no response.
    envelope = {key: request.get(key) if isinstance(request.get(key), str)
                and len(request[key]) <= limit else None for key, limit in (("request_id", 64), ("repository", 255))}
    try:
        frames = _Frames(output, envelope)
    except Exception:
        return 2
    try:
        return _launch(queue, client, request, frames)
    except Exception:
        try:
            frames.emit("error", status="error", message="Investigation did not complete. Inspect its saved job and recovery state; it was not automatically retried.")
        except Exception:
            pass  # Disconnected/nonreading client: no blocking fallback writer.
        return 2
    finally:
        try:
            frames.close()
        except (OSError, ValueError):
            pass


def _launch(queue: ReviewQueue, client: GitHubClient | None, request: Mapping[str, Any], frames: _Frames) -> int:
    required = {"schema_version", "request_id", "operation", "repository", "context_digest"}
    if (not required <= set(request) or set(request) - required - {"retry_job_id", "scheduled_run_id"}
            or ("scheduled_run_id" in request and ("retry_job_id" in request
                or not isinstance(request["scheduled_run_id"], str)
                or re.fullmatch(r"run_[a-f0-9]{20}", request["scheduled_run_id"]) is None))
            or ("retry_job_id" in request and (not isinstance(request["retry_job_id"], str)
                or re.fullmatch(r"job_[a-f0-9]{32}", request["retry_job_id"]) is None))
            or type(request.get("schema_version")) is not int or request["schema_version"] != 1
            or request.get("operation") != "supervisor_start"
            or not isinstance(request.get("request_id"), str)
            or re.fullmatch(r"launch_[a-zA-Z0-9_-]{1,57}", request["request_id"]) is None
            or not isinstance(request.get("repository"), str)
            or not isinstance(request.get("context_digest"), str)
            or re.fullmatch(r"[a-f0-9]{64}", request["context_digest"]) is None):
        raise ReviewQueueError("Investigation launch requires an exact scoped context request.")
    if client is None:
        raise ReviewQueueError("Investigation is not connected to this backend.")
    if supervisor_context_digest(queue.supervision_context(request["repository"])) != request["context_digest"]:
        raise ReviewQueueError("Investigation settings or queue changed; refresh before starting.")

    def present(params: Mapping[str, Any]) -> tuple[str, list[str]]:
        preview = _preview(params)
        description = ("Agent requests managed network access." if preview["kind"] == "network"
                       else "Agent requests command execution.")
        return description + " Exact sensitive preview is available only in the live review window.", preview["choices"]

    def notice(params: Mapping[str, Any], saved: Mapping[str, Any]) -> None:
        frames.emit("permission_preview", request={key: saved[key] for key in
            ("job_id", "context_digest", "connection_id", "permission_id", "thread_id", "turn_id", "item_id", "digest")},
            preview=_preview(params))

    scheduled_run_id = request.get("scheduled_run_id")
    frames.require_consumer()
    if scheduled_run_id is not None:
        consume_slot(queue, scheduled_run_id, request["repository"], request["context_digest"])
    try:
        frames.emit("starting", status="ok")
        result = run_configured_supervisor(queue, client, request["repository"],
            permission_presentation=present, permission_notice=notice, consumer_check=frames.require_consumer,
            retry_job_id=request.get("retry_job_id"))
        if scheduled_run_id is not None:
            finish_slot(queue, scheduled_run_id, request["repository"], job_id=result["job"]["job_id"])
    except Exception:
        if scheduled_run_id is not None:
            finish_slot(queue, scheduled_run_id, request["repository"])
        raise
    frames.emit("finished", status="ok", job_id=result["job"]["job_id"],
                context_digest=result["job"]["context_digest"], job_status=result["job"]["status"],
                reused=result["reused"])
    return 0
