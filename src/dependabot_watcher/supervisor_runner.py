from __future__ import annotations

import copy
import re
import traceback
from pathlib import Path
from typing import Any, Mapping, Protocol

from .review_queue import ReviewConfigurationChanged, ReviewQueueError
from .runtime_state import save_private_json
from .supervision import SupervisorJobs, SupervisorResultError, _validate_result
from .supervisor_events import EventfulSupervisorProvider, SupervisorEvents
from .supervisor_workspace import SupervisorInstructionGuard


class SupervisorProvider(Protocol):
    def analyze(self, job: Mapping[str, Any]) -> dict[str, Any]: ...


def validate_observation(observation: Mapping[str, Any], job: Mapping[str, Any]) -> None:
    required = {"session_id", "analysis", "usage", "command_observations", "turn_completed",
                "provider", "model", "reasoning_effort", "profile", "workspace"}
    if (not isinstance(observation, Mapping) or set(observation) != required or observation.get("turn_completed") is not True
            or any(not isinstance(observation.get(name), str) or not observation[name].strip()
                   or len(observation[name]) > 128 for name in
                   ("session_id", "provider", "model", "reasoning_effort"))
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", observation["session_id"])
            or any(observation.get(name) is not None and (not isinstance(observation[name], str)
                   or len(observation[name]) > 4096) for name in ("profile", "workspace"))
            or not isinstance(observation.get("analysis"), dict)
            or not isinstance(observation.get("usage"), dict)
            or set(observation["usage"]) != {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
            or any(type(value) is not int or value < 0 for value in observation["usage"].values())
            or not isinstance(observation.get("command_observations"), list)
            or len(observation["command_observations"]) > 2000):
        raise ReviewQueueError("Supervisor adapter did not report a complete normalized observation.")
    for command in observation["command_observations"]:
        if (not isinstance(command, dict) or set(command) != {"status", "exit_code"}
                or not isinstance(command["status"], str)
                or command["status"] not in {"completed", "failed", "declined", "in_progress"}
                or (command["exit_code"] is not None and type(command["exit_code"]) is not int)):
            raise ReviewQueueError("Supervisor command observation is malformed.")
    _validate_result(observation["analysis"], job["context"])
    proof = job["context"].get("workspace_evidence")
    if proof is not None:
        policy = job["context"]["supervisor"]
        if (observation["workspace"] != proof["directory"] or observation["profile"] is not None
                or any(observation[field] != policy[field] for field in ("provider", "model", "reasoning_effort"))):
            raise ReviewQueueError("Supervisor observation differs from its selected runtime and workspace.")


class SupervisorRunner:
    """Connect a trusted reasoning adapter to the backend-owned job lifecycle.

    Model completion is evidence of analysis, not an execution authorization.
    Lost journal writes leave an accountable interrupted attempt rather than silently
    launching another agent. The caller can inspect private provider observation
    alongside that attempt to recover; no action can be replayed by this layer.
    """

    def __init__(self, jobs: SupervisorJobs, provider: SupervisorProvider) -> None:
        self.jobs = jobs
        self.provider = provider

    def _record_failure_location(self, job: Mapping[str, Any], error: Exception) -> None:
        """Best-effort internal locations, never exception text or frame locals."""
        try:
            package = Path(__file__).resolve().parent
            locations = []
            for frame, line in traceback.walk_tb(error.__traceback__):
                try:
                    relative = Path(frame.f_code.co_filename).resolve().relative_to(package).as_posix()
                except ValueError:
                    continue
                if len(relative) > 160 or not relative.endswith(".py"):
                    continue
                name = frame.f_code.co_name
                locations.append({"file": relative, "line": line,
                                  "function": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "internal"})
            save_private_json(self.jobs.queue.directory, Path("supervisor-failures") / f'{job["job_id"]}.json',
                              {"schema_version": 1, "job_id": job["job_id"],
                               "context_digest": job["context_digest"], "locations": locations[-32:]},
                              maximum_bytes=16_384)
        except Exception:
            # Diagnostics must not replace the original failure or weaken the
            # durable lifecycle/recovery gates, including when storage fails.
            pass

    def run(self, job_id: str) -> dict[str, Any]:
        self.jobs.queue.require_current_configuration()
        prepared = self.jobs.read(job_id)
        instructions = SupervisorInstructionGuard(prepared["context"].get("workspace_evidence"))
        instructions.verify(force=True)
        if prepared["context"].get("workspace_evidence") is not None and not isinstance(self.provider, EventfulSupervisorProvider):
            raise ReviewQueueError("Receipt-backed supervision requires checked runtime events, not a legacy adapter.")
        job = self.jobs.start(job_id, resume_session_id=getattr(self.provider, "resume_session_id", None))
        try:
            return self._run_started(job, instructions)
        finally:
            self.jobs.release_owner(job_id)

    def _run_started(self, job: dict[str, Any], instructions: SupervisorInstructionGuard) -> dict[str, Any]:
        job_id = job["job_id"]
        try:
            self.jobs.queue.require_current_configuration()
            instructions.verify(force=True)
            if isinstance(self.provider, EventfulSupervisorProvider):
                events = SupervisorEvents(self.jobs, job, instruction_guard=instructions)
                try:
                    observation = self.provider.analyze_with_events(copy.deepcopy(job), events)
                    if job["context"].get("workspace_evidence") is not None and events.connection_id is None:
                        raise ReviewQueueError("Supervisor completed without the selected runtime connection receipt.")
                finally:
                    events.close()
            else:
                observation = self.provider.analyze(copy.deepcopy(job))
        except Exception as exc:
            self._record_failure_location(job, exc)
            self.jobs.fail(job_id, interrupted=isinstance(exc, ReviewConfigurationChanged),
                           invalid_result=isinstance(exc, SupervisorResultError))
            raise
        # The trusted adapter returns only normalized evidence. No UI-controlled
        # observation path, executable, command or approval token is accepted.
        try:
            validate_observation(observation, job)
            if job["session_id"] is not None and observation["session_id"] != job["session_id"]:
                raise ReviewQueueError("Supervisor returned a different session than its scoped continuation.")
            self.jobs.attach_session(job_id, observation["session_id"])
        except ReviewQueueError as exc:
            self._record_failure_location(job, exc)
            self.jobs.fail(job_id, invalid_result=isinstance(exc, SupervisorResultError))
            raise
        # Persist observed output before acknowledging completion. A failed disk
        # write cannot turn a received reply into a completed/delivered job.
        save_private_json(self.jobs.queue.directory, Path("supervisor-observations") / f"{job_id}.json",
                          {"job_id": job_id, "context_digest": job["context_digest"], **observation},
                          maximum_bytes=2_000_000)
        try:
            self.jobs.queue.require_current_configuration()
            instructions.verify(force=True)
            completed = self.jobs.complete(job_id, observation["analysis"])
        except ReviewConfigurationChanged:
            # Preserve a received, validated observation as historical evidence,
            # but never promote it after policy revocation.
            self.jobs.fail(job_id, interrupted=True)
            raise
        return {"job": completed, "observation": observation}
