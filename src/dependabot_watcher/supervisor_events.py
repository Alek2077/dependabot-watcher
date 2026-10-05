"""Backend-owned lifecycle sink for tool-capable provider adapters."""
from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .review_queue import ReviewQueueError
from .runtime_state import save_private_json
from .supervision import SupervisorJobs
from .supervisor_permissions import SupervisorPermissions
from .supervisor_workspace import SupervisorInstructionGuard, verify_supervisor_runtime_binding


class SupervisorEvents:
    """Record actual runtime identity/settings before starting useful work.

    This object is constructed by the runner, not model output or UI input.
    Providers report normalized requests; backend owns their durable decisions.
    """

    def __init__(self, jobs: SupervisorJobs, job: Mapping[str, Any], *, instruction_guard: SupervisorInstructionGuard | None = None) -> None:
        self.jobs = jobs
        self.job_id: str = job["job_id"]
        self.context_digest: str = job["context_digest"]
        self.permissions = SupervisorPermissions(jobs)
        self.connection_id: str | None = None
        self._owner: AbstractContextManager[str] | None = None
        self._instructions = instruction_guard or SupervisorInstructionGuard(job["context"].get("workspace_evidence"))
        self._context = job["context"]

    def connected(self, session_id: str, effective_settings: Mapping[str, Any]) -> None:
        self.ensure_current_authority(force=True)
        if self.connection_id is not None or effective_settings.get("session_id") != session_id:
            raise ReviewQueueError("Supervisor runtime settings do not match the connection.")
        verify_supervisor_runtime_binding(self._context, effective_settings)
        self.jobs.attach_session(self.job_id, session_id)
        save_private_json(self.jobs.queue.directory, Path("supervisor-runtime") / f"{self.job_id}.json",
                          {"schema_version": 1, "job_id": self.job_id, "context_digest": self.context_digest,
                           "effective_settings": dict(effective_settings)}, maximum_bytes=65_536)
        owner = self.permissions.live_connection(self.job_id, session_id)
        self.connection_id = owner.__enter__()
        self._owner = owner

    def started_turn(self, turn_id: str) -> None:
        self.ensure_current_authority(force=True)
        self.permissions.bind_turn(self._connection(), turn_id)

    def ensure_current_authority(self, *, force: bool = False) -> None:
        """A capability checkpoint, not a claim to undo already-started work."""
        self.jobs.queue.require_current_configuration()
        self._instructions.verify(force=force)

    def permission_requested(self, *, provider_request_id: str | int, turn_id: str, item_id: str,
                             digest: str, description: str, choices: list[str]) -> dict[str, Any]:
        return self.permissions.request(self._connection(), provider_request_id=provider_request_id,
                                        turn_id=turn_id, item_id=item_id, digest=digest,
                                        description=description, choices=choices)

    def permission_decision(self, permission_id: str, digest: str) -> str | None:
        return self.permissions.consume(self._connection(), permission_id, digest)

    def permission_resolved(self, permission_id: str) -> None:
        self.permissions.resolve(self._connection(), permission_id)

    def _connection(self) -> str:
        if self.connection_id is None:
            raise ReviewQueueError("Supervisor runtime connection has not been persisted.")
        return self.connection_id

    def close(self) -> None:
        if self._owner is not None:
            owner, self._owner = self._owner, None
            owner.__exit__(None, None, None)


@runtime_checkable
class EventfulSupervisorProvider(Protocol):
    def analyze_with_events(self, job: Mapping[str, Any], events: SupervisorEvents) -> dict[str, Any]: ...
