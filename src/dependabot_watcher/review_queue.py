from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .advisory import advisory_action_policy
from .config import ConfigError, SupervisorValidationConfig, WatcherConfig, load_config, normalize_repository_name
from .planning import PlanningError, validate_plan_identities
from .runtime_state import (
    RuntimeStateError,
    load_private_json,
    load_run_state,
    run_lock,
    save_private_json,
)


_QUEUE_PATH = Path("review-queue.json")
_MAX_BYTES = 5_000_000
_MAX_PROPOSALS = 1000
_MAX_HISTORY = 100
_MAX_RECEIPTS = 100
_MAX_FEEDBACK = 20
_PLAN_ID = re.compile(r"plan_[a-f0-9]{16}")
_STEP_ID = re.compile(r"step_[a-f0-9]{16}")
_PROPOSAL_ID = re.compile(r"proposal_[a-f0-9]{16}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DECISION_ID = re.compile(r"decision_([1-9][0-9]{0,18})_[A-Za-z0-9_-]{1,32}")
_CHOICES = {"pending", "rejected", "deferred"}


class ReviewQueueError(RuntimeError):
    """A local queue request cannot safely be applied."""


class ReviewConfigurationChanged(ReviewQueueError):
    """The saved policy is changed, unavailable or no longer trusted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def supervisor_context_digest(value: object) -> str:
    """One canonical revision shared by queue snapshots and supervisor jobs."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_queue() -> dict[str, Any]:
    return {"schema_version": 3, "proposals": {}, "history": [], "receipts": [], "next_request_sequence": 1,
            "workspace_evidence": {}}


def _bounded_host_path(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 4096
            and not any(ord(char) < 32 for char in value)
            and Path(value).is_absolute() and ".." not in Path(value).parts)


def _validate_workspace_evidence(value: object) -> None:
    fields = {"repository", "directory", "git_directory", "common_directory", "head_sha", "instruction_fingerprints"}
    if (not isinstance(value, dict) or set(value) != fields
            or not isinstance(value["repository"], str) or not 0 < len(value["repository"]) <= 255
            or any(not _bounded_host_path(value[key]) for key in ("directory", "git_directory", "common_directory"))
            or value["git_directory"] == value["common_directory"]
            or not isinstance(value["head_sha"], str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", value["head_sha"])
            or not isinstance(value["instruction_fingerprints"], list) or len(value["instruction_fingerprints"]) > 32):
        raise ReviewQueueError("Supervisor workspace evidence has an invalid bounded schema.")
    try:
        if normalize_repository_name(value["repository"]) != value["repository"]:
            raise ConfigError("Whitespace is not a repository identity.")
    except ConfigError as exc:
        raise ReviewQueueError("Supervisor workspace evidence has an invalid repository identity.") from exc
    seen: set[str] = set()
    for pair in value["instruction_fingerprints"]:
        if (not isinstance(pair, list) or len(pair) != 2 or not _bounded_host_path(pair[0]) or pair[0] in seen
                or not isinstance(pair[1], str) or not re.fullmatch(r"[a-f0-9]{64}", pair[1])):
            raise ReviewQueueError("Supervisor instruction fingerprint is invalid.")
        seen.add(pair[0])


def _workspace_matches_context(
    evidence: Mapping[str, Any], supervisor: object, repository_plan: object, repository: str,
) -> bool:
    """Match a saved receipt to its selected policy and collected base, not live Git."""
    if (not isinstance(supervisor, Mapping) or supervisor.get("mode") != "approval_first"
            or not isinstance(repository_plan, Mapping)
            or not isinstance(supervisor.get("workspaces"), (list, tuple))):
        return False
    registrations = supervisor["workspaces"]
    if len(registrations) != 1 or not isinstance(registrations[0], Mapping):
        return False
    registration = registrations[0]
    snapshot = repository_plan.get("evidence_snapshot")
    base = snapshot.get("base_sha") if isinstance(snapshot, Mapping) else None
    sources = registration.get("instruction_sources")
    return (isinstance(registration.get("repository"), str)
            and registration["repository"].casefold() == evidence["repository"].casefold() == repository.casefold()
            and evidence["directory"] == registration.get("directory")
            and isinstance(sources, (list, tuple))
            and [pair[0] for pair in evidence["instruction_fingerprints"]] == list(sources)
            and isinstance(base, str) and evidence["head_sha"].casefold() == base.casefold())


def _validate_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReviewQueueError("Review queue contains a malformed timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewQueueError("Review queue contains an invalid timestamp.") from exc
    if parsed.utcoffset() is None:
        raise ReviewQueueError("Review queue timestamp has no time zone.")
    return parsed


def _validate_proposal(item: object, proposal_id: object, *, retired: bool = False) -> None:
    fields = {"proposal_id", "repository", "plan_id", "evidence_status", "step",
              "handling", "version", "resume_at", "feedback"}
    if retired:
        fields.add("retired_at")
    if (not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id)
            or not isinstance(item, dict) or set(item) != fields
            or item.get("proposal_id") != proposal_id
            or not isinstance(item["repository"], str) or not item["repository"]
            or not isinstance(item["plan_id"], str) or not _PLAN_ID.fullmatch(item["plan_id"])
            or not isinstance(item["evidence_status"], str) or item["evidence_status"] not in {"complete", "partial"}
            or not isinstance(item["handling"], str) or item["handling"] not in _CHOICES
            or type(item["version"]) is not int or item["version"] < 0
            or not isinstance(item["feedback"], list) or len(item["feedback"]) > _MAX_FEEDBACK
            or not isinstance(item["step"], dict)
            or not isinstance(item["step"].get("step_id"), str)
            or not _STEP_ID.fullmatch(item["step"]["step_id"])):
        raise ReviewQueueError("Review queue contains a malformed proposal.")
    if item["handling"] == "deferred":
        _validate_timestamp(item["resume_at"])
    elif item["resume_at"] is not None:
        raise ReviewQueueError("Non-deferred proposal contains a resume time.")
    step = item["step"]
    identity = step.get("identity")
    if (not isinstance(step.get("kind"), str) or step["kind"] not in {"pull_request", "alert"}
            or not isinstance(step.get("action"), str) or not step["action"]
            or not isinstance(step.get("summary"), str) or not step["summary"]
            or not isinstance(identity, dict) or identity.get("repository") != item["repository"]
            or type(identity.get("number")) is not int or identity["number"] <= 0):
        raise ReviewQueueError("Review queue contains malformed proposal evidence.")
    seen_feedback: set[str] = set()
    for feedback in item["feedback"]:
        if (not isinstance(feedback, dict)
                or set(feedback) != {"request_id", "message", "created_at", "status"}
                or not isinstance(feedback["request_id"], str)
                or not _DECISION_ID.fullmatch(feedback["request_id"])
                or feedback["request_id"] in seen_feedback
                or not isinstance(feedback["message"], str) or not feedback["message"].strip()
                or len(feedback["message"].encode("utf-8")) > 8192
                or feedback["status"] != "saved_not_submitted"):
            raise ReviewQueueError("Review queue contains malformed feedback.")
        _validate_timestamp(feedback["created_at"])
        seen_feedback.add(feedback["request_id"])
    if retired:
        _validate_timestamp(item["retired_at"])


def _load_queue(directory: Path) -> dict[str, Any]:
    # lexists preserves a dangling symlink as an error, not an empty queue.
    if not ((directory / _QUEUE_PATH).exists() or (directory / _QUEUE_PATH).is_symlink()):
        return _empty_queue()
    state = load_private_json(directory, _QUEUE_PATH, maximum_bytes=_MAX_BYTES)
    legacy_fields = {"schema_version", "proposals", "history", "receipts", "next_request_sequence"}
    if type(state.get("schema_version")) is int and state["schema_version"] == 2 and set(state) == legacy_fields:
        state.update(schema_version=3, workspace_evidence={})
    if (set(state) != legacy_fields | {"workspace_evidence"}
            or type(state["schema_version"]) is not int or state["schema_version"] != 3
            or type(state["next_request_sequence"]) is not int
            or not 1 <= state["next_request_sequence"] <= 2**63 - 1):
        raise ReviewQueueError("Review queue has an unsupported schema.")
    workspaces = state["workspace_evidence"]
    if not isinstance(workspaces, dict) or len(workspaces) > 1000:
        raise ReviewQueueError("Supervisor workspace evidence exceeds its scope bound.")
    for key, value in workspaces.items():
        _validate_workspace_evidence(value)
        if not isinstance(key, str) or key != value["repository"].casefold():
            raise ReviewQueueError("Supervisor workspace evidence has mismatched repository ownership.")
    proposals, history, receipts = state["proposals"], state["history"], state["receipts"]
    if (not isinstance(proposals, dict) or len(proposals) > _MAX_PROPOSALS
            or not isinstance(history, list) or len(history) > _MAX_HISTORY
            or not isinstance(receipts, list) or len(receipts) > _MAX_RECEIPTS):
        raise ReviewQueueError("Review queue exceeds its bounded storage contract.")
    for proposal_id, item in proposals.items():
        _validate_proposal(item, proposal_id)
    if any(not isinstance(item, dict) for item in history + receipts):
        raise ReviewQueueError("Review queue contains malformed history.")
    for item in history:
        _validate_proposal(item, item.get("proposal_id"), retired=True)
    seen_receipts: set[str] = set()
    seen_sequences: set[int] = set()
    for receipt in receipts:
        if (set(receipt) != {"request_id", "digest"}
                or not isinstance(receipt["request_id"], str)
                or not _DECISION_ID.fullmatch(receipt["request_id"])
                or receipt["request_id"] in seen_receipts
                or not isinstance(receipt["digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", receipt["digest"])):
            raise ReviewQueueError("Review queue contains a malformed request receipt.")
        sequence = int(receipt["request_id"].split("_", 2)[1])
        if sequence >= state["next_request_sequence"] or sequence in seen_sequences:
            raise ReviewQueueError("Review queue contains conflicting request sequences.")
        seen_receipts.add(receipt["request_id"])
        seen_sequences.add(sequence)
    return state


class ReviewQueue:
    """Backend-owned local handling of a reconciler's saved proposals.

    This layer deliberately has no GitHub client, provider or execution path.
    Closing a UI has no state transition. Proposal identity binds material step,
    repository evidence completeness and relevant policy; unrelated repository
    replanning cannot erase local handling.
    """

    def __init__(
        self, config: WatcherConfig, state_directory: Path,
        *, clock: Callable[[], datetime] = _utc_now, config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.directory = state_directory.expanduser().absolute()
        self.clock = clock
        self.config_path = config_path.expanduser().absolute() if config_path is not None else None

    def require_current_configuration(self) -> None:
        """Recheck the trusted on-disk policy without changing a job's snapshot.

        In-memory diagnostic queues have no file binding. Normal launchers must
        supply the path; a changed policy needs a fresh queue/job, never a silent
        reload into an already-authorized attempt.
        """
        if self.config_path is None:
            return
        try:
            current = load_config(self.config_path)
        except ConfigError as exc:
            raise ReviewConfigurationChanged("Review configuration is unavailable or no longer trusted; reload before continuing.") from exc
        if current != self.config:
            raise ReviewConfigurationChanged("Review configuration changed; reload before continuing.")

    def _current_proposals(self) -> tuple[str | None, str, dict[str, Any]]:
        plan = self._current_plan()
        if plan is None:
            return None, "unavailable", {}
        plan_id = plan["plan_id"]
        configured = {repository.name.casefold(): repository for repository in self.config.repositories}
        proposals: dict[str, Any] = {}
        for repository in plan["repositories"]:
            name = repository["repository"]
            if name.casefold() not in configured:
                continue
            if (not isinstance(repository.get("status"), str)
                    or repository["status"] not in {"complete", "partial"}):
                raise ReviewQueueError("Latest plan has an invalid repository status.")
            for step in repository["recommended_order"]:
                step_id = step["step_id"]
                if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
                    raise ReviewQueueError("Latest plan has invalid step identities.")
                revision = {
                    "step_id": step_id, "evidence_status": repository["status"],
                    "unresolved_evidence": repository.get("unresolved_evidence"),
                    "planned_workflow": repository.get("workflow_profile"),
                    "planned_action_policy": repository.get("action_policy"),
                    "configured_workflow": asdict(configured[name.casefold()].workflow),
                    "configured_advisory_policy": advisory_action_policy(self.config),
                }
                digest = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
                proposal_id = f"proposal_{digest[:16]}"
                if proposal_id in proposals:
                    raise ReviewQueueError("Latest plan has duplicate or invalid proposal identities.")
                proposals[proposal_id] = {
                    "proposal_id": proposal_id, "repository": name, "plan_id": plan_id,
                    "evidence_status": repository["status"], "step": copy.deepcopy(step),
                    "handling": "pending", "version": 0, "resume_at": None, "feedback": [],
                }
                _validate_proposal(proposals[proposal_id], proposal_id)
        if len(proposals) > _MAX_PROPOSALS:
            raise ReviewQueueError("Review queue exceeds its proposal safety ceiling.")
        return plan_id, str(plan["status"]), proposals

    def _current_plan(self) -> dict[str, Any] | None:
        """Load only the reconciler-owned current plan, never a caller's path."""
        latest = load_run_state(self.directory)["latest_plan"]
        if not latest or latest.get("plan_id") is None:
            return None
        plan_id = latest.get("plan_id")
        if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
            raise ReviewQueueError("Latest plan identity is invalid.")
        # Never follow an arbitrary path from a stored record or a UI payload.
        plan = load_private_json(self.directory, Path("plans") / f"{plan_id}.json")
        if (type(plan.get("schema_version")) is not int or plan["schema_version"] != 1
                or plan.get("mode") != "plan"
                or plan.get("plan_id") != plan_id
                or not isinstance(plan.get("status"), str) or plan["status"] not in {"complete", "partial"}
                or plan.get("mutation_authorized") is not False):
            raise ReviewQueueError("Latest plan has an invalid review contract.")
        try:
            validate_plan_identities(plan)
        except PlanningError as exc:
            raise ReviewQueueError("Latest plan does not match its material identity.") from exc
        names = [item["repository"].casefold() for item in plan["repositories"]]
        if len(names) != len(set(names)):
            raise ReviewQueueError("Latest plan repeats a repository identity.")
        return plan

    def supervision_context(self, repository: str) -> dict[str, Any]:
        """Snapshot one configured repository for reasoning, never action authority."""
        with run_lock(self.directory, wait_seconds=2):
            return self._supervision_context_under_lock(repository)

    def record_workspace_evidence(self, evidence: Mapping[str, Any]) -> None:
        """Trusted launcher preflight only; never exposed as a UI operation.

        The verified workspace becomes part of the same immutable context used
        by jobs, replies and native freshness. An old receipt is not a live check.
        """
        # Normalize tuples to the durable JSON representation before validation.
        value = json.loads(json.dumps(dict(evidence), allow_nan=False))
        _validate_workspace_evidence(value)
        with run_lock(self.directory, wait_seconds=2):
            self.require_current_configuration()
            if self.config.supervisor.mode != "approval_first":
                raise ReviewQueueError("Enable the configured supervisor before registering workspace evidence.")
            state = _load_queue(self.directory)
            self._synchronize(state, self.clock())
            plan = self._current_plan()
            key = value["repository"].casefold()
            state["workspace_evidence"][key] = value
            context = self._supervision_context_from_state(value["repository"], state, plan)
            if context["workspace_evidence"] != value:
                raise ReviewQueueError("Verified workspace no longer matches the configured current repository evidence.")
            save_private_json(self.directory, _QUEUE_PATH, state, maximum_bytes=_MAX_BYTES)

    def _supervision_context_under_lock(self, repository: str, *, persist: bool = True) -> dict[str, Any]:
        """Share the operation lock with a job journal without nested acquisition."""
        self.require_current_configuration()
        if not any(item.name.casefold() == repository.casefold() for item in self.config.repositories):
            raise ReviewQueueError("Supervisor repository is outside the configured scope.")
        now = self.clock()
        if now.utcoffset() is None:
            raise ReviewQueueError("Queue clock must include a time zone.")
        state = _load_queue(self.directory)
        self._synchronize(state, now)
        if persist:
            save_private_json(self.directory, _QUEUE_PATH, state, maximum_bytes=_MAX_BYTES)
        plan = self._current_plan()
        return self._supervision_context_from_state(repository, state, plan)

    def _supervision_context_from_state(
        self, repository: str, state: Mapping[str, Any], plan: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Build from one already-synchronized frame; no I/O or nested locks."""
        key = repository.casefold()
        configured = next((item for item in self.config.repositories if item.name.casefold() == key), None)
        if configured is None:
            raise ReviewQueueError("Supervisor repository is outside the configured scope.")
        repository_plan = next((item for item in plan["repositories"]
                                if item["repository"].casefold() == key), None) if plan else None
        # Keep the evidence's exact spelling/identities. Only scope lookup is
        # case-insensitive; rewriting a saved plan would invalidate its hashes.
        repository = repository_plan["repository"] if repository_plan else configured.name
        # Whole-plan identity changes in another repo must not invalidate this
        # repo's job. Keep the repository plan identity and material evidence.
        proposals = [{key: copy.deepcopy(value) for key, value in item.items() if key != "plan_id"}
                     for item in state["proposals"].values() if item["repository"].casefold() == key]
        retired = [{key: copy.deepcopy(value) for key, value in item.items() if key != "plan_id"}
                   for item in state["history"] if item["repository"].casefold() == key]
        supervisor = asdict(self.config.supervisor)
        # New disabled preparation must not invalidate prior jobs or sessions.
        if self.config.supervisor.validation == SupervisorValidationConfig():
            supervisor.pop("validation")
        supervisor["workspaces"] = [asdict(workspace) for workspace in self.config.supervisor.workspaces
                                    if workspace.repository.casefold() == key]
        # Preserve existing context/session identities when the new optional
        # permission is disabled; enabling it must still invalidate authority.
        for registration in supervisor["workspaces"]:
            if not registration["refresh_authorized"]:
                registration.pop("refresh_authorized")
        workspace = state["workspace_evidence"].get(key)
        if workspace is not None and not _workspace_matches_context(workspace, supervisor, repository_plan, repository):
            workspace = None
        context = {
            "schema_version": 1, "repository": repository,
            "repository_config": asdict(configured),
            "path_policy": asdict(self.config.path_policy),
            "operation": asdict(self.config.operation),
            "advisory": asdict(self.config.advisory),
            "supervisor": supervisor,
            "workspace_evidence": copy.deepcopy(workspace),
            "collection": asdict(self.config.collection),
            "repository_plan": copy.deepcopy(repository_plan),
            "evidence_status": repository_plan["status"] if repository_plan else "unavailable",
            "proposals": proposals, "retired_proposals": retired,
            "mutation_authorized": False,
        }
        # The refreshed collector's bounded execution observations explain why
        # an earlier PR disappeared. Absence from the queue alone is not proof
        # of a successful merge, nor is an old action new execution authority.
        attempt = load_run_state(self.directory).get("last_attempt") or {}
        execution = attempt.get("execution_observation") or {}
        if not isinstance(execution, dict):
            raise ReviewQueueError("Maintenance action observations are invalid.")
        observations = execution.get("observations", [])
        if not isinstance(observations, list) or len(observations) > 20:
            raise ReviewQueueError("Maintenance action observations are invalid.")
        actions = []
        for observation in observations:
            if not isinstance(observation, dict):
                raise ReviewQueueError("Maintenance action observation is invalid.")
            if str(observation.get("repository", "")).casefold() != key:
                continue
            actions.append({name: copy.deepcopy(observation.get(name)) for name in
                            ("action_id", "repository", "pull_request", "head_sha", "status", "recovery_required")})
        if actions:
            context["recent_actions"] = actions
            # Runtime import breaks the queue/journal validation dependency;
            # the projection contains no nested context or executable authority.
            from .supervision import previous_assessment
            prior = previous_assessment(self, context)
            if prior is not None:
                context["previous_assessment"] = prior
        return context

    def current_selection(self, proposal_id: str, version: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve an exact pending selection for a backend-owned action.

        This is not an approval. The executor must still perform its own live
        eligibility and authority checks. Remote collection must happen after
        this lock is released, because execution uses the same operation lock.
        """
        if (not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id)
                or type(version) is not int or version < 0):
            raise ReviewQueueError("Selection requires a proposal identity and local version.")
        now = self.clock()
        if now.utcoffset() is None:
            raise ReviewQueueError("Queue clock must include a time zone.")
        with run_lock(self.directory):
            return self._selection_under_lock(proposal_id, version, now)

    def _selection_under_lock(
        self, proposal_id: str, version: int, now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve under an already-owned run_lock; never acquire it recursively.

        The exact-action controller also uses this inside the executor's
        operation lock to consume consent atomically with local selection.
        """
        state = _load_queue(self.directory)
        self._synchronize(state, now)
        save_private_json(self.directory, _QUEUE_PATH, state, maximum_bytes=_MAX_BYTES)
        item = state["proposals"].get(proposal_id)
        if item is None:
            raise ReviewQueueError("Proposal is absent or superseded; refresh the queue.")
        if item["version"] != version:
            raise ReviewQueueError("Proposal handling changed; refresh before deciding.")
        if item["handling"] != "pending":
            raise ReviewQueueError("Undo rejection or deferral before preparing an action.")
        plan = self._current_plan()
        if plan is None or plan["status"] != "complete" or item["evidence_status"] != "complete":
            raise ReviewQueueError("Action preparation requires complete current evidence.")
        return copy.deepcopy(plan), copy.deepcopy(item)

    def _synchronize(self, state: dict[str, Any], now: datetime) -> tuple[str | None, str]:
        configured = {item.name.casefold() for item in self.config.repositories}
        state["workspace_evidence"] = {key: value for key, value in state["workspace_evidence"].items() if key in configured}
        plan_id, status, current = self._current_proposals()
        old = state["proposals"]
        for proposal_id, item in old.items():
            if proposal_id not in current:
                state["history"].append({**item, "retired_at": _timestamp(now)})
        state["history"] = state["history"][-_MAX_HISTORY:]
        for proposal_id, item in current.items():
            if proposal_id in old:
                previous = old[proposal_id]
                for field in ("handling", "version", "resume_at", "feedback"):
                    item[field] = previous[field]
            if (item["handling"] == "deferred"
                    and datetime.fromisoformat(item["resume_at"].replace("Z", "+00:00")) <= now):
                item.update(handling="pending", resume_at=None, version=item["version"] + 1)
        state["proposals"] = current
        return plan_id, status

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one versioned local request under the existing operation lock."""
        self.require_current_configuration()
        request_id = request.get("request_id")
        operation = request.get("operation")
        if (type(request.get("schema_version")) is not int or request["schema_version"] != 1
                or not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)):
            raise ReviewQueueError("Request requires schema version 1 and a bounded request ID.")
        if not isinstance(operation, str) or operation not in {"snapshot", "reject", "defer", "resume", "feedback"}:
            raise ReviewQueueError("Unsupported review operation; no action was performed.")
        sequence_match = _DECISION_ID.fullmatch(request_id)
        if operation == "snapshot":
            if not request_id.startswith("read_"):
                raise ReviewQueueError("Snapshot request IDs must use the read namespace.")
        elif sequence_match is None:
            raise ReviewQueueError("Decision request ID must include the queue's next request sequence.")
        allowed = {"schema_version", "request_id", "operation"}
        if operation != "snapshot":
            allowed |= {"proposal_id", "version"}
        if operation == "defer":
            allowed.add("seconds")
            if type(request.get("seconds")) is not int or not 1 <= request["seconds"] <= 30 * 86400:
                raise ReviewQueueError("Deferral must be between one second and thirty days.")
        if operation == "feedback":
            allowed.add("message")
            message = request.get("message")
            if (not isinstance(message, str) or not message.strip()
                    or len(message.encode("utf-8")) > 8192):
                raise ReviewQueueError("Feedback must be nonempty and at most 8192 UTF-8 bytes.")
        if set(request) != allowed:
            raise ReviewQueueError("Request contains missing or unsupported fields.")
        digest = hashlib.sha256(json.dumps(dict(request), sort_keys=True).encode()).hexdigest()
        now = self.clock()
        if now.utcoffset() is None:
            raise ReviewQueueError("Queue clock must include a time zone.")
        # Local queue reads/handling share short journal transactions with the
        # supervisor. Bounded contention is not an action or scheduler retry.
        with run_lock(self.directory, wait_seconds=2):
            self.require_current_configuration()
            state = _load_queue(self.directory)
            plan_id, evidence_status = self._synchronize(state, now)
            replayed = False
            receipt = next((item for item in state["receipts"] if item.get("request_id") == request_id), None)
            if receipt is not None:
                if receipt.get("digest") != digest:
                    raise ReviewQueueError("Request ID was already used for a different operation.")
                replayed = True
            elif operation != "snapshot":
                sequence = int(request_id.split("_", 2)[1])
                # The durable monotonic counter rejects evicted/reused IDs
                # without retaining an unbounded tombstone for every request.
                if sequence != state["next_request_sequence"]:
                    raise ReviewQueueError("Request sequence is retired or stale; refresh the queue.")
                if sequence >= 2**63 - 1:
                    raise ReviewQueueError("Queue request sequence reached its safety ceiling.")
                proposal_id = request.get("proposal_id")
                if not isinstance(proposal_id, str) or proposal_id not in state["proposals"]:
                    raise ReviewQueueError("Proposal is absent or superseded; refresh the queue.")
                item = state["proposals"][proposal_id]
                if type(request.get("version")) is not int or request["version"] != item["version"]:
                    raise ReviewQueueError("Proposal handling changed; refresh before deciding.")
                if operation == "feedback":
                    if len(item["feedback"]) >= _MAX_FEEDBACK:
                        raise ReviewQueueError("Proposal feedback is full; existing messages were preserved.")
                    item["feedback"].append({
                        "request_id": request_id, "message": request["message"],
                        "created_at": _timestamp(now), "status": "saved_not_submitted",
                    })
                else:
                    item["handling"] = {"reject": "rejected", "defer": "deferred", "resume": "pending"}[str(operation)]
                    item["resume_at"] = _timestamp(now + timedelta(seconds=request["seconds"])) if operation == "defer" else None
                item["version"] += 1
                state["receipts"].append({"request_id": request_id, "digest": digest})
                state["receipts"] = state["receipts"][-_MAX_RECEIPTS:]
                state["next_request_sequence"] += 1
            # Persistence must succeed before acknowledging a local decision.
            save_private_json(self.directory, _QUEUE_PATH, state, maximum_bytes=_MAX_BYTES)
            plan = self._current_plan()
            contexts = [self._supervision_context_from_state(repository.name, state, plan)
                        for repository in self.config.repositories]
            return {
                "schema_version": 1, "request_id": request_id, "status": "ok", "replayed": replayed,
                "plan_id": plan_id, "evidence_status": evidence_status,
                "next_request_sequence": state["next_request_sequence"],
                "proposals": copy.deepcopy(list(state["proposals"].values())),
                "history": copy.deepcopy(state["history"]),
                "supervisor_context_digests": {
                    context["repository"]: supervisor_context_digest(context)
                    for context in contexts
                },
                "capabilities": {"github_mutations": False, "model_calls": False},
                "feedback_delivery": "saved_only_no_investigator_connected",
            }
