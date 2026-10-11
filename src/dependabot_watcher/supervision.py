from __future__ import annotations

import copy
import re
import secrets
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .planning import PlanningError, _overall_plan_identifier, validate_plan_identities
from .maintenance_completion import obligation_scope, validate_obligation
from .review_queue import (ReviewQueue, ReviewQueueError, _validate_proposal,
                           _validate_workspace_evidence, _workspace_matches_context)
from .review_queue import supervisor_context_digest as _digest
from .runtime_state import (RuntimeLockBusyError, ensure_private_directory, load_private_json,
                            run_lock, save_private_json, validate_user_owned_directory)


_PATH = Path("supervisor-jobs.json")
_MAX_BYTES = 10_000_000
_MAX_JOBS = 50
_JOB_ID = re.compile(r"job_[a-f0-9]{32}")
_STATUSES = {"prepared", "running", "completed", "superseded", "failed", "interrupted"}


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReviewQueueError("Supervisor record has an invalid timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewQueueError("Supervisor record has an invalid timestamp.") from exc
    if parsed.utcoffset() is None:
        raise ReviewQueueError("Supervisor record timestamp has no time zone.")
    return parsed


def _validate_result(result: object, context: Mapping[str, Any]) -> None:
    """Agent statements remain advice; they cannot supply executable commands."""
    fields = {"summary", "recommendations", "replies"}
    completion = context.get("maintenance_completion")
    if completion is not None:
        fields.add("maintenance_assessment")
    if (not isinstance(result, dict) or set(result) != fields
            or not isinstance(result["summary"], str) or not result["summary"].strip()
            or len(result["summary"].encode()) > 8192
            or not isinstance(result["recommendations"], list) or len(result["recommendations"]) > 1000
            or not isinstance(result["replies"], list) or len(result["replies"]) > 2000):
        raise ReviewQueueError("Supervisor result has an invalid bounded schema.")
    if completion is not None:
        assessment = result["maintenance_assessment"]
        if (not isinstance(assessment, dict) or set(assessment) != {"action_id", "conclusion", "explanation"}
                or assessment["action_id"] != completion["obligation"]["action_id"]
                or not isinstance(assessment["conclusion"], str)
                or assessment["conclusion"] not in {"ready_for_completion", "blocked", "needs_attention"}
                or not isinstance(assessment["explanation"], str) or not assessment["explanation"].strip()
                or len(assessment["explanation"].encode()) > 8192):
            raise ReviewQueueError("Supervisor maintenance assessment does not match its exact action purpose.")
    proposals = {item["proposal_id"]: item for item in context["proposals"]}
    messages = {(item["proposal_id"], message["request_id"])
                for item in context["proposals"] + context["retired_proposals"]
                for message in item["feedback"]}
    seen: set[str] = set()
    for item in result["recommendations"]:
        if (not isinstance(item, dict) or set(item) not in ({"proposal_id", "conclusion", "explanation"},
                {"proposal_id", "conclusion", "explanation", "ready_for_action"})
                or not isinstance(item["proposal_id"], str) or item["proposal_id"] not in proposals
                or item["proposal_id"] in seen
                or not isinstance(item["conclusion"], str)
                or item["conclusion"] not in {"recommend", "investigate", "hold", "no_action"}
                or ("ready_for_action" in item and type(item["ready_for_action"]) is not bool)
                or (item.get("ready_for_action") is True and item["conclusion"] != "recommend")
                or not isinstance(item["explanation"], str) or not item["explanation"].strip()
                or len(item["explanation"].encode()) > 8192):
            raise ReviewQueueError("Supervisor recommendation does not match its saved context.")
        seen.add(item["proposal_id"])
    answered: set[tuple[str, str]] = set()
    for reply in result["replies"]:
        if (not isinstance(reply, dict) or set(reply) != {"proposal_id", "request_id", "message"}
                or not isinstance(reply["proposal_id"], str) or not isinstance(reply["request_id"], str)
                or (reply["proposal_id"], reply["request_id"]) not in messages
                or (reply["proposal_id"], reply["request_id"]) in answered
                or not isinstance(reply["message"], str) or not reply["message"].strip()
                or len(reply["message"].encode()) > 8192):
            raise ReviewQueueError("Supervisor reply does not match its saved message.")
        answered.add((reply["proposal_id"], reply["request_id"]))


def _validate_context(context: Mapping[str, Any]) -> None:
    fields = {"schema_version", "repository", "repository_config", "path_policy", "operation", "advisory", "collection",
              "repository_plan", "evidence_status", "proposals", "retired_proposals", "mutation_authorized"}
    # Preserve existing development journals; new jobs bind the configured
    # supervisor policy as well. Missing legacy policy is never launch consent.
    if (not fields <= set(context) or set(context) - fields - {"supervisor", "workspace_evidence", "recent_actions", "previous_assessment", "local_sync_observations", "maintenance_completion", "validation_capability_revision"}
            or ("supervisor" in context and not isinstance(context["supervisor"], dict))
            or type(context["schema_version"]) is not int or context["schema_version"] != 1
            or not isinstance(context["repository"], str)
            or not isinstance(context["evidence_status"], str)
            or context["evidence_status"] not in {"complete", "partial", "unavailable"}
            or context["mutation_authorized"] is not False
            or any(not isinstance(context[name], dict) for name in
                   ("repository_config", "path_policy", "operation", "advisory", "collection"))
            or not isinstance(context["repository_config"].get("name"), str)
            or context["repository_config"]["name"].casefold() != context["repository"].casefold()
            or not isinstance(context["proposals"], list) or len(context["proposals"]) > 1000
            or not isinstance(context["retired_proposals"], list) or len(context["retired_proposals"]) > 100):
        raise ReviewQueueError("Supervisor context has an invalid bounded schema.")
    if ("validation_capability_revision" in context
            and context["validation_capability_revision"] != "merged_action_v1"):
        raise ReviewQueueError("Supervisor validation capability revision is invalid.")
    completion = context.get("maintenance_completion")
    if completion is not None:
        if (not isinstance(completion, dict) or set(completion) != {"review_id", "obligation"}
                or not isinstance(completion["review_id"], str)
                or not re.fullmatch(r"review_[a-f0-9]{32}", completion["review_id"])
                or not isinstance(completion["obligation"], dict)):
            raise ReviewQueueError("Supervisor completion purpose has an invalid identity.")
        obligation = completion["obligation"]
        if (not isinstance(obligation.get("repository"), str)
                or obligation["repository"].casefold() != context["repository"].casefold()):
            raise ReviewQueueError("Supervisor completion purpose is outside repository scope.")
        validate_obligation(obligation, {"action_id": obligation.get("action_id"),
            "decision": {"completion_scope": obligation_scope(obligation)}, "binding": {
            "repository": obligation["repository"], "expected_base": context["repository_config"].get("default_branch")}})
        if (obligation["evidence"] is not None or obligation["assessment"] is not None or obligation["local_results"]
                or obligation["finalization"] is not None
                or not isinstance(obligation["action_id"], str)
                or not re.fullmatch(r"action_[a-f0-9]{24}", obligation["action_id"])):
            raise ReviewQueueError("Supervisor completion purpose contains mutable progress or invalid action identity.")
    observations = context.get("local_sync_observations", [])
    if not isinstance(observations, list) or len(observations) > 20:
        raise ReviewQueueError("Supervisor local synchronization evidence is invalid.")
    registrations = context["repository_config"].get("local_sync", [])
    for observation in observations:
        if (not isinstance(observation, dict) or set(observation) != {
                "review_id", "target_id", "directory", "branch", "from_sha", "target_sha", "status", "outcome", "applicability"}
                or observation["applicability"] != "historical_recheck_before_use"
                or not isinstance(observation["review_id"], str) or not re.fullmatch(r"sync_[a-f0-9]{32}", observation["review_id"])
                or not isinstance(observation["target_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", observation["target_id"])
                or not isinstance(registrations, (list, tuple))
                or not any(isinstance(item, dict) and item.get("directory") == observation["directory"]
                           and item.get("branch") == observation["branch"] for item in registrations)
                or any(not isinstance(observation[key], str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", observation[key])
                       for key in ("from_sha", "target_sha"))
                or not isinstance(observation["status"], str)
                or observation["status"] not in {"awaiting_confirmation", "executing", "completed", "failed", "recovery_required"}
                or observation["outcome"] not in (None, "synchronized", "observed_applied", "observed_not_applied", "manual_repair_required")):
            raise ReviewQueueError("Supervisor local synchronization observation is malformed or out of scope.")
    actions = context.get("recent_actions", [])
    if not isinstance(actions, list) or len(actions) > 20:
        raise ReviewQueueError("Supervisor maintenance actions exceed their bounded schema.")
    for action in actions:
        if (not isinstance(action, dict) or set(action) != {
                "action_id", "repository", "pull_request", "head_sha", "status", "recovery_required"}
                or not isinstance(action["repository"], str)
                or action["repository"].casefold() != context["repository"].casefold()
                or not isinstance(action["action_id"], str) or not re.fullmatch(r"action_[a-f0-9]{24}", action["action_id"])
                or type(action["pull_request"]) is not int or action["pull_request"] <= 0
                or not isinstance(action["head_sha"], str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", action["head_sha"])
                or not isinstance(action["status"], str)
                or action["status"] not in {"interrupted", "failed", "already_applied", "observation_incomplete",
                    "success_observed", "success_not_reflected", "repository_state_changed", "unknown"}
                or type(action["recovery_required"]) is not bool):
            raise ReviewQueueError("Supervisor maintenance action is invalid or outside repository scope.")
    prior = context.get("previous_assessment")
    if prior is not None:
        if (not isinstance(prior, dict) or set(prior) != {"source_job_id", "source_context_digest", "repository",
                "provider", "model", "reasoning_effort", "applicability", "summary", "recommendations", "recommendations_omitted"}
                or prior["repository"] != context["repository"]
                or prior["applicability"] != "historical_recheck_before_use"
                or not isinstance(prior["source_job_id"], str) or not _JOB_ID.fullmatch(prior["source_job_id"])
                or not isinstance(prior["source_context_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", prior["source_context_digest"])
                or any(not isinstance(prior[name], str) or not prior[name].strip() or len(prior[name].encode()) > limit
                       for name, limit in (("provider", 128), ("model", 128), ("reasoning_effort", 128), ("summary", 8192)))
                or not isinstance(prior["recommendations"], list) or len(prior["recommendations"]) > 20
                or type(prior["recommendations_omitted"]) is not int or not 0 <= prior["recommendations_omitted"] <= 1000):
            raise ReviewQueueError("Historical supervisor assessment is malformed.")
        for recommendation in prior["recommendations"]:
            if (not isinstance(recommendation, dict) or set(recommendation) != {"proposal_id", "item", "conclusion", "explanation"}
                    or not isinstance(recommendation["proposal_id"], str) or not re.fullmatch(r"proposal_[a-f0-9]{16}", recommendation["proposal_id"])
                    or not isinstance(recommendation["item"], dict)
                    or set(recommendation["item"]) != {"repository", "kind", "number", "head_sha"}
                    or recommendation["item"]["repository"] != context["repository"]
                    or not isinstance(recommendation["item"]["kind"], str)
                    or recommendation["item"]["kind"] not in {"pull_request", "alert"}
                    or type(recommendation["item"]["number"]) is not int or recommendation["item"]["number"] <= 0
                    or (recommendation["item"]["kind"] == "pull_request" and (not isinstance(recommendation["item"]["head_sha"], str)
                        or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", recommendation["item"]["head_sha"])))
                    or (recommendation["item"]["kind"] == "alert" and recommendation["item"]["head_sha"] is not None)
                    or not isinstance(recommendation["conclusion"], str) or recommendation["conclusion"] not in {"recommend", "investigate", "hold", "no_action"}
                    or not isinstance(recommendation["explanation"], str) or not recommendation["explanation"].strip()
                    or len(recommendation["explanation"].encode()) > 8192):
                raise ReviewQueueError("Historical supervisor recommendation is malformed.")
    plan = context["repository_plan"]
    if plan is None:
        if context["evidence_status"] != "unavailable" or context["proposals"]:
            raise ReviewQueueError("Supervisor context lacks its evidence plan.")
    elif (not isinstance(plan, dict) or plan.get("repository") != context["repository"]
          or plan.get("status") != context["evidence_status"]
          or not isinstance(plan.get("plan_id"), str)):
        raise ReviewQueueError("Supervisor context has mismatched repository evidence.")
    else:
        try:
            validate_plan_identities({"status": plan["status"], "repositories": [plan],
                                      "plan_id": _overall_plan_identifier(plan["status"], [plan["plan_id"]])})
        except PlanningError as exc:
            raise ReviewQueueError("Supervisor context plan identity is invalid.") from exc
    workspace = context.get("workspace_evidence")
    if workspace is not None:
        _validate_workspace_evidence(workspace)
        if not _workspace_matches_context(workspace, context.get("supervisor"), plan, context["repository"]):
            raise ReviewQueueError("Supervisor context mixes workspace and repository evidence.")
    for retired, items in ((False, context["proposals"]), (True, context["retired_proposals"])):
        seen: set[str] = set()
        for item in items:
            if (not isinstance(item, dict) or "plan_id" in item
                    or not isinstance(item.get("repository"), str)
                    or item["repository"].casefold() != context["repository"].casefold()):
                raise ReviewQueueError("Supervisor context mixes repository proposals.")
            proposal_id = item.get("proposal_id")
            if not isinstance(proposal_id, str):
                raise ReviewQueueError("Supervisor context has an invalid proposal identity.")
            _validate_proposal({**item, "plan_id": "plan_" + "0" * 16}, proposal_id, retired=retired)
            if not retired and proposal_id in seen:
                raise ReviewQueueError("Supervisor context duplicates a proposal.")
            seen.add(proposal_id)
            if not retired and (plan is None or item["step"] not in plan["recommended_order"]):
                raise ReviewQueueError("Supervisor proposal does not match its evidence plan.")


def previous_assessment(queue: ReviewQueue, context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Carry one verified pre-action analysis, never recursive job history.

    Bind selection to an observed successful exact head which was active in
    that prior job. A post-merge completion cannot select itself and churn the
    context digest forever. Changed policy/harness starts without this input.
    """
    from .supervisor_runner import validate_observation
    from .supervisor_workspace import same_supervisor_session_workspace

    successful = [action for action in context.get("recent_actions", [])
                  if action["status"] in {"success_observed", "already_applied"} and not action["recovery_required"]]
    if not successful:
        return None
    action = successful[-1]
    if any(proposal["step"]["kind"] == "pull_request"
           and proposal["step"]["identity"].get("number") == action["pull_request"]
           and proposal["step"]["identity"].get("head_sha") == action["head_sha"] for proposal in context["proposals"]):
        return None
    candidates = []
    for job in SupervisorJobs(queue)._load()["jobs"].values():
        old = job["context"]
        if (job["repository"].casefold() != context["repository"].casefold()
                or job["status"] not in {"completed", "superseded"}
                or _digest(old.get("supervisor")) != _digest(context.get("supervisor"))
                or not same_supervisor_session_workspace(old.get("workspace_evidence"), context.get("workspace_evidence"))):
            continue
        if any(proposal["step"]["kind"] == "pull_request"
               and proposal["step"]["identity"].get("number") == action["pull_request"]
               and proposal["step"]["identity"].get("head_sha") == action["head_sha"] for proposal in old["proposals"]):
            candidates.append(job)
    for job in sorted(candidates, key=lambda value: (_time(value["finished_at"]), value["job_id"]), reverse=True):
        path = Path("supervisor-observations") / f'{job["job_id"]}.json'
        if not ((queue.directory / path).exists() or (queue.directory / path).is_symlink()):
            continue
        observed = load_private_json(queue.directory, path, maximum_bytes=2_000_000)
        if (observed.get("job_id") != job["job_id"] or observed.get("context_digest") != job["context_digest"]
                or observed.get("session_id") != job["session_id"] or observed.get("analysis") != job["result"]):
            raise ReviewQueueError("Historical supervisor observation does not match its saved job.")
        validate_observation({key: value for key, value in observed.items() if key not in {"job_id", "context_digest"}}, job)
        proposals = {proposal["proposal_id"]: proposal for proposal in job["context"]["proposals"]}
        recommendations = [{"proposal_id": rec["proposal_id"], "item": {
                                "repository": context["repository"], "kind": proposals[rec["proposal_id"]]["step"]["kind"],
                                "number": proposals[rec["proposal_id"]]["step"]["identity"]["number"],
                                "head_sha": proposals[rec["proposal_id"]]["step"]["identity"].get("head_sha")},
                            "conclusion": rec["conclusion"], "explanation": rec["explanation"]}
                           for rec in job["result"]["recommendations"] if rec["proposal_id"] in proposals]
        return {"source_job_id": job["job_id"], "source_context_digest": job["context_digest"],
                "repository": context["repository"], "provider": observed["provider"], "model": observed["model"],
                "reasoning_effort": observed["reasoning_effort"], "applicability": "historical_recheck_before_use",
                "summary": job["result"]["summary"], "recommendations": recommendations[:20],
                "recommendations_omitted": max(0, len(recommendations) - 20)}
    return None


class SupervisorJobs:
    """Private immutable context and accountable reasoning attempts.

    Preparing a job is not provider delivery. Starting records an attempt, not
    completed analysis. This journal has no model client or mutation capability;
    the adapter must report its observed session and completion separately.
    """

    def __init__(self, queue: ReviewQueue) -> None:
        self.queue = queue
        self._owners: dict[str, AbstractContextManager[Path]] = {}

    def _owner_directory(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ReviewQueueError("Supervisor owner identity is invalid.")
        return self.queue.directory / "supervisor-owners" / job_id

    def _owner_is_live(self, job_id: str) -> bool:
        """A held OS lease proves backend ownership, not model responsiveness."""
        directory = self._owner_directory(job_id)
        if not directory.parent.exists():
            return False
        validate_user_owned_directory(directory.parent)
        if not directory.exists():
            return False
        validate_user_owned_directory(directory)
        try:
            with run_lock(directory):
                return False
        except RuntimeLockBusyError:
            return True

    def release_owner(self, job_id: str) -> None:
        """Release only this instance's lease, including uncertain failed saves."""
        owner = self._owners.pop(job_id, None)
        if owner is not None:
            owner.__exit__(None, None, None)

    def _recover_under_lock(self, state: dict[str, Any], repository: str) -> None:
        changed = False
        for job in state["jobs"].values():
            if (job["repository"].casefold() == repository.casefold()
                    and job["status"] == "running" and not self._owner_is_live(job["job_id"])):
                job.update(status="interrupted", finished_at=self._now(), failure="interrupted")
                changed = True
        if changed:
            self._save(state)

    def _now(self) -> str:
        value = self.queue.clock()
        if value.utcoffset() is None:
            raise ReviewQueueError("Supervisor clock must include a time zone.")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _load(self) -> dict[str, Any]:
        path = self.queue.directory / _PATH
        if not (path.exists() or path.is_symlink()):
            return {"schema_version": 1, "jobs": {}}
        state = load_private_json(self.queue.directory, _PATH, maximum_bytes=_MAX_BYTES)
        if (set(state) != {"schema_version", "jobs"} or type(state["schema_version"]) is not int
                or state["schema_version"] != 1 or not isinstance(state["jobs"], dict)
                or len(state["jobs"]) > _MAX_JOBS):
            raise ReviewQueueError("Supervisor journal has an invalid bounded schema.")
        fields = {"job_id", "repository", "context", "context_digest", "status", "created_at",
                  "started_at", "finished_at", "session_id", "result", "failure"}
        for key, job in state["jobs"].items():
            if (not isinstance(key, str) or not _JOB_ID.fullmatch(key)
                    or not isinstance(job, dict) or not fields <= set(job) or set(job) - fields - {"retry_of"}
                    or job["job_id"] != key
                    or not isinstance(job["status"], str) or job["status"] not in _STATUSES
                    or not isinstance(job["repository"], str)
                    or not isinstance(job["context"], dict)
                    or job["context"].get("repository") != job["repository"]
                    or job["context"].get("mutation_authorized") is not False
                    or job["context_digest"] != _digest(job["context"])
                    or (job["session_id"] is not None and (not isinstance(job["session_id"], str)
                        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", job["session_id"])) )
                    or (job["failure"] is not None and (not isinstance(job["failure"], str) or job["failure"] not in
                        {"provider_failed", "interrupted", "context_changed"}))):
                raise ReviewQueueError("Supervisor journal contains a malformed job.")
            _validate_context(job["context"])
            created = _time(job["created_at"])
            if job["started_at"] is not None and _time(job["started_at"]) < created:
                raise ReviewQueueError("Supervisor start precedes job creation.")
            if job["finished_at"] is not None and _time(job["finished_at"]) < (
                    _time(job["started_at"]) if job["started_at"] is not None else created):
                raise ReviewQueueError("Supervisor finish precedes job creation.")
            if job["status"] == "prepared":
                if any(job[name] is not None for name in ("started_at", "finished_at", "session_id", "result", "failure")):
                    raise ReviewQueueError("Prepared supervisor job contains execution evidence.")
            elif job["started_at"] is None:
                raise ReviewQueueError("Supervisor outcome lacks a started attempt.")
            if job["status"] == "running" and any(job[name] is not None for name in ("finished_at", "result", "failure")):
                raise ReviewQueueError("Running supervisor job contains a terminal outcome.")
            if job["status"] in {"completed", "superseded", "failed", "interrupted"} and job["finished_at"] is None:
                raise ReviewQueueError("Terminal supervisor job lacks a finish time.")
            if job["result"] is not None:
                _validate_result(job["result"], job["context"])
            if job["status"] == "completed" and (job["result"] is None or job["failure"] is not None):
                raise ReviewQueueError("Completed supervisor job lacks valid analysis.")
            if job["status"] == "superseded" and (job["result"] is None or job["failure"] != "context_changed"):
                raise ReviewQueueError("Superseded supervisor job lacks its historical analysis.")
            if job["status"] in {"failed", "interrupted"} and (job["result"] is not None or job["failure"] is None):
                raise ReviewQueueError("Failed supervisor job has an inconsistent outcome.")
        retried: set[str] = set()
        for job in state["jobs"].values():
            if "retry_of" not in job:
                continue
            parent_id = job["retry_of"]
            parent = state["jobs"].get(parent_id) if isinstance(parent_id, str) else None
            if (parent is None or parent_id == job["job_id"] or parent_id in retried
                    or parent["status"] not in {"failed", "interrupted"}
                    or parent["repository"].casefold() != job["repository"].casefold()
                    or parent["context_digest"] != job["context_digest"]
                    or _time(job["created_at"]) < _time(parent["finished_at"])):
                raise ReviewQueueError("Supervisor retry does not match one failed historical attempt.")
            retried.add(parent_id)
            seen = {job["job_id"]}
            while parent is not None:
                if parent["job_id"] in seen:
                    raise ReviewQueueError("Supervisor retry history contains a cycle.")
                seen.add(parent["job_id"])
                parent = state["jobs"].get(parent.get("retry_of"))
        session_owners: dict[str, str] = {}
        for job in state["jobs"].values():
            session = job["session_id"]
            if session is not None:
                if session in session_owners and session_owners[session].casefold() != job["repository"].casefold():
                    raise ReviewQueueError("Supervisor journal mixes repository session ownership.")
                session_owners[session] = job["repository"]
        return state

    def _save(self, state: dict[str, Any]) -> None:
        save_private_json(self.queue.directory, _PATH, state, maximum_bytes=_MAX_BYTES)

    def _job(self, state: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id) or job_id not in state["jobs"]:
            raise ReviewQueueError("Supervisor job is unavailable.")
        return state["jobs"][job_id]

    @staticmethod
    def retry_candidate(state: Mapping[str, Any], repository: str, digest: str) -> str | None:
        """Only an uncovered failed attempt in this exact current context."""
        current = [job for job in state["jobs"].values()
                   if job["repository"].casefold() == repository.casefold() and job["context_digest"] == digest]
        if any(job["status"] in {"prepared", "running", "completed"} for job in current):
            return None
        parents = {job.get("retry_of") for job in current}
        failed = [job for job in current if job["status"] in {"failed", "interrupted"} and job["job_id"] not in parents]
        return max(failed, key=lambda job: (_time(job["finished_at"]), job["job_id"]))["job_id"] if failed else None

    def prepare(self, repository: str, *, retry_job_id: str | None = None) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            context = self.queue._supervision_context_under_lock(repository)
            _validate_context(context)
            repository = context["repository"]
            if context["evidence_status"] == "unavailable":
                raise ReviewQueueError("Collect repository evidence before preparing a supervisor job.")
            state = self._load()
            self._recover_under_lock(state, repository)
            digest = _digest(context)
            retry = self.retry_candidate(state, repository, digest)
            if retry_job_id is not None:
                if retry_job_id != retry:
                    raise ReviewQueueError("Retry no longer matches the current failed investigation; inspect saved status.")
            elif retry is not None:
                raise ReviewQueueError("This investigation needs deliberate recovery before retrying its failed attempt.")
            existing = next((job for job in state["jobs"].values()
                             if job["repository"].casefold() == repository.casefold() and job["context_digest"] == digest
                             and job["status"] in {"prepared", "running", "completed"}), None)
            if existing is not None:
                return copy.deepcopy(existing)
            if len(state["jobs"]) >= _MAX_JOBS:
                raise ReviewQueueError("Supervisor history is full; archive reviewed jobs before continuing.")
            job_id = f"job_{secrets.token_hex(16)}"
            job = {"job_id": job_id, "repository": repository, "context": context,
                   "context_digest": digest, "status": "prepared", "created_at": self._now(),
                   "started_at": None, "finished_at": None, "session_id": None,
                   "result": None, "failure": None}
            if retry_job_id is not None:
                job["retry_of"] = retry_job_id
            state["jobs"][job_id] = job
            self._save(state)
            return copy.deepcopy(job)

    def start(self, job_id: str, *, resume_session_id: str | None = None) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            job = self._job(state, job_id)
            self._recover_under_lock(state, job["repository"])
            if job["status"] != "prepared":
                raise ReviewQueueError("Supervisor job was already started; inspect its recorded outcome.")
            context = self.queue._supervision_context_under_lock(job["repository"])
            if _digest(context) != job["context_digest"]:
                raise ReviewQueueError("Supervisor context changed; prepare a fresh job.")
            if any(other["repository"].casefold() == job["repository"].casefold() and other["status"] == "running"
                   for other in state["jobs"].values()):
                raise ReviewQueueError("A supervisor is already running for this repository.")
            if resume_session_id is not None:
                if not isinstance(resume_session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", resume_session_id):
                    raise ReviewQueueError("Supervisor resume identity is invalid.")
                owners = [other for other in state["jobs"].values() if other["session_id"] == resume_session_id]
                if (not owners or any(other["repository"].casefold() != job["repository"].casefold() for other in owners)
                        or not any(other["status"] in {"completed", "superseded"} for other in owners)):
                    raise ReviewQueueError("Supervisor resume lacks a completed owner in this repository.")
            # Acquire before publishing running, including the handshake and
            # legacy provider path. A crash cannot strand an unowned startup.
            directory = self._owner_directory(job_id)
            ensure_private_directory(directory.parent)
            owner = run_lock(directory)
            owner.__enter__()
            try:
                job.update(status="running", started_at=self._now(), session_id=resume_session_id)
                self._save(state)
            except BaseException:
                owner.__exit__(None, None, None)
                raise
            self._owners[job_id] = owner
            return copy.deepcopy(job)

    def attach_session(self, job_id: str, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session_id):
            raise ReviewQueueError("Supervisor session identity is invalid.")
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            job = self._job(state, job_id)
            if job["status"] != "running" or job["session_id"] not in {None, session_id}:
                raise ReviewQueueError("Supervisor session does not match the running attempt.")
            others = [other for other in state["jobs"].values()
                      if other["job_id"] != job_id and other["session_id"] == session_id]
            if (any(other["repository"].casefold() != job["repository"].casefold() for other in others)
                    or (others and job["session_id"] is None)):
                raise ReviewQueueError("Supervisor session is already owned; start an explicit scoped continuation.")
            job["session_id"] = session_id
            self._save(state)
            return copy.deepcopy(job)

    def complete(self, job_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            job = self._job(state, job_id)
            _validate_result(result, job["context"])
            if job["status"] in {"completed", "superseded"} and job["result"] == result:
                return copy.deepcopy(job)
            if job["status"] != "running":
                raise ReviewQueueError("Supervisor result does not belong to a running attempt.")
            current = self.queue._supervision_context_under_lock(job["repository"])
            stale = _digest(current) != job["context_digest"]
            job.update(status="superseded" if stale else "completed", finished_at=self._now(),
                       result=copy.deepcopy(dict(result)), failure="context_changed" if stale else None)
            self._save(state)
            self.release_owner(job_id)
            return copy.deepcopy(job)

    def fail(self, job_id: str, *, interrupted: bool = False) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            return self._fail_under_lock(job_id, interrupted=interrupted)

    def _fail_under_lock(self, job_id: str, *, interrupted: bool = False) -> dict[str, Any]:
        """Shared journal transaction for owner-loss recovery; no nested lock."""
        state = self._load()
        job = self._job(state, job_id)
        if job["status"] != "running":
            raise ReviewQueueError("Only a running supervisor attempt can fail.")
        job.update(status="interrupted" if interrupted else "failed", finished_at=self._now(),
                   failure="interrupted" if interrupted else "provider_failed")
        self._save(state)
        self.release_owner(job_id)
        return copy.deepcopy(job)

    def read(self, job_id: str) -> dict[str, Any]:
        with run_lock(self.queue.directory, wait_seconds=2):
            state = self._load()
            job = self._job(state, job_id)
            self._recover_under_lock(state, job["repository"])
            return copy.deepcopy(job)
