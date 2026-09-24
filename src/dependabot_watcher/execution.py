from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, cast

from .config import AdvisoryConfig, RepositoryConfig, WatcherConfig
from .github import ExecutionGitHubClient, GitHubClient, GitHubError
from .planning import (
    PLAN_SCHEMA_VERSION,
    PlanningError,
    plan,
    validate_plan_identities,
)


EXECUTION_SCHEMA_VERSION = 1
_AUDIT_FILE = "execution-audit.jsonl"
_LOCK_FILE = "execution.lock"
_MAX_AUDIT_BYTES = 10_000_000
_AUDIT_EVENT_TYPES = {
    "execution_started",
    "execution_succeeded",
    "execution_failed",
    "execution_reconciled",
}


class ExecutionError(RuntimeError):
    """Raised when an action is not safely executable."""


def default_state_directory() -> Path:
    override = os.getenv("DEPENDABOT_WATCHER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "dependabot-watcher"
    state_home = Path(os.getenv("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return state_home / "dependabot-watcher"


def load_plan(path: Path) -> dict[str, Any]:
    expanded = path.expanduser()
    try:
        if expanded.stat().st_size > 50_000_000:
            raise ExecutionError("Execution plan exceeds the 50 MB safety ceiling.")
        raw = expanded.read_text(encoding="utf-8")
    except ExecutionError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ExecutionError(f"Could not read plan file {expanded}.") from exc
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ExecutionError("Execution plan must be valid JSON from `plan --format json`.") from exc
    if not isinstance(value, dict):
        raise ExecutionError("Execution plan must be a JSON object.")
    if value.get("schema_version") != PLAN_SCHEMA_VERSION or value.get("mode") != "plan":
        raise ExecutionError("Execution plan has an unsupported schema or mode.")
    if value.get("status") != "complete":
        raise ExecutionError("Execution requires a complete plan; partial plans fail closed.")
    if value.get("mutation_authorized") is not False:
        raise ExecutionError("Saved plan contains invalid mutation authority.")
    if not isinstance(value.get("repositories"), list):
        raise ExecutionError("Execution plan has malformed repository records.")
    _validate_plan_integrity(value)
    return value


def _validate_plan_integrity(report: Mapping[str, Any]) -> None:
    try:
        validate_plan_identities(report)
    except PlanningError as exc:
        raise ExecutionError(f"Execution plan identity validation failed: {exc}") from exc


def _find_step(
    report: Mapping[str, Any], step_id: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for repository in report.get("repositories") or []:
        if not isinstance(repository, Mapping):
            raise ExecutionError("Execution plan has a malformed repository record.")
        for step in repository.get("recommended_order") or []:
            if not isinstance(step, Mapping):
                raise ExecutionError("Execution plan has a malformed step record.")
            if step.get("step_id") == step_id:
                matches.append((repository, step))
    if len(matches) != 1:
        raise ExecutionError(f"Plan must contain exactly one step named {step_id!r}.")
    return matches[0]


def _find_target_step(
    report: Mapping[str, Any], repository_name: str, pull_request: int
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for repository in report.get("repositories") or []:
        if not isinstance(repository, Mapping) or repository.get("repository") != repository_name:
            continue
        for step in repository.get("recommended_order") or []:
            identity = step.get("identity") if isinstance(step, Mapping) else None
            if (
                isinstance(step, Mapping)
                and isinstance(identity, Mapping)
                and step.get("kind") == "pull_request"
                and identity.get("number") == pull_request
            ):
                matches.append((repository, step))
    if len(matches) != 1:
        raise ExecutionError("Fresh plan no longer contains the exact pull-request target.")
    return matches[0]


def _repository_config(config: WatcherConfig, name: str) -> RepositoryConfig:
    matches = [item for item in config.repositories if item.name == name]
    if len(matches) != 1:
        raise ExecutionError(f"Repository {name!r} is not uniquely configured.")
    return matches[0]


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionError(f"Execution plan is missing {field}.")
    return value


def _binding(
    repository_plan: Mapping[str, Any],
    step: Mapping[str, Any],
    repository_config: RepositoryConfig,
) -> dict[str, Any]:
    if repository_plan.get("status") != "complete":
        raise ExecutionError("Execution requires complete repository evidence.")
    if repository_plan.get("mutation_authorized") is not False:
        raise ExecutionError("Repository plan contains invalid mutation authority.")
    if step.get("mutation_authorized") is not False:
        raise ExecutionError("Plan step contains invalid mutation authority.")
    if step.get("kind") != "pull_request" or step.get("action") != "validate_then_merge":
        raise ExecutionError("Only validate_then_merge pull-request steps are executable.")
    if step.get("deterministic_classification") != "baseline_candidate":
        raise ExecutionError("Execution requires a deterministic baseline candidate.")
    if step.get("blockers") != []:
        raise ExecutionError("Execution refuses a step with deterministic blockers.")
    workflow = repository_plan.get("workflow_profile")
    if not isinstance(workflow, Mapping):
        raise ExecutionError("Execution plan has malformed repository workflow evidence.")
    expected_workflow = {
        "merge_method": repository_config.workflow.merge_method,
        "promotion_branch": repository_config.workflow.promotion_branch,
        "required_checks": list(repository_config.workflow.required_checks),
        "deployment_environments": list(
            repository_config.workflow.deployment_environments
        ),
        "validation_notes": list(repository_config.workflow.validation_notes),
    }
    observed_workflow = {
        "merge_method": workflow.get("merge_method"),
        "promotion_branch": workflow.get("promotion_branch"),
        "required_checks": list(workflow.get("required_checks") or []),
        "deployment_environments": list(
            workflow.get("deployment_environments") or []
        ),
        "validation_notes": list(workflow.get("validation_notes") or []),
    }
    if observed_workflow != expected_workflow:
        raise ExecutionError(
            "Repository workflow configuration changed after the saved plan; create a new plan."
        )
    identity = step.get("identity")
    freshness = step.get("freshness")
    if not isinstance(identity, Mapping) or not isinstance(freshness, Mapping):
        raise ExecutionError("Execution step has malformed identity or freshness evidence.")
    repository = _required_text(identity.get("repository"), "repository identity")
    if repository != repository_config.name or repository_plan.get("repository") != repository:
        raise ExecutionError("Execution repository identity does not match configuration.")
    number = identity.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ExecutionError("Execution plan has an invalid pull-request number.")
    expected_base = _required_text(identity.get("expected_base"), "expected base")
    observed_base = _required_text(identity.get("observed_base"), "observed base")
    if expected_base != repository_config.default_branch or observed_base != expected_base:
        raise ExecutionError("Execution target does not match the configured base branch.")
    head_sha = _required_text(identity.get("head_sha"), "head SHA")
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_sha):
        raise ExecutionError("Execution requires a full Git commit SHA.")
    if freshness.get("head_sha") != head_sha:
        raise ExecutionError("Execution plan has contradictory head-SHA evidence.")
    if not isinstance(freshness.get("base_sha"), str):
        raise ExecutionError("Execution plan has no base-SHA freshness evidence.")
    merge_method = repository_config.workflow.merge_method
    if merge_method not in {"merge", "squash", "rebase"}:
        raise ExecutionError(
            "Execution requires an explicit repository merge_method: merge, squash, or rebase."
        )
    if not repository_config.workflow.required_checks:
        raise ExecutionError("Execution requires at least one configured required check.")
    if (
        repository_config.workflow.promotion_branch
        or repository_config.workflow.deployment_environments
        or repository_config.workflow.validation_notes
    ):
        raise ExecutionError(
            "This initial execution surface cannot verify promotion, deployment, or manual validation gates."
        )
    status_checks = step.get("status_checks")
    if not isinstance(status_checks, list):
        raise ExecutionError("Execution plan has malformed status-check evidence.")
    observed: dict[str, str] = {}
    for item in status_checks:
        if not isinstance(item, Mapping):
            raise ExecutionError("Execution plan has malformed status-check evidence.")
        name = _required_text(item.get("name"), "status-check name")
        observed[name.casefold()] = str(item.get("state") or "unknown")
    missing = [
        name
        for name in repository_config.workflow.required_checks
        if observed.get(name.casefold()) != "passing"
    ]
    if missing:
        raise ExecutionError(
            "Configured required checks are missing or not passing: " + ", ".join(missing) + "."
        )
    return {
        "repository_plan_id": _required_text(repository_plan.get("plan_id"), "repository plan ID"),
        "step_id": _required_text(step.get("step_id"), "step ID"),
        "repository": repository,
        "pull_request": number,
        "expected_base": expected_base,
        "head_sha": head_sha,
        "merge_method": merge_method,
    }


def approval_token(binding: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"contract": "dependabot-watcher-execution-approval-v1", **dict(binding)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "approve_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _action_id(binding: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(binding), sort_keys=True, separators=(",", ":"))
    return "action_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _material_snapshot(
    repository_plan: Mapping[str, Any], step: Mapping[str, Any]
) -> dict[str, Any]:
    identity_value = step.get("identity")
    freshness_value = step.get("freshness")
    evidence_value = repository_plan.get("evidence_snapshot")
    alert_value = repository_plan.get("alert_snapshot")
    workflow_value = repository_plan.get("workflow_profile")
    identity = identity_value if isinstance(identity_value, Mapping) else {}
    freshness = freshness_value if isinstance(freshness_value, Mapping) else {}
    evidence = evidence_value if isinstance(evidence_value, Mapping) else {}
    alert_snapshot = alert_value if isinstance(alert_value, Mapping) else {}
    workflow = workflow_value if isinstance(workflow_value, Mapping) else {}
    return {
        "repository": repository_plan.get("repository"),
        "repository_status": repository_plan.get("status"),
        "workflow_profile": {
            "merge_method": workflow.get("merge_method"),
            "promotion_branch": workflow.get("promotion_branch"),
            "required_checks": list(workflow.get("required_checks") or []),
            "deployment_environments": list(
                workflow.get("deployment_environments") or []
            ),
            "validation_notes": list(workflow.get("validation_notes") or []),
        },
        "expected_base": repository_plan.get("expected_base") or identity.get("expected_base"),
        "repository_base_sha": evidence.get("base_sha") or freshness.get("base_sha"),
        "alert_snapshot_digest": alert_snapshot.get("digest")
        or freshness.get("alert_snapshot_digest"),
        "kind": step.get("kind"),
        "action": step.get("action"),
        "deterministic_classification": step.get("deterministic_classification"),
        "blockers": step.get("blockers"),
        "identity": {
            "repository": identity.get("repository"),
            "number": identity.get("number"),
            "expected_base": identity.get("expected_base"),
            "observed_base": identity.get("observed_base"),
            "head_sha": identity.get("head_sha"),
        },
        "freshness": {
            "base_sha": freshness.get("base_sha"),
            "head_sha": freshness.get("head_sha"),
            "pr_updated_at": freshness.get("pr_updated_at"),
            "alert_snapshot_digest": freshness.get("alert_snapshot_digest"),
        },
        "status_checks": step.get("status_checks"),
    }


def _fresh_preflight(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    saved_plan: Mapping[str, Any],
    step_id: str,
) -> tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    _validate_plan_integrity(saved_plan)
    saved_repository, saved_step = _find_step(saved_plan, step_id)
    identity = saved_step.get("identity")
    if not isinstance(identity, Mapping):
        raise ExecutionError("Execution step has malformed identity evidence.")
    repository_name = _required_text(identity.get("repository"), "repository identity")
    repository_config = _repository_config(config, repository_name)
    saved_binding = _binding(saved_repository, saved_step, repository_config)

    current_state = client.fetch_pull_request_state(
        repository_name, int(saved_binding["pull_request"])
    )
    if current_state.get("headRefOid") != saved_binding["head_sha"]:
        raise ExecutionError("Pull-request head changed after the saved plan; create a new plan.")
    if current_state.get("baseRefName") != saved_binding["expected_base"]:
        raise ExecutionError("Pull-request base changed after the saved plan; create a new plan.")
    if str(current_state.get("state") or "").upper() == "MERGED":
        return saved_binding, saved_repository, {"already_applied": True, **current_state}
    if str(current_state.get("state") or "").upper() != "OPEN":
        raise ExecutionError("The planned pull request is no longer open.")

    deterministic_config = replace(
        config,
        repositories=(repository_config,),
        advisory=AdvisoryConfig(),
    )
    fresh_report = plan(deterministic_config, cast(GitHubClient, client))
    if fresh_report.get("status") != "complete":
        raise ExecutionError("Fresh reinspection is partial; execution fails closed.")
    _validate_plan_integrity(fresh_report)
    fresh_repository, fresh_step = _find_target_step(
        fresh_report, repository_name, int(saved_binding["pull_request"])
    )
    fresh_binding = _binding(fresh_repository, fresh_step, repository_config)
    if _material_snapshot(fresh_repository, fresh_step) != _material_snapshot(
        saved_repository, saved_step
    ):
        raise ExecutionError("Repository evidence changed after the saved plan; create a new plan.")
    if any(
        fresh_binding[field] != saved_binding[field]
        for field in (
            "repository",
            "pull_request",
            "expected_base",
            "head_sha",
            "merge_method",
        )
    ):
        raise ExecutionError("Execution binding changed after the saved plan; create a new plan.")
    return saved_binding, fresh_repository, fresh_step


def preflight_execution(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    saved_plan: Mapping[str, Any],
    step_id: str,
) -> dict[str, Any]:
    if config.operation.autonomy != "approval_required" or not config.operation.mutation_enabled:
        raise ExecutionError(
            "Execution is disabled; configure autonomy='approval_required' and mutation_enabled=true."
        )
    binding, _, fresh_step = _fresh_preflight(config, client, saved_plan, step_id)
    already_applied = bool(fresh_step.get("already_applied"))
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "mode": "execute_preflight",
        "generated_at": _timestamp(),
        "status": "already_applied" if already_applied else "ready",
        "mutation_performed": False,
        "mutation_authorized": False,
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "binding": binding,
        "action_id": _action_id(binding),
        "approval_token": None if already_applied else approval_token(binding),
        "next": (
            "No action is needed; GitHub reports this exact head as already merged."
            if already_applied
            else "Repeat execute with the displayed approval token to authorize this exact action."
        ),
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _prepare_state_directory(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        if expanded.is_symlink():
            raise ExecutionError("Execution state directory must not be a symbolic link.")
        if expanded.exists() and not expanded.is_dir():
            raise ExecutionError("Execution state path must be a directory.")
        expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            expanded,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            state = os.fstat(descriptor)
            if not stat.S_ISDIR(state.st_mode):
                raise ExecutionError("Execution state path must be a directory.")
            if hasattr(os, "getuid") and state.st_uid != os.getuid():
                raise ExecutionError("Execution state directory must be owned by the current user.")
            if stat.S_IMODE(state.st_mode) & 0o077:
                raise ExecutionError(
                    "Execution state directory permissions must not allow group or other access."
                )
        finally:
            os.close(descriptor)
    except ExecutionError:
        raise
    except OSError as exc:
        raise ExecutionError(
            f"Execution state directory cannot be safely accessed at {expanded}."
        ) from exc
    return expanded


def _validate_private_regular_file(descriptor: int, label: str) -> os.stat_result:
    state = os.fstat(descriptor)
    if not stat.S_ISREG(state.st_mode):
        raise ExecutionError(f"{label} must be a regular file.")
    if hasattr(os, "getuid") and state.st_uid != os.getuid():
        raise ExecutionError(f"{label} must be owned by the current user.")
    if stat.S_IMODE(state.st_mode) & 0o077:
        raise ExecutionError(f"{label} permissions must not allow group or other access.")
    return state


@contextmanager
def _execution_lock(state_directory: Path) -> Iterator[None]:
    lock_path = state_directory / _LOCK_FILE
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise ExecutionError(
            f"Another execution may be active; inspect and recover the lock at {lock_path}."
        ) from exc
    except OSError as exc:
        raise ExecutionError(f"Execution lock cannot be safely created at {lock_path}.") from exc
    try:
        _validate_private_regular_file(descriptor, "Execution lock")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(
                    json.dumps({"pid": os.getpid(), "created_at": _timestamp()})
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ExecutionError(
                f"Execution lock cannot be safely written at {lock_path}."
            ) from exc
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _append_audit(
    state_directory: Path,
    event: Mapping[str, Any],
    *,
    reserve_bytes: int = 0,
) -> Path:
    audit_path = state_directory / _AUDIT_FILE
    try:
        descriptor = os.open(
            audit_path,
            os.O_APPEND
            | os.O_CREAT
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        raise ExecutionError(f"Execution audit cannot be safely opened at {audit_path}.") from exc
    try:
        state = _validate_private_regular_file(descriptor, "Execution audit")
        payload = _audit_payload(event)
        if state.st_size + len(payload) + reserve_bytes > _MAX_AUDIT_BYTES:
            raise ExecutionError(
                "Execution audit reached its safety ceiling; archive it deliberately before "
                "attempting another action."
            )
        _write_all(descriptor, payload)
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise ExecutionError("Execution audit could not be synchronized safely.") from exc
    finally:
        os.close(descriptor)
    return audit_path


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except OSError as exc:
            raise ExecutionError("Execution audit could not be written safely.") from exc
        if written <= 0:
            raise ExecutionError("Execution audit could not be written completely.")
        remaining = remaining[written:]


def _audit_payload(event: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _read_audit_events(state_directory: Path) -> list[dict[str, Any]]:
    audit_path = state_directory / _AUDIT_FILE
    try:
        descriptor = os.open(
            audit_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ExecutionError("Execution audit cannot be safely read for reconciliation.") from exc
    try:
        state = _validate_private_regular_file(descriptor, "Execution audit")
        if state.st_size > _MAX_AUDIT_BYTES:
            raise ExecutionError("Execution audit cannot be safely read for reconciliation.")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            lines = handle.read().splitlines()
    except (OSError, UnicodeError) as exc:
        raise ExecutionError("Execution audit cannot be read for reconciliation.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise ExecutionError("Execution audit contains malformed JSON.") from exc
        if (
            not isinstance(event, dict)
            or event.get("schema_version") != EXECUTION_SCHEMA_VERSION
            or event.get("event_type") not in _AUDIT_EVENT_TYPES
            or not isinstance(event.get("event_id"), str)
            or not isinstance(event.get("occurred_at"), str)
            or not isinstance(event.get("action_id"), str)
            or not isinstance(event.get("binding"), dict)
            or not isinstance(event.get("outcome"), str)
        ):
            raise ExecutionError("Execution audit contains a malformed event record.")
        events.append(event)
    return events


def validate_audit_history(state_directory: Path) -> None:
    """Fail closed when an existing bounded audit is malformed or unsafe."""

    _read_audit_events(state_directory)


def _audit_has_success(state_directory: Path, action_id: str) -> bool:
    return any(
        event.get("action_id") == action_id
        and event.get("event_type") == "execution_succeeded"
        for event in reversed(_read_audit_events(state_directory))
    )


def _audit_event(event_type: str, binding: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "event_id": f"event_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": _timestamp(),
        "action_id": _action_id(binding),
        "binding": dict(binding),
        **extra,
    }


def _terminal_audit_reserve(binding: Mapping[str, Any]) -> int:
    success = _audit_event(
        "execution_succeeded",
        binding,
        outcome="merged",
        merge_commit_sha="f" * 64,
    )
    failure = _audit_event(
        "execution_failed",
        binding,
        outcome="github_error",
        recovery="Reinspect GitHub state and create a fresh plan before retrying.",
    )
    return max(len(_audit_payload(success)), len(_audit_payload(failure)))


def _sanitized_merge_result(result: Mapping[str, Any]) -> dict[str, Any]:
    sha = result.get("merge_commit_sha")
    safe_sha = (
        sha
        if isinstance(sha, str)
        and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha)
        else None
    )
    return {
        "merged": result.get("merged") is True,
        "message": str(result.get("message") or "Pull request merged.")[:2000],
        "merge_commit_sha": safe_sha,
    }


def execute_approved(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    saved_plan: Mapping[str, Any],
    step_id: str,
    supplied_approval_token: str,
    *,
    state_directory: Path | None = None,
) -> dict[str, Any]:
    if config.operation.autonomy != "approval_required" or not config.operation.mutation_enabled:
        raise ExecutionError(
            "Execution is disabled; configure autonomy='approval_required' and mutation_enabled=true."
        )
    _validate_plan_integrity(saved_plan)
    saved_repository, saved_step = _find_step(saved_plan, step_id)
    saved_identity = saved_step.get("identity")
    if not isinstance(saved_identity, Mapping):
        raise ExecutionError("Execution step has malformed identity evidence.")
    repository_name = _required_text(
        saved_identity.get("repository"), "repository identity"
    )
    repository_config = _repository_config(config, repository_name)
    saved_binding = _binding(saved_repository, saved_step, repository_config)
    if not hmac.compare_digest(
        supplied_approval_token, approval_token(saved_binding)
    ):
        raise ExecutionError("Approval token does not authorize the current exact action.")

    state_path = _prepare_state_directory(state_directory or default_state_directory())
    with _execution_lock(state_path):
        validate_audit_history(state_path)
        binding, _, current = _fresh_preflight(config, client, saved_plan, step_id)
        if current.get("already_applied"):
            prior_success = _audit_has_success(state_path, _action_id(binding))
            audit_path = _append_audit(
                state_path,
                _audit_event(
                    "execution_reconciled",
                    binding,
                    outcome="already_applied",
                    prior_success_recorded=prior_success,
                ),
            )
            return {
                "schema_version": EXECUTION_SCHEMA_VERSION,
                "mode": "execute",
                "generated_at": _timestamp(),
                "status": "already_applied",
                "mutation_performed": False,
                "mutation_authorized": True,
                "approval_token": None,
                "capabilities": {
                    "model_calls": False,
                    "persistent_local_writes": True,
                    "github_mutations": False,
                    "notifications": False,
                },
                "binding": binding,
                "action_id": _action_id(binding),
                "audit_path": str(audit_path),
                "next": "No action is needed; GitHub reports this exact head as already merged.",
            }
        expected_token = approval_token(binding)
        if not hmac.compare_digest(supplied_approval_token, expected_token):
            raise ExecutionError("Action changed during the final recheck; approval is invalid.")

        audit_path = _append_audit(
            state_path,
            _audit_event("execution_started", binding, outcome="pending"),
            reserve_bytes=_terminal_audit_reserve(binding),
        )
        try:
            raw_result = client.merge_pull_request(
                str(binding["repository"]),
                int(binding["pull_request"]),
                head_sha=str(binding["head_sha"]),
                merge_method=str(binding["merge_method"]),
            )
        except GitHubError:
            _append_audit(
                state_path,
                _audit_event(
                    "execution_failed",
                    binding,
                    outcome="github_error",
                    recovery="Reinspect GitHub state and create a fresh plan before retrying.",
                ),
            )
            raise
        result = _sanitized_merge_result(raw_result)
        _append_audit(
            state_path,
            _audit_event(
                "execution_succeeded",
                binding,
                outcome="merged",
                merge_commit_sha=result.get("merge_commit_sha"),
            ),
        )
        return {
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "mode": "execute",
            "generated_at": _timestamp(),
            "status": "merged",
            "mutation_performed": True,
            "mutation_authorized": True,
            "capabilities": {
                "model_calls": False,
                "persistent_local_writes": True,
                "github_mutations": True,
                "notifications": False,
            },
            "binding": binding,
            "action_id": _action_id(binding),
            "result": result,
            "audit_path": str(audit_path),
            "recovery": [
                "Reinspect the repository before taking another action.",
                "Verify configured downstream checks or deployment state outside this command.",
            ],
        }
