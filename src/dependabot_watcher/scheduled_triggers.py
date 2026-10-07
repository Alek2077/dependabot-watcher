"""Single-use scheduled investigation receipts, owned by the Python backend.

A receipt reserves the selected bounded set before any launch. Consuming a slot
is irreversible: crashes or lost replies never restore model-use authority.
Fresh repository collection may advance the latest run; the original receipt
continues to bind its configuration and reserved context, not a new run budget.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .review_queue import ReviewQueue, ReviewQueueError, supervisor_context_digest
from .runtime_state import load_private_json, run_lock, save_private_json
from .scheduled_supervisor import _require_run, scheduled_supervisor_plan
from .supervisor_status import supervision_status


_MAX_BYTES = 65_536


def _path(run_id: str) -> Path:
    if not isinstance(run_id, str) or re.fullmatch(r"run_[a-f0-9]{20}", run_id) is None:
        raise ReviewQueueError("Scheduled trigger requires an exact run identity.")
    return Path("scheduled-supervisor") / f"{run_id}.json"


def _policy(queue: ReviewQueue) -> str:
    if queue.config_path is None:
        raise ReviewQueueError("Scheduled trigger requires a trusted saved configuration.")
    queue.require_current_configuration()
    configuration = asdict(queue.config)
    for registration in configuration["supervisor"]["workspaces"]:
        if not registration["refresh_authorized"]:
            registration.pop("refresh_authorized")
    return supervisor_context_digest({"path": str(queue.config_path), "configuration": configuration})


def _load(queue: ReviewQueue, run_id: str) -> dict[str, Any]:
    value = load_private_json(queue.directory, _path(run_id), maximum_bytes=_MAX_BYTES)
    if (set(value) != {"schema_version", "run_id", "configuration_digest", "selection", "selection_digest", "slots"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["run_id"] != run_id or value["configuration_digest"] != _policy(queue)
            or not isinstance(value["selection"], dict)
            or value["selection_digest"] != supervisor_context_digest(value["selection"])
            or not isinstance(value["slots"], list)):
        raise ReviewQueueError("Scheduled trigger is malformed or belongs to a different configuration.")
    selection = value["selection"]
    if (set(selection) != {"schema_version", "mode", "run_id", "status", "max_jobs_per_run", "repositories", "skipped", "capabilities"}
            or type(selection.get("schema_version")) is not int or selection["schema_version"] != 1
            or selection["mode"] != "scheduled_supervisor_plan" or not isinstance(selection["status"], str)
            or selection["status"] != "ready"
            or selection["capabilities"] != {"model_calls": False, "github_mutations": False}
            or not isinstance(selection["skipped"], list)
            or selection.get("run_id") != run_id or type(selection.get("max_jobs_per_run")) is not int
            or selection["max_jobs_per_run"] != queue.config.supervisor.max_jobs_per_run
            or not isinstance(selection.get("repositories"), list)
            or not value["slots"] or len(value["slots"]) != len(selection["repositories"])
            or len(value["slots"]) > queue.config.supervisor.max_jobs_per_run):
        raise ReviewQueueError("Scheduled trigger has an invalid reserved workload.")
    seen: set[str] = set()
    for slot, selected in zip(value["slots"], selection["repositories"]):
        if (not isinstance(slot, dict) or set(slot) != {"repository", "context_digest", "phase", "job_id"}
                or not isinstance(selected, dict) or set(selected) != {"repository", "context_digest"}
                or not isinstance(slot["repository"], str)
                or not any(repo.name.casefold() == slot["repository"].casefold() for repo in queue.config.repositories)
                or slot["repository"].casefold() in seen
                or not isinstance(slot["context_digest"], str) or re.fullmatch(r"[a-f0-9]{64}", slot["context_digest"]) is None
                or {key: slot[key] for key in selected} != selected
                or not isinstance(slot["phase"], str) or slot["phase"] not in {"reserved", "started", "finished", "recovery_required"}
                or (slot["phase"] == "finished" and (not isinstance(slot["job_id"], str)
                    or re.fullmatch(r"job_[a-f0-9]{32}", slot["job_id"]) is None))
                or (slot["phase"] != "finished" and slot["job_id"] is not None)):
            raise ReviewQueueError("Scheduled trigger contains an invalid or ambiguous slot.")
        seen.add(slot["repository"].casefold())
    reasons = {"configuration_blocked", "evidence_unavailable", "already_running", "completion_unverified",
               "unanswered_feedback", "completed_current", "recovery_required", "no_work", "budget_deferred",
               "evidence_incomplete"}
    configured = {repo.name.casefold() for repo in queue.config.repositories}
    for skipped in selection["skipped"]:
        if (not isinstance(skipped, dict) or set(skipped) != {"repository", "reason"}
                or not isinstance(skipped["repository"], str)
                or skipped["repository"].casefold() not in configured
                or skipped["repository"].casefold() in seen
                or not isinstance(skipped["reason"], str) or skipped["reason"] not in reasons):
            raise ReviewQueueError("Scheduled trigger has an invalid skipped repository partition.")
        seen.add(skipped["repository"].casefold())
    if seen != configured:
        raise ReviewQueueError("Scheduled trigger omits configured repository scope.")
    return value


def claim_trigger(queue: ReviewQueue, run_id: str) -> dict[str, Any]:
    """Reserve once; an existing receipt never returns fresh launch slots."""
    path = _path(run_id)
    with run_lock(queue.directory, wait_seconds=2):
        _policy(queue)
        if os.path.lexists(queue.directory / path):
            return {"claimed": False, "receipt": _load(queue, run_id)}
    selection = scheduled_supervisor_plan(queue, run_id)
    with run_lock(queue.directory, wait_seconds=2):
        _require_run(queue, run_id)
        if os.path.lexists(queue.directory / path):
            return {"claimed": False, "receipt": _load(queue, run_id)}
        for selected in selection["repositories"]:
            if supervisor_context_digest(queue._supervision_context_under_lock(selected["repository"])) != selected["context_digest"]:
                raise ReviewQueueError("Scheduled context changed before reservation; no agent was started.")
        receipt = {"schema_version": 1, "run_id": run_id, "configuration_digest": _policy(queue),
            "selection": selection, "selection_digest": supervisor_context_digest(selection),
            "slots": [{**selected, "phase": "reserved", "job_id": None} for selected in selection["repositories"]]}
        if selection["status"] != "ready" or not receipt["slots"]:
            return {"claimed": False, "receipt": copy.deepcopy(receipt)}
        save_private_json(queue.directory, path, receipt, maximum_bytes=_MAX_BYTES)
        return {"claimed": True, "receipt": copy.deepcopy(receipt)}


def consume_slot(queue: ReviewQueue, run_id: str, repository: str, context_digest: str) -> None:
    """Write the consumed slot before collection or inference can start."""
    with run_lock(queue.directory, wait_seconds=2):
        receipt = _load(queue, run_id)
        slot = next((item for item in receipt["slots"] if item["repository"].casefold() == repository.casefold()), None)
        if slot is None or slot["context_digest"] != context_digest or slot["phase"] != "reserved":
            raise ReviewQueueError("Scheduled slot is absent, changed or already consumed; it was not retried.")
        if supervisor_context_digest(queue._supervision_context_under_lock(repository)) != context_digest:
            raise ReviewQueueError("Scheduled context changed before launch; refresh in a new run.")
        slot["phase"] = "started"
        save_private_json(queue.directory, _path(run_id), receipt, maximum_bytes=_MAX_BYTES)


def finish_slot(queue: ReviewQueue, run_id: str, repository: str, *, job_id: str | None = None) -> None:
    """Persist completion or uncertainty; neither state allows replay."""
    if job_id is not None and (not isinstance(job_id, str) or re.fullmatch(r"job_[a-f0-9]{32}", job_id) is None):
        raise ReviewQueueError("Scheduled completion requires an exact saved job identity.")
    if job_id is not None:
        status = supervision_status(queue, {"schema_version": 1, "request_id": "read_schedule_finish",
            "operation": "supervisor_status", "repository": repository})
        job = next((item for item in status["jobs"] if item["job_id"] == job_id), None)
        if (job is None or job["status"] not in {"completed", "superseded"}
                or job["result"] is None or not job["provider_completion_observed"]):
            raise ReviewQueueError("Scheduled completion has no verified saved provider outcome.")
    with run_lock(queue.directory, wait_seconds=2):
        receipt = _load(queue, run_id)
        slot = next((item for item in receipt["slots"] if item["repository"].casefold() == repository.casefold()), None)
        if slot is None or slot["phase"] != "started":
            raise ReviewQueueError("Scheduled completion does not match a consumed slot.")
        slot["phase"] = "finished" if job_id is not None else "recovery_required"
        slot["job_id"] = job_id
        save_private_json(queue.directory, _path(run_id), receipt, maximum_bytes=_MAX_BYTES)
