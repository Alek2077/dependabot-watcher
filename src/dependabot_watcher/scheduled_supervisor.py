"""Select bounded scheduled reasoning from saved state, not model commands.

This selection boundary is intended for the existing native launch owner.
Selection is not inference, completion, permission consent or merge authority.
"""
from __future__ import annotations

import re
from typing import Any

from .review_queue import ReviewQueue, ReviewQueueError, supervisor_context_digest
from .runtime_state import load_run_state, run_lock
from .supervisor_status import supervision_status


def _require_run(queue: ReviewQueue, run_id: str) -> None:
    queue.require_current_configuration()
    state = load_run_state(queue.directory)
    attempt = state.get("last_attempt")
    if not isinstance(attempt, dict) or attempt.get("run_id") != run_id:
        raise ReviewQueueError("Scheduled investigation no longer matches the latest saved run; no agent was started.")


def scheduled_supervisor_plan(queue: ReviewQueue, run_id: str) -> dict[str, Any]:
    """Choose current work and explain skips without launching or replaying.

    Every eventual launch still performs fresh policy/workspace/runtime checks.
    Status reads validate observations and recover dead owners. A final locked
    check rejects changed run/context while those independent reads occurred.
    The same current completion stays quiet; a same-context failure needs an
    explicit retry. Different-context live owners also prevent another launch.
    """
    if (queue.config_path is None or not isinstance(run_id, str)
            or re.fullmatch(r"run_[a-f0-9]{20}", run_id) is None):
        raise ReviewQueueError("Scheduled investigation requires a trusted profile and exact saved run.")
    with run_lock(queue.directory, wait_seconds=2):
        _require_run(queue, run_id)
    policy = queue.config.supervisor
    response: dict[str, Any] = {
        "schema_version": 1, "mode": "scheduled_supervisor_plan", "run_id": run_id,
        "status": "off" if policy.mode == "off" else "idle",
        "max_jobs_per_run": policy.max_jobs_per_run,
        "repositories": [], "skipped": [],
        "capabilities": {"model_calls": False, "github_mutations": False},
    }
    if policy.mode == "off":
        return response
    checked: dict[str, str] = {}
    for configured_repository in queue.config.repositories:
        repository = configured_repository.name
        status = supervision_status(queue, {
            "schema_version": 1, "request_id": "read_schedule", "operation": "supervisor_status",
            "repository": repository,
        })
        context = queue.supervision_context(repository)
        digest = supervisor_context_digest(context)
        if digest != status["context_digest"]:
            raise ReviewQueueError("Scheduled investigation context changed during selection; refresh before starting.")
        checked[repository] = digest
        jobs = status["jobs"]
        current = [job for job in jobs if job["current_context"]]
        completed = [job for job in current if job["status"] == "completed"]
        answered = {(message["proposal_id"], message["request_id"])
                    for job in jobs if job["provider_completion_observed"]
                    and job["status"] in {"completed", "superseded"}
                    for message in job["messages"]}
        unanswered = any((proposal["proposal_id"], message["request_id"]) not in answered
                         for proposal in context["proposals"] + context["retired_proposals"]
                         for message in proposal["feedback"])
        reason: str | None = None
        if status["configured_launch"]["status"] != "configured":
            reason = "configuration_blocked"
        elif context["evidence_status"] == "unavailable":
            reason = "evidence_unavailable"
        elif any(job["status"] == "running" for job in jobs):
            reason = "already_running"
        elif completed:
            reason = ("completion_unverified" if not all(job["provider_completion_observed"] for job in completed)
                      else "unanswered_feedback" if unanswered
                      else "evidence_incomplete" if context["evidence_status"] == "partial" and not context["proposals"]
                      else "completed_current")
        elif any(job["status"] in {"failed", "interrupted"} for job in current):
            reason = "recovery_required"
        else:
            pending = any(proposal["handling"] == "pending" for proposal in context["proposals"])
            if not pending and not unanswered:
                reason = "evidence_incomplete" if context["evidence_status"] == "partial" and not context["proposals"] else "no_work"
            elif len(response["repositories"]) >= policy.max_jobs_per_run:
                reason = "budget_deferred"
        if reason is not None:
            response["skipped"].append({"repository": repository, "reason": reason})
        else:
            response["repositories"].append({"repository": repository, "context_digest": digest})
    with run_lock(queue.directory, wait_seconds=2):
        _require_run(queue, run_id)
        for repository, digest in checked.items():
            if supervisor_context_digest(queue._supervision_context_under_lock(repository)) != digest:
                raise ReviewQueueError("Scheduled investigation changed before selection completed; no agent was started.")
    if response["repositories"]:
        response["status"] = "ready"
    elif any(item["reason"] not in {"completed_current", "no_work", "already_running"}
             for item in response["skipped"]):
        response["status"] = "attention"
    return response
