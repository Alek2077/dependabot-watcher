"""Durable provider-neutral permission decisions for one live job connection.

Access grants are not merge approvals. The provider supplies a bounded,
normalized description and the digest of its original request, never raw
provider output or credentials. Live process ownership remains with its runner.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .review_queue import ReviewQueueError
from .runtime_state import (
    load_private_json, run_lock, save_private_json,
)
from .supervision import SupervisorJobs, _digest, _time


_PATH = Path("supervisor-permissions.json")
_MAX_BYTES = 4_000_000
_MAX_CONNECTIONS = 50
_MAX_REQUESTS = 200
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def permission_digest(request: Mapping[str, Any]) -> str:
    """Transient request hash binds all requested scope without persisting it."""
    try:
        payload = json.dumps(dict(request), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ReviewQueueError("Supervisor permission request is not valid JSON.") from exc
    if len(payload) > 65_536:
        raise ReviewQueueError("Supervisor permission request is too large.")
    return hashlib.sha256(payload).hexdigest()


def _identity(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _validate_request(value: object) -> None:
    fields = {"permission_id", "provider_request_id", "thread_id", "turn_id", "item_id", "digest",
              "description", "choices", "status", "decision", "created_at", "resolved_at"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(not _identity(value[name]) for name in ("permission_id", "thread_id", "turn_id", "item_id"))
            or type(value["provider_request_id"]) not in (str, int)
            or (isinstance(value["provider_request_id"], str) and not _identity(value["provider_request_id"]))
            or not isinstance(value["digest"], str) or not _DIGEST.fullmatch(value["digest"])
            or not isinstance(value["description"], str) or not value["description"].strip()
            or len(value["description"].encode()) > 8192
            or not isinstance(value["choices"], list) or not value["choices"]
            or any(choice not in ("allow_once", "allow_session", "deny", "cancel") for choice in value["choices"])
            or len(set(value["choices"])) != len(value["choices"])
            or value["status"] not in ("waiting", "decided", "consumed", "resolved", "invalidated")
            or (value["decision"] is not None and value["decision"] not in value["choices"])):
        raise ReviewQueueError("Supervisor permission journal has an invalid request.")
    if (value["status"] == "waiting" and (value["decision"] is not None or value["resolved_at"] is not None)
            or value["status"] in ("decided", "consumed") and value["decision"] is None
            or value["status"] in ("resolved", "invalidated") and value["resolved_at"] is None):
        raise ReviewQueueError("Supervisor permission journal has inconsistent decision evidence.")
    created = _time(value["created_at"])
    if value["resolved_at"] is not None and _time(value["resolved_at"]) < created:
        raise ReviewQueueError("Supervisor permission resolution precedes its request.")


class SupervisorPermissions:
    """Persist before presenting, consume before sending, invalidate on exit.

    An independent OS lock is held by the job owner, never across the
    global job journal. Owner loss invalidates requests and interrupts the job;
    conversation history cannot reuse that consent. A held lock proves backend
    ownership, not that a remote model is responding.
    """

    def __init__(self, jobs: SupervisorJobs) -> None:
        self.jobs = jobs
        self.queue = jobs.queue

    def _load(self) -> dict[str, Any]:
        path = self.queue.directory / _PATH
        if not (path.exists() or path.is_symlink()):
            return {"schema_version": 1, "connections": {}}
        state = load_private_json(self.queue.directory, _PATH, maximum_bytes=_MAX_BYTES)
        if (set(state) != {"schema_version", "connections"} or type(state["schema_version"]) is not int
                or state["schema_version"] != 1 or not isinstance(state["connections"], dict)
                or len(state["connections"]) > _MAX_CONNECTIONS):
            raise ReviewQueueError("Supervisor permission journal is invalid.")
        for identifier, connection in state["connections"].items():
            if (not _identity(identifier) or not isinstance(connection, dict)
                    or set(connection) != {"connection_id", "job_id", "context_digest", "session_id", "turn_id", "status", "requests"}
                    or connection["connection_id"] != identifier
                    or not _identity(connection["job_id"]) or not _identity(connection["session_id"])
                    or not isinstance(connection["context_digest"], str) or not _DIGEST.fullmatch(connection["context_digest"])
                    or connection["status"] not in ("active", "closed")
                    or (connection["turn_id"] is not None and not _identity(connection["turn_id"]))
                    or not isinstance(connection["requests"], dict) or len(connection["requests"]) > _MAX_REQUESTS):
                raise ReviewQueueError("Supervisor permission connection is invalid.")
            seen: set[str | int] = set()
            for key, request in connection["requests"].items():
                _validate_request(request)
                if (key != request["permission_id"] or request["thread_id"] != connection["session_id"]
                        or request["turn_id"] != connection["turn_id"]
                        or request["provider_request_id"] in seen
                        or connection["status"] == "closed" and request["status"] in ("waiting", "decided", "consumed")):
                    raise ReviewQueueError("Supervisor permission request ownership is invalid.")
                seen.add(request["provider_request_id"])
        return state

    def _save(self, state: dict[str, Any]) -> None:
        save_private_json(self.queue.directory, _PATH, state, maximum_bytes=_MAX_BYTES)

    def _connection(self, state: dict[str, Any], identifier: str, *, active: bool = True) -> dict[str, Any]:
        return self._connection_from_jobs(state, identifier, self.jobs._load(), active=active)

    def _connection_from_jobs(
        self, state: dict[str, Any], identifier: str, jobs: Mapping[str, Any], *, active: bool = True,
    ) -> dict[str, Any]:
        """Validate against the already-validated job snapshot under the same lock.

        Ordinary action callers still load fresh jobs through _connection.
        Snapshot projection can reuse its one locked journal read instead of
        revalidating all historical jobs once per historical connection.
        """
        if not _identity(identifier) or identifier not in state["connections"]:
            raise ReviewQueueError("Supervisor permission connection is unavailable.")
        connection = state["connections"][identifier]
        job = self.jobs._job(jobs, connection["job_id"])
        if (job["context_digest"] != connection["context_digest"] or job["session_id"] != connection["session_id"]
                or active and (connection["status"] != "active" or job["status"] != "running")):
            raise ReviewQueueError("Supervisor permission connection is no longer active or bound to its job.")
        if active and not self._lease_is_live(connection):
            self._retire_orphan(connection)
            self._save(state)
            raise ReviewQueueError("Supervisor connection owner stopped; its permissions were invalidated.")
        return connection

    def _lease_is_live(self, connection: Mapping[str, Any]) -> bool:
        return self.jobs._owner_is_live(connection["job_id"])

    def _retire_orphan(self, connection: dict[str, Any]) -> None:
        for request in connection["requests"].values():
            if request["status"] in ("waiting", "decided", "consumed"):
                request.update(status="invalidated", resolved_at=self.jobs._now())
        connection["status"] = "closed"
        job = self.jobs._job(self.jobs._load(), connection["job_id"])
        if job["status"] == "running":
            self.jobs._fail_under_lock(connection["job_id"], interrupted=True)

    @contextmanager
    def live_connection(self, job_id: str, session_id: str) -> Iterator[str]:
        """Publish only under an already-owned job; close does not end the job."""
        with run_lock(self.queue.directory, wait_seconds=2):
            job = self.jobs._job(self.jobs._load(), job_id)
            if (job["status"] != "running" or job["session_id"] != session_id
                    or not self.jobs._owner_is_live(job_id)):
                raise ReviewQueueError("Supervisor permissions require a persisted running session.")
            state = self._load()
            if (len(state["connections"]) >= _MAX_CONNECTIONS
                    or any(c["job_id"] == job_id and c["status"] == "active" for c in state["connections"].values())):
                raise ReviewQueueError("Supervisor already has an active connection or its history is full.")
            identifier = "connection_" + secrets.token_hex(16)
            state["connections"][identifier] = {"connection_id": identifier, "job_id": job_id,
                "context_digest": job["context_digest"], "session_id": session_id,
                "turn_id": None, "status": "active", "requests": {}}
            self._save(state)
        try:
            yield identifier
        finally:
            self.close(identifier)

    def bind_turn(self, connection_id: str, turn_id: str) -> None:
        if not _identity(turn_id):
            raise ReviewQueueError("Supervisor turn identity is invalid.")
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            connection = self._connection(state, connection_id)
            if connection["turn_id"] is not None:
                raise ReviewQueueError("Supervisor connection already has a bound turn.")
            connection["turn_id"] = turn_id
            self._save(state)

    def request(self, connection_id: str, *, provider_request_id: str | int, turn_id: str, item_id: str,
                digest: str, description: str, choices: list[str]) -> dict[str, Any]:
        if any(choice not in {"allow_once", "deny", "cancel"} for choice in choices):
            raise ReviewQueueError("Session-wide permission grants are not supported by this client.")
        with run_lock(self.queue.directory):
            state = self._load()
            connection = self._connection(state, connection_id)
            if connection["turn_id"] != turn_id:
                raise ReviewQueueError("Supervisor permission belongs to a different turn.")
            if (len(connection["requests"]) >= _MAX_REQUESTS
                    or any(r["provider_request_id"] == provider_request_id for r in connection["requests"].values())):
                raise ReviewQueueError("Supervisor permission request is duplicated or history is full.")
            identifier = "permission_" + secrets.token_hex(16)
            request = {"permission_id": identifier, "provider_request_id": provider_request_id,
                       "thread_id": connection["session_id"], "turn_id": turn_id, "item_id": item_id,
                       "digest": digest, "description": description, "choices": list(choices),
                       "status": "waiting", "decision": None, "created_at": self.jobs._now(), "resolved_at": None}
            _validate_request(request)
            connection["requests"][identifier] = request
            self._save(state)
            return copy.deepcopy(request)

    def decide(self, connection_id: str, permission_id: str, digest: str, decision: str) -> dict[str, Any]:
        if decision not in {"allow_once", "deny", "cancel"}:
            raise ReviewQueueError("Session-wide permission grants are not supported by this client.")
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            connection = self._connection(state, connection_id)
            request = self._request(connection, permission_id)
            job = self.jobs._job(self.jobs._load(), connection["job_id"])
            current = self.queue._supervision_context_under_lock(job["repository"])
            if _digest(current) != connection["context_digest"]:
                raise ReviewQueueError("Supervisor context changed; the requested permission is stale.")
            if request["status"] != "waiting" or request["digest"] != digest or decision not in request["choices"]:
                raise ReviewQueueError("Supervisor permission decision is stale or outside the requested scope.")
            request.update(status="decided", decision=decision)
            self._save(state)
            return copy.deepcopy(request)

    @staticmethod
    def _request(connection: dict[str, Any], permission_id: str) -> dict[str, Any]:
        if not _identity(permission_id) or permission_id not in connection["requests"]:
            raise ReviewQueueError("Supervisor permission request is unavailable.")
        return connection["requests"][permission_id]

    def consume(self, connection_id: str, permission_id: str, digest: str) -> str | None:
        """Single-use decision; a failed send is unknown, never auto-replayed."""
        # This is a provider poll, not a user transaction. Never stall protocol
        # event reads while another short journal operation owns the lock.
        with run_lock(self.queue.directory):
            state = self._load()
            connection = self._connection(state, connection_id)
            request = self._request(connection, permission_id)
            job = self.jobs._job(self.jobs._load(), connection["job_id"])
            if _digest(self.queue._supervision_context_under_lock(job["repository"])) != connection["context_digest"]:
                raise ReviewQueueError("Supervisor context changed before permission delivery.")
            if request["digest"] != digest:
                raise ReviewQueueError("Supervisor permission payload changed before delivery.")
            if request["status"] == "waiting":
                return None
            if request["status"] != "decided":
                raise ReviewQueueError("Supervisor permission decision was already consumed or invalidated.")
            if request["decision"] not in {"allow_once", "deny", "cancel"}:
                raise ReviewQueueError("An old session-wide permission decision cannot be replayed.")
            request["status"] = "consumed"
            self._save(state)
            return request["decision"]

    def resolve(self, connection_id: str, permission_id: str) -> None:
        with run_lock(self.queue.directory):
            state = self._load()
            connection = self._connection(state, connection_id)
            request = self._request(connection, permission_id)
            if request["status"] in ("resolved", "invalidated"):
                return
            if request["status"] != "consumed":
                raise ReviewQueueError("Permission resolution requires a consumed decision.")
            request.update(status="resolved", resolved_at=self.jobs._now())
            self._save(state)

    def close(self, connection_id: str) -> None:
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            connection = self._connection(state, connection_id, active=False)
            for request in connection["requests"].values():
                if request["status"] in ("waiting", "decided", "consumed"):
                    request.update(status="invalidated", resolved_at=self.jobs._now())
            connection["status"] = "closed"
            self._save(state)

    def snapshot(self, repository: str) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            context = self.queue._supervision_context_under_lock(repository)
            state = self._load()
            jobs = self.jobs._load()["jobs"]
            recovered = False
            for connection in state["connections"].values():
                job = self.jobs._job({"jobs": jobs}, connection["job_id"])
                if job["repository"].casefold() != repository.casefold():
                    continue
                self._connection_from_jobs(state, connection["connection_id"], {"jobs": jobs}, active=False)
                if connection["status"] == "active" and not self._lease_is_live(connection):
                    self._retire_orphan(connection)
                    recovered = True
            if recovered:
                self._save(state)
            jobs = self.jobs._load()["jobs"]
            connections = []
            for connection in state["connections"].values():
                if jobs[connection["job_id"]]["repository"].casefold() != repository.casefold():
                    continue
                record = copy.deepcopy(connection)
                record["current_context"] = connection["context_digest"] == _digest(context)
                record["job_status"] = jobs[connection["job_id"]]["status"]
                record["live_connection_confirmed"] = (
                    connection["status"] == "active" and self._lease_is_live(connection))
                connections.append(record)
            return {"repository": repository, "connections": connections}

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"schema_version", "request_id", "operation", "repository"}
        operation = request.get("operation")
        if operation == "supervisor_permission_decide":
            fields |= {"connection_id", "permission_id", "digest", "decision"}
        if (set(request) != fields or type(request.get("schema_version")) is not int
                or request["schema_version"] != 1 or not _identity(request.get("request_id"))
                or not isinstance(request.get("repository"), str)
                or operation not in ("supervisor_permissions", "supervisor_permission_decide")):
            raise ReviewQueueError("Supervisor permissions require a bounded scoped request.")
        snapshot = self.snapshot(request["repository"])
        if operation == "supervisor_permission_decide":
            if (not _identity(request.get("connection_id")) or not _identity(request.get("permission_id"))
                    or not isinstance(request.get("digest"), str) or not _DIGEST.fullmatch(request["digest"])
                    or request.get("decision") not in ("allow_once", "allow_session", "deny", "cancel")
                    or request["connection_id"] not in {c["connection_id"] for c in snapshot["connections"]}):
                raise ReviewQueueError("Supervisor permission decision does not match the selected repository.")
            self.decide(request["connection_id"], request["permission_id"], request["digest"], request["decision"])
            snapshot = self.snapshot(request["repository"])
        return {"schema_version": 1, "request_id": request["request_id"], "status": "ok",
                "mode": operation, **snapshot, "capabilities": {"github_mutations": False, "model_calls": False}}
