from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .advisory import (
    AdvisoryProvider,
    advisory_action_policy,
    advise_from_inspection,
    queue_synopsis,
)
from .config import WatcherConfig
from .execution import ExecutionError, execution_observation
from .github import GitHubClient, GitHubError
from .inspector import Clock, _utc_now, inspect
from .local_sync import validate_control_locations
from .planning import plan_from_advisory
from .runtime_state import (
    RuntimeStateError,
    append_run_record,
    default_state_directory,
    load_private_json,
    load_run_state,
    prune_plan_artifacts,
    run_lock,
    save_private_json,
    save_run_state,
)


RUN_SCHEMA_VERSION = 1
_VOLATILE_EVIDENCE_KEYS = frozenset(
    {"generated_at", "observed_at", "evidence_observed_at"}
)


class ReconciliationError(RuntimeError):
    """Raised when a run-once reconciliation cannot finish safely."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _normalized_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _normalized_evidence(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_EVIDENCE_KEYS
        }
    if isinstance(value, list):
        return [_normalized_evidence(item) for item in value]
    return value


def _sanitize_scheduled_inspection(inspection: Mapping[str, Any]) -> dict[str, Any]:
    """Remove raw collector failures before durable or scheduled output."""

    sanitized = copy.deepcopy(dict(inspection))
    for repository in sanitized.get("repositories") or []:
        if not isinstance(repository, dict):
            continue
        errors = repository.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if not isinstance(error, dict):
                    continue
                surface = str(error.get("surface") or "repository_evidence")
                error["message"] = f"GitHub collection failed for {surface}."
        manifest_evidence = repository.get("manifest_evidence")
        if isinstance(manifest_evidence, dict):
            manifest_errors = manifest_evidence.get("errors")
            if isinstance(manifest_errors, list):
                for error in manifest_errors:
                    if isinstance(error, dict):
                        error["message"] = "Manifest evidence collection failed."
        pull_requests = repository.get("pull_requests")
        if isinstance(pull_requests, list):
            for pull_request in pull_requests:
                if (
                    isinstance(pull_request, dict)
                    and pull_request.get("classification") == "collection_error"
                ):
                    pull_request["reasons"] = [
                        "Pull-request evidence collection failed."
                    ]
    return sanitized


def evidence_revision(inspection: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _normalized_evidence(inspection),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"evidence_{hashlib.sha256(payload).hexdigest()[:24]}"


def _run_id(generated_at: str, revision: str) -> str:
    payload = f"{generated_at}\0{revision}\0{uuid.uuid4().hex}".encode("utf-8")
    return f"run_{hashlib.sha256(payload).hexdigest()[:20]}"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _retry_is_deferred(
    last_attempt: Mapping[str, Any],
    *,
    now: datetime,
    retry_after_seconds: int,
) -> tuple[bool, str | None]:
    if last_attempt.get("outcome") == "retry_deferred":
        retry_at = _parse_timestamp(last_attempt.get("retry_at"))
        if retry_at is None:
            return False, None
        return now < retry_at, _timestamp(retry_at)
    if last_attempt.get("outcome") not in {"partial", "failed"}:
        return False, None
    attempted_at = _parse_timestamp(last_attempt.get("completed_at"))
    if attempted_at is None:
        return False, None
    retry_at = attempted_at + timedelta(seconds=retry_after_seconds)
    if now >= retry_at:
        return False, _timestamp(retry_at)
    return True, _timestamp(retry_at)


def _schedule_observation(
    state: Mapping[str, Any],
    *,
    now: datetime,
    expected_interval_minutes: int | None,
) -> dict[str, Any]:
    previous = state.get("last_attempt")
    previous_data = previous if isinstance(previous, Mapping) else {}
    previous_completed = _parse_timestamp(previous_data.get("completed_at"))
    elapsed_seconds = (
        max(0, int((now - previous_completed).total_seconds()))
        if previous_completed is not None
        else None
    )
    if expected_interval_minutes is None:
        return {
            "expected_interval_minutes": None,
            "previous_completed_at": (
                _timestamp(previous_completed) if previous_completed is not None else None
            ),
            "elapsed_seconds": elapsed_seconds,
            "overdue_catch_up": False,
        }
    interval_seconds = expected_interval_minutes * 60
    grace_seconds = max(300, interval_seconds // 2)
    return {
        "expected_interval_minutes": expected_interval_minutes,
        "previous_completed_at": (
            _timestamp(previous_completed) if previous_completed is not None else None
        ),
        "elapsed_seconds": elapsed_seconds,
        "overdue_catch_up": (
            elapsed_seconds is not None
            and elapsed_seconds > interval_seconds + grace_seconds
        ),
    }


def _plan_step_count(plan: Mapping[str, Any]) -> int:
    summary = plan.get("summary")
    return int((summary or {}).get("recommended_step_count") or 0)


def _first_executable_step(plan: Mapping[str, Any]) -> str | None:
    for repository in plan.get("repositories") or []:
        if not isinstance(repository, Mapping):
            continue
        for step in repository.get("recommended_order") or []:
            if not isinstance(step, Mapping):
                continue
            if (
                step.get("kind") == "pull_request"
                and step.get("action") == "validate_then_merge"
                and not step.get("blockers")
                and isinstance(step.get("step_id"), str)
            ):
                return str(step["step_id"])
    return None


def _advisory_coverage(advisory_report: Mapping[str, Any]) -> dict[str, Any]:
    advisory = advisory_report.get("advisory")
    advisory_data = advisory if isinstance(advisory, Mapping) else {}
    summary = advisory_data.get("summary")
    summary_data = summary if isinstance(summary, Mapping) else {}
    mode = advisory_data.get("mode")
    eligible = int(summary_data.get("eligible_candidate_count") or 0)
    advised = int(summary_data.get("advised_candidate_count") or 0)
    deferred = int(summary_data.get("deferred_candidate_count") or 0)
    errors = list(advisory_data.get("errors") or [])
    complete = mode == "off" or (
        not errors and deferred == 0 and advised == eligible
    )
    return {
        "mode": mode,
        "eligible_candidate_count": eligible,
        "advised_candidate_count": advised,
        "deferred_candidate_count": deferred,
        "provider_call_count": int(summary_data.get("provider_call_count") or 0),
        "complete": complete,
        "errors": copy.deepcopy(errors),
    }


def _provider_usage(advisory_report: Mapping[str, Any]) -> dict[str, Any]:
    advisory = advisory_report.get("advisory")
    advisory_data = advisory if isinstance(advisory, Mapping) else {}
    calls = [
        copy.deepcopy(dict(call))
        for call in advisory_data.get("calls") or []
        if isinstance(call, Mapping)
    ]
    summary = advisory_data.get("summary")
    summary_data = summary if isinstance(summary, Mapping) else {}
    attempted_call_count = int(summary_data.get("provider_call_count") or 0)
    return {
        "provider": advisory_data.get("provider"),
        "calls": calls,
        "call_count": attempted_call_count,
        "successful_call_count": len(calls),
        "usage": {
            key: sum(
                int((call.get("usage") or {}).get(key) or 0)
                for call in calls
                if isinstance(call.get("usage"), Mapping)
            )
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        },
    }


def _notification(
    *,
    outcome: str,
    plan: Mapping[str, Any] | None,
    plan_path: Path | None,
    config_path: Path,
    state_directory: Path,
    execution: Mapping[str, Any],
    execution_enabled: bool = False,
) -> dict[str, Any]:
    if execution.get("recovery_required") is True:
        return {
            "needed": True,
            "reason": "action_recovery_required",
            "message": "A previous approved Dependabot action needs recovery review.",
            "next": {
                "argv": [
                    "dependabot-watcher",
                    "run",
                    "--config",
                    str(config_path),
                    "--state-dir",
                    str(state_directory),
                    "--format",
                    "json",
                ]
            },
        }
    if outcome in {"unchanged", "no_work", "retry_deferred"}:
        return {"needed": False, "reason": outcome, "message": None, "next": None}
    if outcome in {"partial", "failed"}:
        return {
            "needed": True,
            "reason": "incomplete_evidence_or_advice",
            "message": "The scheduled Dependabot run failed or produced incomplete evidence.",
            "next": {
                "argv": [
                    "dependabot-watcher",
                    "run",
                    "--config",
                    str(config_path),
                    "--state-dir",
                    str(state_directory),
                    "--format",
                    "json",
                ]
            },
        }
    step_id = _first_executable_step(plan or {})
    if step_id and plan_path is not None:
        if not execution_enabled:
            return {
                "needed": True,
                "reason": "candidate_available_execution_disabled",
                "message": "A candidate is available for investigation; GitHub execution is disabled in this profile.",
                "next": {"argv": ["dependabot-watcher", "plan", "--config", str(config_path),
                                  "--verbosity", "standard"]},
            }
        return {
            "needed": True,
            "reason": "execution_preflight_required",
            "message": "A candidate needs fresh execution preflight and any required agent investigation; no merge is approved.",
            "next": {
                "argv": [
                    "dependabot-watcher",
                    "execute",
                    "--config",
                    str(config_path),
                    "--state-dir",
                    str(state_directory),
                    "--plan-file",
                    str(plan_path),
                    "--step-id",
                    step_id,
                ]
            },
        }
    return {
        "needed": True,
        "reason": "review_required",
        "message": "The Dependabot queue has changed and requires review.",
        "next": {
            "argv": [
                "dependabot-watcher",
                "plan",
                "--config",
                str(config_path),
                "--verbosity",
                "standard",
            ]
        },
    }


def _notification_delivery(
    notification: Mapping[str, Any],
    notification_mode: str,
) -> dict[str, Any]:
    if notification_mode not in {"off", "desktop"}:
        raise ReconciliationError(f"Unsupported notification mode: {notification_mode}.")
    if not notification.get("needed"):
        status = "not_needed"
    elif notification_mode == "off":
        status = "disabled"
    else:
        status = "pending"
    return {
        "mode": notification_mode,
        "status": status,
        "attempted_at": None,
        "delivered_at": None,
        "error": None,
    }


def _attach_notification(
    record: dict[str, Any],
    notification: Mapping[str, Any],
    notification_mode: str,
) -> dict[str, Any]:
    record["notification"] = copy.deepcopy(dict(notification))
    record["notification_delivery"] = _notification_delivery(
        notification,
        notification_mode,
    )
    return record


def _notification_predecessor(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Use accepted equivalent history for deduplication, not new-run attribution.

    Acceptance can arrive after an unchanged run was saved. Keep that receipt on
    its actual run, but don't retry the same notice just because the latest run
    was still pending when collected. Changed evidence/policy/action never reuse it.
    """
    previous = state.get("last_attempt")
    if not isinstance(previous, Mapping):
        return {}
    notification = previous.get("notification")
    delivery = previous.get("notification_delivery")
    if (not isinstance(notification, Mapping) or notification.get("needed") is not True
            or not isinstance(delivery, Mapping) or delivery.get("status") == "delivered"):
        return previous
    for earlier in reversed(state.get("history") or []):
        accepted = earlier.get("notification_delivery")
        if (isinstance(accepted, Mapping) and accepted.get("status") == "delivered"
                and accepted.get("mode") == delivery.get("mode")
                and earlier.get("notification") == notification
                # Plan identity binds material advice, not just GitHub evidence.
                # Equal generic notice text cannot acknowledge changed advice.
                and earlier.get("plan_id") == previous.get("plan_id")
                and earlier.get("advisory_coverage") == previous.get("advisory_coverage")
                and all(earlier.get(field) == previous.get(field) and previous.get(field) is not None
                        for field in ("evidence_revision", "advisory_policy_id", "execution_revision"))):
            return {**dict(previous), "notification_delivery": accepted}
    return previous


