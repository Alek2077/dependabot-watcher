"""Read-only current intelligence gate; a recommendation is never merge consent."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .config import WatcherConfig
from .review_queue import ReviewQueue, ReviewQueueError, supervisor_context_digest
from .runtime_state import load_private_json, validate_user_owned_directory
from .supervision import SupervisorJobs
from .supervisor_runner import validate_observation
from .supervisor_workspace import verify_supervisor_runtime_binding, verify_supervisor_workspace


def require_supervisor_recommendation(
    config: WatcherConfig, directory: Path, saved_plan: Mapping[str, Any], step_id: str,
) -> dict[str, Any] | None:
    """Validate without writes, inference or consent; execution owns its lock.

    Use the actual scoped queue/job/observation, never displayed model prose.
    Both preparation and the final executor call this gate. Missing, historical,
    failed, superseded or conservative reasoning cannot become action readiness.
    """
    if config.supervisor.mode == "off":
        return None
    # Refuse missing/untrusted state before readers can create or repair it.
    validate_user_owned_directory(directory)
    if directory.stat().st_mode & 0o077:
        raise ReviewQueueError("Supervised action requires an existing private state directory.")
    repository_plan = next((record for record in saved_plan["repositories"]
        if any(step["step_id"] == step_id for step in record["recommended_order"])), None)
    if repository_plan is None:
        raise ReviewQueueError("Supervised action is absent from the selected plan.")
    repository = repository_plan["repository"]
    queue = ReviewQueue(config, directory)
    current_plan = queue._current_plan()
    if current_plan is None or current_plan != saved_plan:
        raise ReviewQueueError("Supervised action requires the current saved queue plan.")
    context = queue._supervision_context_under_lock(repository, persist=False)
    matches = [item for item in context["proposals"] if item["step"]["step_id"] == step_id]
    if len(matches) != 1 or matches[0]["handling"] != "pending":
        raise ReviewQueueError("Supervised action requires one current pending proposal.")
    proof = context["workspace_evidence"]
    if proof is None:
        raise ReviewQueueError("Supervised action requires verified workspace evidence and current agent reasoning.")
    fresh_proof = asdict(verify_supervisor_workspace(config, repository, repository_plan))
    if supervisor_context_digest(fresh_proof) != supervisor_context_digest(proof):
        raise ReviewQueueError("Supervised workspace or trusted instructions changed; investigate again.")
    digest = supervisor_context_digest(context)
    jobs = SupervisorJobs(queue)._load()["jobs"].values()
    current = [job for job in jobs if job["repository"].casefold() == repository.casefold()
               and job["context_digest"] == digest]
    if not current:
        raise ReviewQueueError("No completed agent investigation covers this current action context.")
    job = max(current, key=lambda item: (datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")), item["job_id"]))
    if job["status"] != "completed" or job["result"] is None:
        raise ReviewQueueError("The latest current agent attempt is not accepted as completed; inspect its outcome.")
    runtime = load_private_json(directory, Path("supervisor-runtime") / f'{job["job_id"]}.json',
                                maximum_bytes=65_536)
    settings = runtime.get("effective_settings")
    if (set(runtime) != {"schema_version", "job_id", "context_digest", "effective_settings"}
            or type(runtime.get("schema_version")) is not int or runtime["schema_version"] != 1
            or runtime.get("job_id") != job["job_id"] or runtime.get("context_digest") != digest
            or not isinstance(settings, dict) or settings.get("session_id") != job["session_id"]):
        raise ReviewQueueError("Supervised action has no matching runtime-settings receipt.")
    verify_supervisor_runtime_binding(job["context"], settings)
    observed = load_private_json(directory, Path("supervisor-observations") / f'{job["job_id"]}.json',
                                 maximum_bytes=2_000_000)
    if (observed.get("job_id") != job["job_id"] or observed.get("context_digest") != digest
            or observed.get("session_id") != job["session_id"] or observed.get("analysis") != job["result"]):
        raise ReviewQueueError("Supervised action has no matching provider-completion receipt.")
    observation = {key: value for key, value in observed.items() if key not in {"job_id", "context_digest"}}
    validate_observation(observation, job)
    recommendation = next((item for item in job["result"]["recommendations"]
                           if item["proposal_id"] == matches[0]["proposal_id"]), None)
    if (recommendation is None or recommendation["conclusion"] != "recommend"
            or recommendation.get("ready_for_action") is not True):
        raise ReviewQueueError("The agent has not explicitly marked this exact action ready for approval; ask it to investigate or clarify.")
    # Preparation is a read-only optimistic view. Reject concurrent feedback or
    # job changes rather than creating a lock/state file; execution repeats this
    # same check under its existing operation lock before any remote mutation.
    current_again = {item["job_id"]: item for item in SupervisorJobs(queue)._load()["jobs"].values()
                     if item["repository"].casefold() == repository.casefold() and item["context_digest"] == digest}
    if (supervisor_context_digest(queue._supervision_context_under_lock(repository, persist=False)) != digest
            or current_again != {item["job_id"]: item for item in current}):
        raise ReviewQueueError("Supervisor context changed while checking action readiness; refresh before approval.")
    return {"job_id": job["job_id"], "context_digest": digest, "proposal_id": matches[0]["proposal_id"],
            "provider": observed["provider"], "model": observed["model"],
            "reasoning_effort": observed["reasoning_effort"], "session_id": observed["session_id"],
            "conclusion": recommendation["conclusion"], "explanation": recommendation["explanation"],
            "ready_for_action": True,
            "assurance": "current_provider_completion", "mutation_authorized": False}
