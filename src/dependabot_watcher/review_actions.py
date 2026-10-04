from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from .config import ConfigError, load_config, repository_names_equal
from .execution import approval_token, exact_action_audit, execute_approved, preflight_execution
from .github import ExecutionGitHubClient, GitHubClient
from .reconciliation import reconcile
from .review_queue import ReviewQueue, ReviewQueueError
from .runtime_state import RuntimeStateError, load_private_json, run_lock, save_private_json


_REVIEWS_PATH = Path("review-actions.json")
_MAX_REVIEWS = 100
_MAX_BYTES = 5_000_000
_REVIEW_ID = re.compile(r"review_[a-f0-9]{32}")
_STATUSES = {"awaiting_confirmation", "executing", "completed", "invalidated", "expired", "recovery_required"}


def _validate_preparation(prepared: Mapping[str, Any]) -> None:
    fields = {"schema_version", "proposal_id", "version", "plan_id", "status", "binding",
              "decision", "action_id", "mutation_performed", "mutation_authorized", "capabilities"}
    binding = prepared.get("binding")
    capabilities = prepared.get("capabilities")
    if (set(prepared) != fields or type(prepared["schema_version"]) is not int
            or prepared["schema_version"] != 1
            or not isinstance(prepared["proposal_id"], str)
            or not re.fullmatch(r"proposal_[a-f0-9]{16}", prepared["proposal_id"])
            or type(prepared["version"]) is not int or prepared["version"] < 0
            or not isinstance(prepared["plan_id"], str)
            or not re.fullmatch(r"plan_[a-f0-9]{16}", prepared["plan_id"])
            or prepared["status"] != "ready"
            or prepared["mutation_performed"] is not False
            or prepared["mutation_authorized"] is not False
            or not isinstance(capabilities, dict)
            or set(capabilities) != {"github_mutations", "model_calls"}
            or capabilities["github_mutations"] is not False or capabilities["model_calls"] is not False
            or not isinstance(prepared["decision"], dict)
            or not isinstance(prepared["action_id"], str)
            or not re.fullmatch(r"action_[a-f0-9]{24}", prepared["action_id"])
            or not isinstance(binding, dict)
            or set(binding) != {"repository_plan_id", "step_id", "repository", "pull_request",
                                "expected_base", "head_sha", "merge_method"}):
        raise ReviewQueueError("Action preparation contains an invalid confirmation contract.")
    for key, pattern in (("repository_plan_id", r"plan_[a-f0-9]{16}"),
                         ("step_id", r"step_[a-f0-9]{16}"),
                         ("head_sha", r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}")):
        if not isinstance(binding[key], str) or not re.fullmatch(pattern, binding[key]):
            raise ReviewQueueError("Action binding contains an invalid identity.")
    if (type(binding["pull_request"]) is not int or binding["pull_request"] <= 0
            or not isinstance(binding["repository"], str) or not binding["repository"]
            or not isinstance(binding["expected_base"], str) or not binding["expected_base"]
            or not isinstance(binding["merge_method"], str)
            or binding["merge_method"] not in {"merge", "squash", "rebase"}):
        raise ReviewQueueError("Action binding contains an invalid target.")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReviewQueueError("Action record has an invalid timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewQueueError("Action record has an invalid timestamp.") from exc
    if parsed.utcoffset() is None:
        raise ReviewQueueError("Action record timestamp has no time zone.")
    return parsed


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_reviews(directory: Path, now: datetime) -> dict[str, Any]:
    path = directory / _REVIEWS_PATH
    if not (path.exists() or path.is_symlink()):
        return {"schema_version": 1, "reviews": {}}
    state = load_private_json(directory, _REVIEWS_PATH, maximum_bytes=_MAX_BYTES)
    if (set(state) != {"schema_version", "reviews"} or type(state["schema_version"]) is not int
            or state["schema_version"] != 1 or not isinstance(state["reviews"], dict)
            or len(state["reviews"]) > _MAX_REVIEWS):
        raise ReviewQueueError("Action history has an invalid bounded schema.")
    for key, record in state["reviews"].items():
        if (not isinstance(key, str) or not _REVIEW_ID.fullmatch(key)
                or not isinstance(record, dict)
                or set(record) != {"review_id", "preparation", "preparation_digest", "selection_digest", "config_digest",
                                   "created_at", "expires_at", "status", "consented_at", "result"}
                or record["review_id"] != key or not isinstance(record["status"], str)
                or record["status"] not in _STATUSES
                or not isinstance(record["preparation"], dict)
                or record["preparation"].get("status") != "ready"
                or record["preparation_digest"] != _digest(record["preparation"])
                or not isinstance(record["selection_digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", record["selection_digest"])
                or not isinstance(record["config_digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", record["config_digest"])
                or (record["result"] is not None and not isinstance(record["result"], dict))):
            raise ReviewQueueError("Action history contains a malformed review.")
        created, expires = _time(record["created_at"]), _time(record["expires_at"])
        _validate_preparation(record["preparation"])
        if expires - created != timedelta(minutes=15):
            raise ReviewQueueError("Action confirmation has an invalid lifetime.")
        if record["consented_at"] is not None:
            if not created <= _time(record["consented_at"]) < expires:
                raise ReviewQueueError("Action consent is outside its confirmation lifetime.")
        if record["status"] in {"awaiting_confirmation", "invalidated", "expired"} and record["consented_at"] is not None:
            raise ReviewQueueError("An unconsumed confirmation contains consent.")
        if record["status"] in {"executing", "completed", "recovery_required"} and record["consented_at"] is None:
            raise ReviewQueueError("Action outcome lacks its explicit consent record.")
        if record["status"] in {"awaiting_confirmation", "executing", "expired"} and record["result"] is not None:
            raise ReviewQueueError("An unfinished action contains a terminal result.")
        if record["status"] == "completed":
            result = record["result"]
            if (not isinstance(result, dict) or not isinstance(result.get("status"), str)
                    or result["status"] not in {"merged", "already_applied"}
                    or result.get("binding") != record["preparation"]["binding"]
                    or result.get("mutation_performed") is not (result["status"] == "merged")):
                raise ReviewQueueError("Completed action has an invalid executor result.")
        if record["status"] == "awaiting_confirmation" and now >= expires:
            record["status"] = "expired"
    return state


def _save_reviews(directory: Path, state: dict[str, Any], *, keep: str) -> None:
    reviews = state["reviews"]
    # Unknown/in-flight actions and unconsumed confirmations are never silently
    # pruned. An evicted terminal review ID cannot be used to execute again.
    while len(reviews) > _MAX_REVIEWS:
        retired = next((key for key, item in reviews.items() if key != keep
                        and item["status"] in {"completed", "invalidated", "expired"}), None)
        if retired is None:
            raise ReviewQueueError("Action history is full; resolve outstanding reviews before continuing.")
        del reviews[retired]
    save_private_json(directory, _REVIEWS_PATH, state, maximum_bytes=_MAX_BYTES)


def _require_current_config(queue: ReviewQueue, path: Path) -> None:
    try:
        current = load_config(path)
    except ConfigError as exc:
        raise ReviewQueueError("Configuration is unavailable; request a fresh review.") from exc
    if current != queue.config:
        raise ReviewQueueError("Configuration changed; request a fresh review.")


def prepare_review_action(
    queue: ReviewQueue,
    client: ExecutionGitHubClient,
    proposal_id: str,
    version: int,
    *,
    config_path: Path,
) -> dict[str, Any]:
    """Read-only preparation of the backend-resolved exact selected action.

    Neither plan content nor execution tokens come from the window. Live reads
    happen outside the queue lock; a second resolution rejects a plan, policy
    or local-handling change during collection. This result is display context,
    not authorization, and exposes no token or mutating operation.
    """
    _require_current_config(queue, config_path)
    plan, selected = queue.current_selection(proposal_id, version)
    prepared = preflight_execution(
        queue.config, client, plan, selected["step"]["step_id"], state_directory=queue.directory,
    )
    _require_current_config(queue, config_path)
    current_plan, current_selection = queue.current_selection(proposal_id, version)
    if current_plan != plan or current_selection != selected:
        raise ReviewQueueError("Review context changed during preparation; refresh the queue.")
    return {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "version": version,
        "plan_id": plan["plan_id"],
        "status": prepared["status"],
        "binding": prepared["binding"],
        "decision": prepared["decision"],
        "action_id": prepared["action_id"],
        "mutation_performed": False,
        "mutation_authorized": False,
        "capabilities": {"github_mutations": False, "model_calls": False},
    }


class ActionReviews:
    """Durable per-item confirmation around the one existing executor.

    The window may request preparation, then separately confirm a returned
    review ID. It cannot supply the action binding, executor token or paths.
    Confirmation is consumed inside the executor's operation lock before live
    reads. Lost replies/interruption never cause automatic consent replay.
    """

    def __init__(self, queue: ReviewQueue, client: ExecutionGitHubClient, *, config_path: Path) -> None:
        self.queue = queue
        self.client = client
        self.config_path = config_path.expanduser().absolute()

    def _now(self) -> datetime:
        now = self.queue.clock()
        if now.utcoffset() is None:
            raise ReviewQueueError("Action clock must include a time zone.")
        return now

    @staticmethod
    def _view(record: Mapping[str, Any], *, outcome_recorded: bool = True) -> dict[str, Any]:
        view = copy.deepcopy({key: record[key] for key in (
            "review_id", "preparation", "created_at", "expires_at", "status", "consented_at", "result",
        )})
        # A reopened caller cannot know whether an earlier command is still
        # alive. Keep that uncertainty explicit instead of offering approve.
        if view["status"] == "executing":
            view["status"] = "recovery_required"
            view["message"] = "The outcome is not recorded yet. It may still be running; do not approve again."
        view["schema_version"] = 1
        view["outcome_recorded"] = outcome_recorded
        return view

    def snapshot(self) -> list[dict[str, Any]]:
        # Atomic private JSON reads do not need the operation lock. This remains
        # readable while the executor holds that lock during live collection.
        state = _load_reviews(self.queue.directory, self._now())
        return [self._view(record) for record in state["reviews"].values()]

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Fixed action protocol; request bodies never select paths or bindings."""
        operation = request.get("operation")
        request_id = request.get("request_id")
        if (type(request.get("schema_version")) is not int or request["schema_version"] != 1
                or not isinstance(request_id, str) or not re.fullmatch(r"action_[a-f0-9]{32}", request_id)
                or not isinstance(operation, str)
                or operation not in {"prepare_action", "confirm_action", "check_action", "action_history", "collect_queue"}):
            raise ReviewQueueError("Action request has an invalid version, ID or operation.")
        fields = {"schema_version", "request_id", "operation"}
        fields |= {"proposal_id", "version"} if operation == "prepare_action" else (
            {"review_id"} if operation not in {"action_history", "collect_queue"} else set())
        if set(request) != fields:
            raise ReviewQueueError("Action request contains missing or unsupported fields.")
        response: dict[str, Any] = {"schema_version": 1, "request_id": request_id, "status": "ok"}
        if operation == "collect_queue":
            _require_current_config(self.queue, self.config_path)
            if self.queue.config.advisory.mode != "off":
                raise ReviewQueueError("This fresh-collection route makes no model calls. Configure the investigator before refreshing an AI-enabled profile here.")
            report = reconcile(self.queue.config, cast(GitHubClient, self.client), config_path=self.config_path,
                               state_directory=self.queue.directory, clock=self.queue.clock,
                               notification_mode="off")
            _require_current_config(self.queue, self.config_path)
            response = self.queue.handle({"schema_version": 1, "request_id": "read_" + secrets.token_hex(16),
                                          "operation": "snapshot"})
            response.update(request_id=request_id, reviews=self.snapshot(),
                            execution_enabled=self.queue.config.operation.autonomy == "approval_required"
                            and self.queue.config.operation.mutation_enabled,
                            collection_status=report["status"], collection_outcome=report["outcome"])
        elif operation == "action_history":
            response["reviews"] = self.snapshot()
        elif operation == "prepare_action":
            response["review"] = self.prepare(request["proposal_id"], request["version"])
        elif operation == "confirm_action":
            response["review"] = self.confirm(request["review_id"])
        else:
            response["review"] = self.check_outcome(request["review_id"])
        return response

    def check_outcome(self, review_id: str) -> dict[str, Any]:
        """Observe an interrupted exact action; never replay its consent.

        The shared operation lock prevents a competing active command's journal
        being overwritten. Removed repositories or changed configured branches
        do not grant read authority from historical UI state.
        """
        if not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id):
            raise ReviewQueueError("Outcome checking requires a valid saved review ID.")
        with run_lock(self.queue.directory):
            _require_current_config(self.queue, self.config_path)
            state = _load_reviews(self.queue.directory, self._now())
            record = state["reviews"].get(review_id)
            if record is None:
                raise ReviewQueueError("The saved review is absent or retired.")
            if record["status"] not in {"executing", "recovery_required"}:
                return self._view(record)
            binding = record["preparation"]["binding"]
            configured = next((repo for repo in self.queue.config.repositories
                               if repository_names_equal(repo.name, binding["repository"])), None)
            if configured is None or configured.default_branch != binding["expected_base"]:
                raise ReviewQueueError("The historical action is outside the current repository/branch scope.")
            audit = exact_action_audit(self.queue.directory, binding)
            live = self.client.fetch_pull_request_state(binding["repository"], binding["pull_request"])
            _require_current_config(self.queue, self.config_path)
            matches = (type(live.get("number")) is int and live["number"] == binding["pull_request"]
                       and live.get("headRefOid") == binding["head_sha"]
                       and live.get("baseRefName") == binding["expected_base"])
            if matches and live.get("state") == "MERGED":
                record.update(status="completed", result={
                    "status": "already_applied", "mutation_performed": False,
                    "binding": copy.deepcopy(binding), "action_id": record["preparation"]["action_id"],
                    "watcher_success_recorded": audit["success_recorded"],
                    "next": "GitHub confirms this exact head is merged. This check made no changes; refresh the queue.",
                })
            else:
                record.update(status="recovery_required", result={
                    "status": "recovery_required", "mutation_performed": None,
                    "binding": copy.deepcopy(binding), "action_id": record["preparation"]["action_id"],
                    "next": "The approved head is not confirmed merged. No action was retried; recovery remains necessary.",
                })
            _save_reviews(self.queue.directory, state, keep=review_id)
            return self._view(record)

    def prepare(self, proposal_id: str, version: int) -> dict[str, Any]:
        prepared = prepare_review_action(
            self.queue, self.client, proposal_id, version, config_path=self.config_path,
        )
        if prepared["status"] == "already_applied":
            return {"schema_version": 1, "status": "already_applied", "preparation": prepared}
        now = self._now()
        review_id = "review_" + secrets.token_hex(16)
        with run_lock(self.queue.directory):
            _require_current_config(self.queue, self.config_path)
            plan, selected = self.queue._selection_under_lock(proposal_id, version, now)
            if plan["plan_id"] != prepared["plan_id"]:
                raise ReviewQueueError("Plan changed before confirmation was saved; prepare again.")
            state = _load_reviews(self.queue.directory, now)
            repository = selected["repository"]
            for item in state["reviews"].values():
                if (repository_names_equal(item["preparation"]["binding"]["repository"], repository)
                        and item["status"] in {"executing", "recovery_required"}):
                    raise ReviewQueueError("An earlier action needs its outcome checked before another approval.")
            record = {
                "review_id": review_id, "preparation": prepared,
                "preparation_digest": _digest(prepared), "selection_digest": _digest(selected),
                "config_digest": _digest(asdict(self.queue.config)),
                "created_at": _timestamp(now), "expires_at": _timestamp(now + timedelta(minutes=15)),
                "status": "awaiting_confirmation", "consented_at": None, "result": None,
            }
            state["reviews"][review_id] = record
            _save_reviews(self.queue.directory, state, keep=review_id)
        return self._view(record)

    def confirm(self, review_id: str) -> dict[str, Any]:
        """Execute only after a separate deliberate exact-review confirmation."""
        if not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id):
            raise ReviewQueueError("Confirmation requires a valid saved review ID.")
        now = self._now()
        state = _load_reviews(self.queue.directory, now)
        record = state["reviews"].get(review_id)
        if record is None:
            raise ReviewQueueError("Confirmation is absent or retired; prepare a fresh review.")
        if record["status"] != "awaiting_confirmation":
            # Already-consumed requests return evidence, never invoke/retry the
            # executor, including when the previous response was lost.
            return self._view(record)
        _require_current_config(self.queue, self.config_path)
        prepared = record["preparation"]
        plan, selected = self.queue.current_selection(prepared["proposal_id"], prepared["version"])
        if (_digest(selected) != record["selection_digest"]
                or plan["plan_id"] != prepared["plan_id"]
                or _digest(asdict(self.queue.config)) != record["config_digest"]):
            raise ReviewQueueError("Saved confirmation no longer matches current context; prepare again.")
        consumed = False
        consented_at: str | None = None

        def consume_under_execution_lock() -> None:
            nonlocal consumed, consented_at
            current_time = self._now()
            current = _load_reviews(self.queue.directory, current_time)
            saved = current["reviews"].get(review_id)
            if saved is None or saved["status"] != "awaiting_confirmation":
                raise ReviewQueueError("Confirmation was consumed or expired; no action was started.")
            if saved != record:
                raise ReviewQueueError("Confirmation changed before execution; prepare again.")
            _, item = self.queue._selection_under_lock(
                prepared["proposal_id"], prepared["version"], current_time,
            )
            current_plan = self.queue._current_plan()
            if (current_plan is None or current_plan["plan_id"] != prepared["plan_id"]
                    or _digest(item) != record["selection_digest"]):
                raise ReviewQueueError("Selection changed before execution; prepare again.")
            _require_current_config(self.queue, self.config_path)
            saved.update(status="executing", consented_at=_timestamp(current_time))
            _save_reviews(self.queue.directory, current, keep=review_id)
            consumed = True
            consented_at = saved["consented_at"]

        try:
            execution = execute_approved(
                self.queue.config, self.client, plan, selected["step"]["step_id"],
                approval_token(prepared["binding"]), state_directory=self.queue.directory,
                config_path=self.config_path, before_execution=consume_under_execution_lock,
            )
            result = {key: execution[key] for key in (
                "status", "mutation_performed", "binding", "decision", "action_id", "result",
                "post_action_state", "audit_path", "recovery", "next",
            ) if key in execution}
            record.update(status="completed", result=result)
        except Exception:
            # After consumption an exception can follow a remote success or
            # terminal audit failure. Never report no mutation or retry it.
            record.update(status="recovery_required" if consumed else "invalidated", result={
                "status": "recovery_required" if consumed else "blocked",
                "mutation_performed": None if consumed else False,
                "next": "Check the execution audit and exact live PR outcome before another approval."
                        if consumed else "Prepare a fresh review; this action was not started.",
            })
        if consumed:
            # Preserve the actual saved consent timestamp, not response time.
            record["consented_at"] = consented_at
        try:
            with run_lock(self.queue.directory):
                current = _load_reviews(self.queue.directory, self._now())
                saved = current["reviews"].get(review_id)
                if saved is None:
                    raise ReviewQueueError("Consumed review disappeared; check the execution audit.")
                if not consumed and saved["status"] != "awaiting_confirmation":
                    # Another confirmation may have completed between the read
                    # and executor-lock acquisition. Never overwrite its result.
                    return self._view(saved)
                record["consented_at"] = saved["consented_at"]
                if consumed and record["consented_at"] is None:
                    raise ReviewQueueError("Consumed review lost its consent record.")
                current["reviews"][review_id] = record
                _save_reviews(self.queue.directory, current, keep=review_id)
        except (RuntimeStateError, ReviewQueueError):
            # A confirmed merge stays visible even if this second journal write
            # fails. The executor audit remains the recovery source of truth.
            view = self._view(record, outcome_recorded=False)
            if not consumed:
                # A competing invocation may have consumed the same review.
                # Without a durable terminal transition, no whole-action
                # no-mutation promise is justified by this attempt's failure.
                view.update(status="recovery_required", result={
                    "status": "recovery_required", "mutation_performed": None,
                    "next": "Refresh the saved outcome and execution audit before another approval.",
                })
            view["message"] = "The review outcome could not be saved. Check the execution audit before another action."
            return view
        return self._view(record)
