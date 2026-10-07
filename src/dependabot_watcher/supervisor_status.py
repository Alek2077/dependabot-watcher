from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any, Mapping

from .review_queue import ReviewQueue, ReviewQueueError
from .runtime_state import load_private_json, run_lock
from .supervision import SupervisorJobs, _digest
from .supervisor_runner import validate_observation
from .supervisor_launch import configured_launch_summary


def _message_receipts(job: Mapping[str, Any], result: Mapping[str, Any], *, current: bool) -> list[dict[str, Any]]:
    """Project conversation identity from validated saved context, never model prose.

    Recommendation revisions may retire a proposal while its question is still
    being answered. This display-only lineage must not transfer action authority.
    """
    questions: dict[tuple[str, str], dict[str, Any]] = {}
    context = job["context"]
    for proposal in context["proposals"] + context["retired_proposals"]:
        identity = proposal["step"]["identity"]
        for question in proposal["feedback"]:
            receipt = {
                "proposal_id": proposal["proposal_id"], "request_id": question["request_id"],
                "question": question["message"], "created_at": question["created_at"],
                "item": {"repository": proposal["repository"], "kind": proposal["step"]["kind"],
                         "number": identity["number"], "head_sha": identity.get("head_sha"),
                         "expected_base": identity.get("expected_base"),
                         "observed_base": identity.get("observed_base")},
                "processing": "answered_current" if current else "answered_historical",
            }
            key = (proposal["proposal_id"], question["request_id"])
            if key in questions and questions[key] != receipt:
                raise ReviewQueueError("Saved question has ambiguous conversation identity.")
            questions[key] = receipt
    return [questions[(reply["proposal_id"], reply["request_id"])] for reply in result["replies"]]


def supervision_status(queue: ReviewQueue, request: Mapping[str, Any]) -> dict[str, Any]:
    """Read one repository's recorded reasoning and message-processing evidence.

    This operation never launches an agent. Missing provider observation is not
    treated as delivery; stale advice is explicitly historical, not current.
    """
    if (set(request) != {"schema_version", "request_id", "operation", "repository"}
            or type(request.get("schema_version")) is not int or request["schema_version"] != 1
            or request.get("operation") != "supervisor_status"
            or not isinstance(request.get("request_id"), str)
            or not re.fullmatch(r"read_[a-zA-Z0-9_-]{1,91}", request["request_id"])
            or not isinstance(request.get("repository"), str)):
        raise ReviewQueueError("Supervisor status requires a scoped read-only request.")
    with run_lock(queue.directory, wait_seconds=2):
        context = queue._supervision_context_under_lock(request["repository"])
        digest = _digest(context)
        jobs = SupervisorJobs(queue)
        state = jobs._load()
        jobs._recover_under_lock(state, request["repository"])
        retry = jobs.retry_candidate(state, request["repository"], digest)
        records = []
        # Journal JSON is key-sorted for durability; random job IDs are not a
        # timeline. Consumers use the final matching record as the newest answer.
        ordered = sorted(state["jobs"].values(), key=lambda job: (
            datetime.fromisoformat(job["created_at"].replace("Z", "+00:00")), job["job_id"]))
        for job in ordered:
            if job["repository"].casefold() != request["repository"].casefold():
                continue
            relative = Path("supervisor-observations") / f'{job["job_id"]}.json'
            path = queue.directory / relative
            observed: dict[str, Any] | None = None
            if path.exists() or path.is_symlink():
                observed = load_private_json(queue.directory, relative, maximum_bytes=2_000_000)
                if (observed.get("job_id") != job["job_id"]
                        or observed.get("context_digest") != job["context_digest"]
                        or observed.get("session_id") != job["session_id"]
                        or observed.get("turn_completed") is not True
                        or (job["result"] is not None and observed.get("analysis") != job["result"])):
                    raise ReviewQueueError("Supervisor observation does not match its saved attempt.")
                validate_observation({key: value for key, value in observed.items()
                                      if key not in {"job_id", "context_digest"}}, job)
            display_result = job["result"] if job["result"] is not None else observed["analysis"] if observed else None
            records.append({
                "job_id": job["job_id"], "context_digest": job["context_digest"],
                "status": job["status"], "current_context": job["context_digest"] == digest,
                "created_at": job["created_at"], "finished_at": job["finished_at"],
                "result": job["result"],
                # Preserve the journal's non-acceptance after revocation/crash.
                # The checked received answer is historical display data only.
                "observed_result": display_result if observed and job["result"] is None else None,
                "failure": job["failure"], "retry_of": job.get("retry_of"),
                "retry_available": job["job_id"] == retry,
                "provider_completion_observed": observed is not None,
                "provenance": {key: observed.get(key) for key in
                               ("provider", "model", "reasoning_effort", "profile", "session_id", "usage")}
                               if observed else None,
                "messages": _message_receipts(job, display_result, current=(job["context_digest"] == digest
                              and job["result"] is not None and job["status"] == "completed"))
                             if observed and display_result else [],
            })
        return {"schema_version": 1, "request_id": request["request_id"], "status": "ok",
                "mode": "supervisor_status", "repository": request["repository"],
                "context_digest": digest, "jobs": records,
                "configured_launch": configured_launch_summary(queue, request["repository"]),
                "capabilities": {"github_mutations": False, "model_calls": False}}
