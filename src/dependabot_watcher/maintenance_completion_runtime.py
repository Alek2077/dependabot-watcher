"""Observe and record the selected post-merge loop without replaying actions.

Called through the action controller after module initialization: the action
journal and executor intentionally remain each other's guarded boundaries.
Remote inspection happens outside the shared queue lock; final local state is
rechecked under the same queue-then-checkout order as the integration writer.
"""
from __future__ import annotations

import copy
import re
import secrets
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping

from .config import LocalSyncCheckout, repository_names_equal
from .local_sync import _local_state, _separate_control_paths
from .maintenance_completion import new_obligation, obligation_scope, validate_obligation
from .review_actions import ActionReviews, _load_reviews, _require_current_config, _save_reviews, _time, _timestamp
from .review_queue import ReviewQueueError, supervisor_context_digest
from .runtime_state import RuntimeStateError, ensure_private_directory, load_run_state, run_lock
from .supervision import SupervisorJobs
from .supervisor_status import supervision_status


def evidence_digest(context: Mapping[str, Any]) -> str:
    """Exclude historical display receipts, never current plan/policy/scope."""
    return supervisor_context_digest({key: value for key, value in context.items()
                                      if key not in {"previous_assessment", "local_sync_observations",
                                                     "validation_capability_revision"}})


def _assessment(actions: ActionReviews, context: Mapping[str, Any], *,
                finalized: bool = False) -> tuple[dict[str, str] | None, str | None]:
    status = supervision_status(actions.queue, {"schema_version": 1,
        "request_id": "read_" + secrets.token_hex(16), "operation": "supervisor_status",
        "repository": context["repository"]})
    # Status validates provider observations; journal context supplies exact
    # action-purpose provenance. Neither a model sentence nor latest job alone
    # establishes completion. Recheck accepted state after that validation.
    with run_lock(actions.queue.directory, wait_seconds=2):
        jobs = SupervisorJobs(actions.queue)._load()["jobs"]
        candidates = []
        for item in status["jobs"]:
            job = jobs.get(item["job_id"])
            if (job is not None and item["provider_completion_observed"] is True
                    and item["status"] == job["status"] == "completed"
                    and item["result"] == job["result"] and job["result"] is not None
                    and job["session_id"] is not None
                    and (finalized or job["context"].get("validation_capability_revision")
                         == context.get("validation_capability_revision"))
                    and job["context"].get("maintenance_completion") == context.get("maintenance_completion")
                    and evidence_digest(job["context"]) == evidence_digest(context)):
                candidates.append(job)
        if not candidates:
            return None, None
        job = max(candidates, key=lambda candidate: (_time(candidate["finished_at"]), candidate["job_id"]))
        verdict = job["result"]["maintenance_assessment"]
        if verdict["conclusion"] != "ready_for_completion":
            return None, verdict["explanation"]
        return ({"job_id": job["job_id"], "session_id": job["session_id"],
                 "context_digest": evidence_digest(context)}, None)