def _suppressed_notification(
    previous: Mapping[str, Any],
    *,
    default: Mapping[str, Any],
    notification_mode: str,
    execution_revision: str,
    current_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous_notification = previous.get("notification")
    notification = (
        previous_notification
        if isinstance(previous_notification, Mapping)
        else {}
    )
    previous_delivery = previous.get("notification_delivery")
    delivery = previous_delivery if isinstance(previous_delivery, Mapping) else {}
    recovery_supersedes_previous = default.get("reason") == "action_recovery_required"
    should_retry = (
        not recovery_supersedes_previous
        and notification_mode != "off"
        and notification.get("needed") is True
        and (
            delivery.get("mode") != notification_mode
            or delivery.get("status") not in {"delivered", "not_needed"}
        )
    )
    if (
        notification.get("reason") == default.get("reason")
        and previous.get("execution_revision") == execution_revision
        and delivery.get("mode") == notification_mode
        and delivery.get("status") == "delivered"
    ):
        return {
            "needed": False,
            "reason": "unchanged",
            "message": None,
            "next": None,
        }
    if should_retry:
        if default.get("needed") is True:
            return copy.deepcopy(dict(default))
        if current_candidate is not None and notification.get("reason") in {
            "approval_ready", "execution_preflight_required", "candidate_available_execution_disabled",
        }:
            return copy.deepcopy(dict(current_candidate))
    return copy.deepcopy(dict(notification if should_retry else default))


def _record(
    *,
    run_id: str,
    completed_at: str,
    outcome: str,
    revision: str,
    inspection: Mapping[str, Any],
    synopsis: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    plan_path: Path | None,
    provider_usage: Mapping[str, Any] | None,
    advisory_coverage: Mapping[str, Any] | None,
    execution: Mapping[str, Any],
    schedule: Mapping[str, Any],
    supersedes_plan_id: object,
    advisory_policy_id: str,
    retry_at: str | None = None,
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    summary = inspection.get("summary")
    summary_data = summary if isinstance(summary, Mapping) else {}
    return {
        "run_id": run_id,
        "completed_at": completed_at,
        "outcome": outcome,
        "evidence_revision": revision,
        "advisory_policy_id": advisory_policy_id,
        "evidence_complete": inspection.get("status") == "complete",
        "queue": {
            "repository_count": int(summary_data.get("repository_count") or 0),
            "open_pull_request_count": int(summary_data.get("open_pull_request_count") or 0),
            "open_alert_count": int(summary_data.get("open_alert_count") or 0),
            "attention_required_count": int(summary_data.get("attention_required_count") or 0),
            "unresolved_alert_count": int(summary_data.get("unresolved_alert_count") or 0),
            "synopsis_complete": bool(synopsis.get("complete"))
            and not bool(synopsis.get("truncated")),
        },
        "advisory_coverage": copy.deepcopy(dict(advisory_coverage or {})),
        "provider_usage": copy.deepcopy(dict(provider_usage or {})),
        "execution_observation": copy.deepcopy(dict(execution)),
        "execution_revision": execution.get("revision"),
        "schedule_observation": copy.deepcopy(dict(schedule)),
        "failure": copy.deepcopy(dict(failure or {})),
        "plan_id": plan.get("plan_id") if plan is not None else None,
        "plan_path": str(plan_path) if plan_path is not None else None,
        "plan_status": plan.get("status") if plan is not None else None,
        "recommended_step_count": _plan_step_count(plan or {}),
        "supersedes_plan_id": supersedes_plan_id,
        "retry_at": retry_at,
        "mutation_authorized": False,
    }


_MAX_INVALIDATED_PLAN_IDS = 2_048


def _advance_plan_lineage(
    previous_plan: Mapping[str, Any],
) -> tuple[list[str], bool]:
    """Carry bounded supersession lineage forward without reopening stale plans."""

    raw_invalidated = previous_plan.get("invalidated_plan_ids")
    invalidated = [
        item
        for item in raw_invalidated
        if isinstance(item, str) and item.startswith("plan_")
    ] if isinstance(raw_invalidated, list) else []
    for field in ("supersedes_plan_id", "plan_id"):
        plan_id = previous_plan.get(field)
        if isinstance(plan_id, str) and plan_id.startswith("plan_"):
            invalidated.append(plan_id)
    deduplicated = list(dict.fromkeys(invalidated))
    raw_overflow = previous_plan.get("lineage_overflow")
    overflow = raw_overflow is True
    has_legacy_authority_record = any(
        field in previous_plan
        for field in ("plan_id", "supersedes_plan_id", "revoked_reason")
    )
    if has_legacy_authority_record and (
        not isinstance(raw_invalidated, list)
        or not isinstance(raw_overflow, bool)
    ):
        # Pre-lineage records cannot prove that older superseded plans are
        # absent. Preserve known predecessors and carry a fail-closed marker
        # into the first post-upgrade revocation transition.
        overflow = True
    if len(deduplicated) > _MAX_INVALIDATED_PLAN_IDS:
        deduplicated = deduplicated[-_MAX_INVALIDATED_PLAN_IDS:]
        overflow = True
    return deduplicated, overflow


@contextmanager
def _collection_window(
    config: WatcherConfig, client: GitHubClient, state_path: Path, now: datetime,
) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Serialize collectors without blocking interactive journals on network IO.

    The queue lock is held only for baseline capture and commit. An intervening
    run-state or execution change invalidates this collection, including failed
    collection: old evidence must not replace or revoke a newer action/plan.
    The separate refresh lease stays held through commit to prevent collectors
    publishing snapshots in the reverse order. Advisory work during commit
    remains unchanged; configured supervisor launches require advice off.
    """
    with run_lock(state_path / "reconciliation"):
        with run_lock(state_path, wait_seconds=2):
            before = load_run_state(state_path)
            execution_before = execution_observation(state_path, {})
        try:
            inspection = _sanitize_scheduled_inspection(inspect(config, client, clock=lambda: now))
        except GitHubError:
            inspection = None
        with run_lock(state_path, wait_seconds=2):
            state = load_run_state(state_path)
            if state != before or execution_observation(state_path, {}) != execution_before:
                raise ReconciliationError("Run or execution state changed during collection; refresh before saving this evidence.")
            yield state, inspection


def reconcile(
    config: WatcherConfig,
    client: GitHubClient,
    *,
    config_path: Path,
    state_directory: Path | None = None,
    provider: AdvisoryProvider | None = None,
    clock: Clock = _utc_now,
    retry_after_seconds: int = 3600,
    notification_mode: str = "off",
    expected_interval_minutes: int | None = None,
) -> dict[str, Any]:
    """Run one durable, non-mutating observation/advice/plan reconciliation."""

    if retry_after_seconds < 60 or retry_after_seconds > 604_800:
        raise ReconciliationError("Retry delay must be between 60 seconds and 7 days.")
    if expected_interval_minutes is not None and not 15 <= expected_interval_minutes <= 10_080:
        raise ReconciliationError(
            "Expected schedule interval must be between 15 minutes and 7 days."
        )
    state_path = (state_directory or default_state_directory()).expanduser()
    # Validate before the refresh lease or queue lock creates/chmods anything.
    validate_control_locations(config, config_path, state_path)
    now = clock().astimezone(timezone.utc)
    completed_at = _timestamp(now)
    advisory_policy_id = str(advisory_action_policy(config)["policy_id"])
    try:
        with _collection_window(config, client, state_path, now) as (state, collected):
            schedule = _schedule_observation(
                state,
                now=now,
                expected_interval_minutes=expected_interval_minutes,
            )
            try:
                if collected is None:
                    raise GitHubError("GitHub collection failed.")
                inspection = collected
            except GitHubError:
                inspection = {
                    "schema_version": 1,
                    "mode": "inspect",
                    "generated_at": completed_at,
                    "status": "partial",
                    "capabilities": {
                        "model_calls": False,
                        "persistent_local_writes": False,
                        "github_mutations": False,
                        "notifications": False,
                    },
                    "summary": {
                        "repository_count": len(config.repositories),
                        "open_pull_request_count": 0,
                        "open_alert_count": 0,
                        "attention_required_count": 0,
                        "unresolved_alert_count": 0,
                        "error_count": 1,
                        "truncation_count": 0,
                    },
                    "repositories": [],
                    "errors": [
                        {
                            "category": "github_collection_error",
                            "message": "GitHub collection failed before repository evidence was available.",
                        }
                    ],
                }
                revision = "failure_github_collection"
                synopsis = queue_synopsis(inspection)
                execution = execution_observation(state_path, inspection)
                previous_plan = state.get("latest_plan")
                previous_plan_data = (
                    previous_plan if isinstance(previous_plan, Mapping) else {}
                )
                invalidated_plan_ids, lineage_overflow = _advance_plan_lineage(
                    previous_plan_data
                )
                retry_at = _timestamp(
                    now + timedelta(seconds=retry_after_seconds)
                )
                previous = _notification_predecessor(state)
                record = _record(
                    run_id=_run_id(completed_at, revision),
                    completed_at=completed_at,
                    outcome="failed",
                    revision=revision,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=None,
                    plan_path=None,
                    provider_usage=None,
                    advisory_coverage=None,
                    execution=execution,
                    schedule=schedule,
                    supersedes_plan_id=previous_plan_data.get("plan_id"),
                    advisory_policy_id=advisory_policy_id,
                    retry_at=retry_at,
                    failure={"category": "github_collection_error"},
                )
                notification = _notification(
                    execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                    outcome="failed",
                    plan=None,
                    plan_path=None,
                    config_path=config_path,
                    state_directory=state_path,
                    execution=execution,
                )
                if previous.get("evidence_revision") == revision:
                    notification = _suppressed_notification(
                        previous,
                        default=notification,
                        notification_mode=notification_mode,
                        execution_revision=str(execution.get("revision") or ""),
                    )
                _attach_notification(
                    record,
                    notification,
                    notification_mode,
                )
                revoked_plan = {
                    "plan_id": None,
                    "supersedes_plan_id": previous_plan_data.get("plan_id"),
                    "invalidated_plan_ids": invalidated_plan_ids,
                    "lineage_overflow": lineage_overflow,
                    "revoked_reason": "github_collection_error",
                    "evidence_revision": revision,
                    "recorded_at": completed_at,
                }
                state = append_run_record(
                    state,
                    record,
                    successful=False,
                    latest_plan=revoked_plan,
                )
                state_file = save_run_state(state_path, state)
                return _report(
                    config=config,
                    record=record,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=None,
                    state_file=state_file,
                    state_directory=state_path,
                )
            revision = evidence_revision(inspection)
            run_id = _run_id(completed_at, revision)
            synopsis = queue_synopsis(inspection)
            execution = execution_observation(state_path, inspection)
            previous = _notification_predecessor(state)
            previous_plan = state.get("latest_plan")
            previous_plan_data = previous_plan if isinstance(previous_plan, Mapping) else {}
            supersedes_plan_id = previous_plan_data.get("plan_id")
            invalidated_plan_ids, lineage_overflow = _advance_plan_lineage(
                previous_plan_data
            )

            same_revision = (
                previous.get("evidence_revision") == revision
                and previous.get("advisory_policy_id") == advisory_policy_id
            )
            retained_plan: dict[str, Any] | None = None
            retained_plan_path: Path | None = None
            retained_plan_id = previous_plan_data.get("plan_id")
            if same_revision and isinstance(retained_plan_id, str):
                retained_plan_path = state_path / "plans" / f"{retained_plan_id}.json"
                retained_plan = load_private_json(
                    state_path,
                    Path("plans") / f"{retained_plan_id}.json",
                )
                if retained_plan.get("plan_id") != retained_plan_id:
                    raise ReconciliationError(
                        "Retained plan identity does not match durable run state."
                    )
            retry_deferred, retry_at = _retry_is_deferred(
                previous,
                now=now,
                retry_after_seconds=retry_after_seconds,
            )
            if same_revision and previous.get("outcome") in {
                "action_required",
                "no_work",
                "unchanged",
            }:
                outcome = "unchanged"
                record = _record(
                    run_id=run_id,
                    completed_at=completed_at,
                    outcome=outcome,
                    revision=revision,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=retained_plan,
                    plan_path=retained_plan_path,
                    provider_usage=None,
                    advisory_coverage=(
                        previous.get("advisory_coverage")
                        if isinstance(previous.get("advisory_coverage"), Mapping)
                        else None
                    ),
                    execution=execution,
                    schedule=schedule,
                    supersedes_plan_id=previous_plan_data.get("supersedes_plan_id"),
                    advisory_policy_id=advisory_policy_id,
                )
                notification = _suppressed_notification(
                    previous,
                    current_candidate=_notification(
                        outcome="action_required", plan=retained_plan, plan_path=retained_plan_path,
                        config_path=config_path, state_directory=state_path, execution=execution,
                        execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                    ),
                    default=_notification(
                        execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                        outcome=outcome,
                        plan=retained_plan,
                        plan_path=retained_plan_path,
                        config_path=config_path,
                        state_directory=state_path,
                        execution=execution,
                    ),
                    notification_mode=notification_mode,
                    execution_revision=str(execution.get("revision") or ""),
                )
                _attach_notification(record, notification, notification_mode)
                state = append_run_record(
                    state,
                    record,
                    successful=True,
                    latest_plan=None,
                )
                state_file = save_run_state(state_path, state)
                return _report(
                    config=config,
                    record=record,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=retained_plan,
                    state_file=state_file,
                    state_directory=state_path,
                )
            if same_revision and retry_deferred:
                outcome = "retry_deferred"
                record = _record(
                    run_id=run_id,
                    completed_at=completed_at,
                    outcome=outcome,
                    revision=revision,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=retained_plan,
                    plan_path=retained_plan_path,
                    provider_usage=None,
                    advisory_coverage=(
                        previous.get("advisory_coverage")
                        if isinstance(previous.get("advisory_coverage"), Mapping)
                        else None
                    ),
                    execution=execution,
                    schedule=schedule,
                    supersedes_plan_id=previous_plan_data.get("supersedes_plan_id"),
                    advisory_policy_id=advisory_policy_id,
                    retry_at=retry_at,
                )
                notification = _suppressed_notification(
                    previous,
                    current_candidate=_notification(
                        outcome="action_required", plan=retained_plan, plan_path=retained_plan_path,
                        config_path=config_path, state_directory=state_path, execution=execution,
                        execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                    ),
                    default=_notification(
                        execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                        outcome=outcome,
                        plan=retained_plan,
                        plan_path=retained_plan_path,
                        config_path=config_path,
                        state_directory=state_path,
                        execution=execution,
                    ),
                    notification_mode=notification_mode,
                    execution_revision=str(execution.get("revision") or ""),
                )
                _attach_notification(record, notification, notification_mode)
                state = append_run_record(
                    state,
                    record,
                    successful=False,
                    latest_plan=None,
                )
                state_file = save_run_state(state_path, state)
                return _report(
                    config=config,
                    record=record,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=retained_plan,
                    state_file=state_file,
                    state_directory=state_path,
                )

            inspection_summary = inspection.get("summary") or {}
            no_work = (
                inspection.get("status") == "complete"
                and int(inspection_summary.get("open_pull_request_count") or 0) == 0
                and int(inspection_summary.get("open_alert_count") or 0) == 0
            )
            if no_work:
                outcome = "no_work"
                latest_plan = {
                    "plan_id": None,
                    "supersedes_plan_id": supersedes_plan_id,
                    "invalidated_plan_ids": invalidated_plan_ids,
                    "lineage_overflow": lineage_overflow,
                    "revoked_reason": "no_work",
                    "evidence_revision": revision,
                    "recorded_at": completed_at,
                }
                record = _record(
                    run_id=run_id,
                    completed_at=completed_at,
                    outcome=outcome,
                    revision=revision,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=None,
                    plan_path=None,
                    provider_usage=None,
                    advisory_coverage=None,
                    execution=execution,
                    schedule=schedule,
                    supersedes_plan_id=supersedes_plan_id,
                    advisory_policy_id=advisory_policy_id,
                )
                _attach_notification(
                    record,
                    _notification(
                        execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                        outcome=outcome,
                        plan=None,
                        plan_path=None,
                        config_path=config_path,
                        state_directory=state_path,
                        execution=execution,
                    ),
                    notification_mode,
                )
                state = append_run_record(
                    state,
                    record,
                    successful=True,
                    latest_plan=latest_plan,
                )
                state_file = save_run_state(state_path, state)
                prune_plan_artifacts(
                    state_path,
                    [
                        str(item.get("plan_id"))
                        for item in state.get("history") or []
                        if isinstance(item, Mapping) and isinstance(item.get("plan_id"), str)
                    ],
                )
                return _report(
                    config=config,
                    record=record,
                    inspection=inspection,
                    synopsis=synopsis,
                    plan=None,
                    state_file=state_file,
                    state_directory=state_path,
                )

            advisory_report = advise_from_inspection(config, inspection, provider)
            plan = plan_from_advisory(advisory_report)
            coverage = _advisory_coverage(advisory_report)
            usage = _provider_usage(advisory_report)
            outcome = (
                "partial"
                if inspection.get("status") != "complete"
                or advisory_report.get("status") != "complete"
                or plan.get("status") != "complete"
                else "action_required"
            )
            plan_id = plan.get("plan_id")
            if not isinstance(plan_id, str) or not plan_id.startswith("plan_"):
                raise ReconciliationError("Reconciler produced an invalid plan identity.")
            plan_path = save_private_json(
                state_path,
                Path("plans") / f"{plan_id}.json",
                plan,
            )
            latest_plan = {
                "plan_id": plan_id,
                "path": str(plan_path),
                "status": plan.get("status"),
                "evidence_revision": revision,
                "supersedes_plan_id": supersedes_plan_id,
                "invalidated_plan_ids": invalidated_plan_ids,
                "lineage_overflow": lineage_overflow,
                "advisory_policy_id": advisory_policy_id,
                "recorded_at": completed_at,
            }
            record = _record(
                run_id=run_id,
                completed_at=completed_at,
                outcome=outcome,
                revision=revision,
                inspection=inspection,
                synopsis=synopsis,
                plan=plan,
                plan_path=plan_path,
                provider_usage=usage,
                advisory_coverage=coverage,
                execution=execution,
                schedule=schedule,
                supersedes_plan_id=supersedes_plan_id,
                advisory_policy_id=advisory_policy_id,
                retry_at=(
                    _timestamp(now + timedelta(seconds=retry_after_seconds))
                    if outcome == "partial"
                    else None
                ),
            )
            _attach_notification(
                record,
                _notification(
                    execution_enabled=config.operation.autonomy == "approval_required" and config.operation.mutation_enabled,
                    outcome=outcome,
                    plan=plan,
                    plan_path=plan_path,
                    config_path=config_path,
                    state_directory=state_path,
                    execution=execution,
                ),
                notification_mode,
            )
            state = append_run_record(
                state,
                record,
                successful=outcome == "action_required",
                latest_plan=latest_plan,
            )
            state_file = save_run_state(state_path, state)
            prune_plan_artifacts(
                state_path,
                [
                    str(item.get("plan_id"))
                    for item in state.get("history") or []
                    if isinstance(item, Mapping) and isinstance(item.get("plan_id"), str)
                ],
            )
            return _report(
                config=config,
                record=record,
                inspection=inspection,
                synopsis=synopsis,
                plan=plan,
                state_file=state_file,
                state_directory=state_path,
            )
    except (RuntimeStateError, ExecutionError) as exc:
        raise ReconciliationError(str(exc)) from exc


def _report(
    *,
    config: WatcherConfig,
    record: Mapping[str, Any],
    inspection: Mapping[str, Any],
    synopsis: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    state_file: Path,
    state_directory: Path,
) -> dict[str, Any]:
    outcome = str(record.get("outcome") or "failed")
    status = "partial" if outcome in {"partial", "failed"} else "complete"
    report_path = state_directory / "latest-run.json"
    report = {
        "schema_version": RUN_SCHEMA_VERSION,
        "mode": "run",
        "supervisor": {"mode": config.supervisor.mode, "model": config.supervisor.model,
            "reasoning_effort": config.supervisor.reasoning_effort,
            "assurance": "saved_policy_only", "github_mutations_authorized": False},
        "generated_at": record.get("completed_at"),
        "run_id": record.get("run_id"),
        "status": status,
        "outcome": outcome,
        "mutation_authorized": False,
        "capabilities": {
            "model_calls": bool((record.get("provider_usage") or {}).get("call_count")),
            "persistent_local_writes": True,
            "github_mutations": False,
            "notifications": False,
        },
        "summary": copy.deepcopy(dict(record.get("queue") or {})),
        "evidence_revision": record.get("evidence_revision"),
        "queue_synopsis": copy.deepcopy(dict(synopsis)),
        "advisory_coverage": copy.deepcopy(dict(record.get("advisory_coverage") or {})),
        "provider_usage": copy.deepcopy(dict(record.get("provider_usage") or {})),
        "execution_observation": copy.deepcopy(
            dict(record.get("execution_observation") or {})
        ),
        "schedule_observation": copy.deepcopy(
            dict(record.get("schedule_observation") or {})
        ),
        "failure": copy.deepcopy(dict(record.get("failure") or {})),
        "plan": copy.deepcopy(dict(plan)) if plan is not None else None,
        "plan_lineage": {
            "plan_id": record.get("plan_id"),
            "plan_path": record.get("plan_path"),
            "supersedes_plan_id": record.get("supersedes_plan_id"),
        },
        "retry_at": record.get("retry_at"),
        "notification": {
            **copy.deepcopy(dict(record.get("notification") or {})),
            "delivery": copy.deepcopy(
                dict(record.get("notification_delivery") or {})
            ),
        },
        "state_path": str(state_file),
        "report_path": str(report_path),
        "inspection": copy.deepcopy(dict(inspection)),
    }
    save_private_json(state_directory, Path("latest-run.json"), report)
    return report


def record_notification_delivery(
    state_directory: Path,
    run_id: str,
    delivery: Mapping[str, Any],
    *,
    original_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach acceptance to its durable run without replacing newer evidence.

    The CLI supplies its original report so concurrent collection cannot change
    which run it prints. Other callers receive a delivery receipt if that report
    is no longer latest; persisted history always remains bound by run ID.
    """

    status = delivery.get("status")
    if status not in {"delivered", "failed"}:
        raise ReconciliationError("Notification delivery status must be delivered or failed.")
    if delivery.get("mode") != "desktop":
        raise ReconciliationError("Only desktop delivery results are currently supported.")
    state_path = state_directory.expanduser()
    try:
        with run_lock(state_path):
            state = load_run_state(state_path)
            history = [dict(item) for item in state.get("history") or []]
            matches = [index for index, item in enumerate(history) if item.get("run_id") == run_id]
            if len(matches) != 1:
                raise ReconciliationError(
                    "Notification result does not match exactly one durable run record."
                )
            delivery_record = copy.deepcopy(dict(delivery))
            history[matches[0]]["notification_delivery"] = delivery_record
            updated_state = dict(state)
            updated_state["history"] = history
            for field in ("last_attempt", "last_success"):
                value = updated_state.get(field)
                if isinstance(value, Mapping) and value.get("run_id") == run_id:
                    updated = dict(value)
                    updated["notification_delivery"] = delivery_record
                    updated_state[field] = updated
            report = load_private_json(state_path, Path("latest-run.json"))
            latest_attempt = state.get("last_attempt")
            if (not isinstance(latest_attempt, Mapping)
                    or report.get("run_id") != latest_attempt.get("run_id")):
                raise ReconciliationError(
                    "Latest run report changed before notification delivery was recorded."
                )
            latest_matches = report.get("run_id") == run_id
            if not latest_matches:
                if original_report is None:
                    save_run_state(state_path, updated_state)
                    return {"schema_version": 1, "mode": "notification_delivery", "status": "recorded",
                            "run_id": run_id, "delivery": delivery_record, "latest_report_updated": False}
                record = history[matches[0]]
                if (original_report.get("run_id") != run_id or original_report.get("mode") != "run"
                        or original_report.get("summary") != record.get("queue")
                        or not isinstance(original_report.get("notification"), Mapping)
                        or {key: value for key, value in original_report["notification"].items() if key != "delivery"}
                        != record.get("notification")):
                    raise ReconciliationError("Original notification report does not match its durable run.")
                report = copy.deepcopy(dict(original_report))
            notification = report.get("notification")
            if not isinstance(notification, Mapping):
                raise ReconciliationError("Latest run report has malformed notification state.")
            report["notification"] = {
                **dict(notification),
                "delivery": delivery_record,
            }
            capabilities = report.get("capabilities")
            if not isinstance(capabilities, Mapping):
                raise ReconciliationError("Latest run report has malformed capabilities.")
            report["capabilities"] = {
                **dict(capabilities),
                "notifications": status == "delivered",
            }
            save_run_state(state_path, updated_state)
            if latest_matches:
                save_private_json(state_path, Path("latest-run.json"), report)
            return report
    except RuntimeStateError as exc:
        raise ReconciliationError(str(exc)) from exc