def observe_completion(actions: ActionReviews, review_id: str) -> dict[str, Any]:
    """Check one exact saved obligation; never merge, launch AI or integrate Git."""
    if not isinstance(review_id, str) or not re.fullmatch(r"review_[a-f0-9]{32}", review_id):
        raise ReviewQueueError("Maintenance checking requires an exact saved action review.")
    queue = actions.queue
    with run_lock(queue.directory, wait_seconds=2):
        _require_current_config(queue, actions.config_path)
        record = _load_reviews(queue.directory, actions._now())["reviews"].get(review_id)
        if record is None or "completion" not in record:
            raise ReviewQueueError("This action has no recorded maintenance completion obligation.")
        original = copy.deepcopy(record)
        obligation = copy.deepcopy(record["completion"])
        repository = obligation["repository"]
        configured = next((item for item in queue.config.repositories
                           if repository_names_equal(item.name, repository)), None)
        if configured is None or configured.default_branch != obligation["base_branch"]:
            raise ReviewQueueError("Saved maintenance is outside the current repository/branch scope; its obligation was preserved.")
        if obligation_scope(new_obligation(queue.config, record["preparation"])) != obligation_scope(obligation):
            raise ReviewQueueError("Completion policy or selected checkouts changed; the earlier obligation was preserved.")
        if record["status"] != "completed" or obligation["merge_commit_sha"] is None:
            return {"review_id": review_id, "stage": "merge_outcome", "complete": False,
                    "message": "Check the exact saved merge outcome and revision before continuing maintenance.", "targets": []}
        context = queue._supervision_context_under_lock(repository)
        purpose_obligation = copy.deepcopy(obligation)
        purpose_obligation.update(evidence=None, assessment=None, local_results={}, finalization=None)
        if context.get("maintenance_completion") != {"review_id": review_id, "obligation": purpose_obligation}:
            raise ReviewQueueError("A different action owns the current maintenance context; this historical obligation was preserved.")
        attempt = load_run_state(queue.directory).get("last_attempt") or {}
        collected_at = attempt.get("completed_at")

    def pending(stage: str, message: str, targets: list[dict[str, str]] | None = None) -> dict[str, Any]:
        if original["completion"]["finalization"] is not None:
            # A previous success is historical, not permission to ignore new
            # contrary evidence. Revoke its current-completion gate explicitly.
            with run_lock(queue.directory):
                _require_current_config(queue, actions.config_path)
                state = _load_reviews(queue.directory, actions._now())
                if state["reviews"].get(review_id) != original:
                    raise ReviewQueueError("Maintenance changed during inspection; check progress again.")
                stale = state["reviews"][review_id]["completion"]
                stale.update(evidence=None, assessment=None, local_results={}, finalization=None)
                _save_reviews(queue.directory, state, keep=review_id)
        return {"review_id": review_id, "action_id": obligation["action_id"], "repository": repository,
                "stage": stage, "complete": False, "message": message, "targets": targets or []}

    plan = context["repository_plan"]
    if (not isinstance(plan, dict) or plan["status"] != "complete" or not isinstance(collected_at, str)
            or _time(collected_at) < _time(record["consented_at"])):
        return pending("refresh", "Collect fresh PR and alert evidence for the confirmed merge.")
    if any(step.get("kind") == "pull_request"
           and step.get("identity", {}).get("number") == record["preparation"]["binding"]["pull_request"]
           and step.get("identity", {}).get("head_sha") == record["preparation"]["binding"]["head_sha"]
           for step in plan["recommended_order"]):
        return pending("refresh", "Fresh collection still lists the merged head as open; wait for confirmed remote state.")
    base_sha = (plan.get("evidence_snapshot") or {}).get("base_sha")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", base_sha):
        return pending("refresh", "Fresh collection did not establish the exact remote base revision.")
    fetch_head = getattr(actions.client, "fetch_branch_head", None)
    if not callable(fetch_head):
        raise ReviewQueueError("Maintenance checking requires a branch-aware read-only transport.")
    remote = fetch_head(repository, obligation["base_branch"])
    if not isinstance(remote, str) or remote.lower() != base_sha.lower():
        return pending("refresh", "The remote branch advanced after collection; refresh evidence before continuing.")
    base_sha = base_sha.lower()
    if obligation["merge_commit_sha"].lower() != base_sha:
        ancestry = getattr(actions.client, "commit_is_ancestor", None)
        if not callable(ancestry) or ancestry(repository, obligation["merge_commit_sha"], base_sha) is not True:
            return pending("refresh", "The current remote revision is not confirmed to contain this exact merge; investigate before continuing.")
    obligation.update(evidence={"base_sha": base_sha, "context_digest": evidence_digest(context)},
                      assessment=None, local_results={}, finalization=None)
    if obligation["assessment_required"]:
        obligation["assessment"], blocked = _assessment(actions, context,
            finalized=original["completion"]["finalization"] is not None)
        if obligation["assessment"] is None:
            stage, message = ("blocked", blocked) if blocked else (
                "assessment", "The configured agent still needs to assess this exact post-merge state.")
        else:
            stage, message = "local_sync", "Review the selected local updates to finish this maintenance action."
    else:
        stage, message = "local_sync", "Review the selected local updates to finish this maintenance action."
    targets: list[dict[str, str]] = []
    verified_locals: dict[str, dict[str, Any]] = {}
    if not obligation["assessment_required"] or obligation["assessment"] is not None:
        for target in sorted(obligation["targets"], key=lambda item: item["target_id"]):
            observed = {**target, "status": "pending", "message": "Review this selected local update."}
            try:
                local = _local_state(repository, LocalSyncCheckout(target["directory"], target["branch"]))
                _separate_control_paths(local, actions.config_path, queue.directory)
                if local["head_sha"].lower() == base_sha:
                    verified_locals[target["target_id"]] = local
                    obligation["local_results"][target["target_id"]] = {"head_sha": base_sha, "remote_sha": base_sha}
                    observed.update(status="verified", message="Clean selected branch matches the current remote revision.")
            except (ReviewQueueError, RuntimeStateError, OSError):
                observed.update(status="blocked", message="Preserve this checkout; its identity, branch or clean state needs attention.")
            targets.append(observed)
        if len(obligation["local_results"]) == len(obligation["targets"]):
            # Detect remote advancement during local/assessment inspection; an
            # older fast-forward receipt never closes the newer remote loop.
            verified_remote = fetch_head(repository, obligation["base_branch"])
            if not isinstance(verified_remote, str) or verified_remote.lower() != base_sha:
                return pending("refresh", "The remote branch changed during verification; refresh before continuing.", targets)
            obligation["finalization"] = {**obligation["evidence"], "verified_at": _timestamp(actions._now())}
            stage, message = "completed", "Selected maintenance checks are complete. No deployment or production-health claim is made."
    validate_obligation(obligation, record["preparation"])
    # All network work is finished. Use the writer's queue→checkout lock order
    # for the bounded final identity/clean/head recheck and journal commit.
    with run_lock(queue.directory), ExitStack() as checkout_locks:
        _require_current_config(queue, actions.config_path)
        state = _load_reviews(queue.directory, actions._now())
        if state["reviews"].get(review_id) != original:
            raise ReviewQueueError("Saved maintenance progress changed during inspection; check again without replaying an action.")
        current = queue._supervision_context_under_lock(repository)
        if evidence_digest(current) != evidence_digest(context):
            raise ReviewQueueError("Maintenance evidence changed during inspection; refresh before continuing.")
        if obligation["finalization"] is not None:
            for target in sorted(obligation["targets"], key=lambda item: item["target_id"]):
                local = verified_locals[target["target_id"]]
                lock_directory = ensure_private_directory(Path(local["git_directory"]) / "dependabot-watcher-sync")
                checkout_locks.enter_context(run_lock(lock_directory))
                if _local_state(repository, LocalSyncCheckout(target["directory"], target["branch"])) != local:
                    raise ReviewQueueError("Selected checkout changed before completion was recorded; check progress again.")
        state["reviews"][review_id]["completion"] = obligation
        _save_reviews(queue.directory, state, keep=review_id)
    return {"review_id": review_id, "action_id": obligation["action_id"], "repository": repository,
            "stage": stage, "complete": stage == "completed", "message": message, "targets": targets,
            "verification": obligation["finalization"]}
