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
from typing import Any, Callable, Iterator, Mapping, cast

from .advisory import advisory_action_policy
from .config import AdvisoryConfig, ConfigError, RepositoryConfig, WatcherConfig, load_config, repository_names_equal
from .github import (
    ExecutionGitHubClient,
    GitHubClient,
    GitHubError,
    public_github_error_message,
)
from .planning import (
    PLAN_SCHEMA_VERSION,
    PlanningError,
    plan,
    validate_plan_identities,
)
from .runtime_state import (
    RuntimeStateError,
    default_state_directory,
    ensure_private_directory,
    load_run_state,
    run_lock,
    secure_private_file,
    validate_private_file,
)
from .review_queue import ReviewQueueError
from .local_sync import validate_control_locations
from .supervisor_action import require_supervisor_recommendation


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
    if value.get("status") not in {"complete", "partial"}:
        raise ExecutionError("Execution plan has no safely usable planning status.")
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
    matches = [item for item in config.repositories if repository_names_equal(item.name, name)]
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
    if not repository_names_equal(repository, repository_config.name) or repository_plan.get("repository") != repository:
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
    observed: dict[str, Mapping[str, Any]] = {}
    observed_casefolded: set[str] = set()
    for item in status_checks:
        if not isinstance(item, Mapping):
            raise ExecutionError("Execution plan has malformed status-check evidence.")
        name = _required_text(item.get("name"), "status-check name")
        folded_name = name.casefold()
        if folded_name in observed_casefolded:
            raise ExecutionError(
                "Execution plan has ambiguous duplicate status-check evidence for "
                f"{name!r}."
            )
        observed_casefolded.add(folded_name)
        observed[name] = item
    missing = [
        name
        for name in repository_config.workflow.required_checks
        if not _required_check_succeeded(observed.get(name))
    ]
    if missing:
        raise ExecutionError(
            "Configured required checks are missing or not explicitly successful: "
            + ", ".join(missing)
            + "."
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


def _required_check_succeeded(check: Mapping[str, Any] | None) -> bool:
    """Require a valid explicit GitHub success for an execution-gating check."""

    if check is None:
        return False
    kind = str(check.get("kind") or "")
    state = str(check.get("state") or "")
    status = str(check.get("status") or "").upper()
    conclusion = str(check.get("conclusion") or "").upper()
    if state != "passing":
        return False
    if kind == "check_run":
        return status == "COMPLETED" and conclusion == "SUCCESS"
    if kind == "status_context":
        return not status and conclusion == "SUCCESS"
    return False


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


def _require_saved_advice(
    config: WatcherConfig,
    saved_repository: Mapping[str, Any],
    saved_step: Mapping[str, Any],
) -> None:
    if not config.operation.require_advice_before_execution:
        return
    if config.advisory.mode == "off" or config.advisory.provider is None:
        raise ExecutionError("Execution policy requires enabled model advice.")
    action_policy = saved_repository.get("action_policy")
    policy = action_policy if isinstance(action_policy, Mapping) else {}
    expected_policy = advisory_action_policy(config)
    if (
        policy.get("advice_required") is not True
        or policy.get("advisory_mode") != config.advisory.mode
        or policy.get("provider") != config.advisory.provider
        or policy.get("policy_id") != expected_policy["policy_id"]
    ):
        raise ExecutionError(
            "Saved plan does not contain complete current advice for the configured action policy."
        )
    advice = saved_step.get("advice")
    if not isinstance(advice, Mapping):
        raise ExecutionError(
            "Execution policy requires successful current advice for this candidate."
        )
    if advice.get("assurance") not in {"triage", "review"}:
        raise ExecutionError("Saved candidate advice has no valid assurance level.")
    if advice.get("requires_review") is True:
        raise ExecutionError("Saved candidate advice still requires review.")
    if advice.get("recommended_action") != "merge_candidate":
        raise ExecutionError("Saved candidate advice does not recommend merge eligibility.")


def _fresh_preflight(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    saved_plan: Mapping[str, Any],
    step_id: str,
) -> tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    _validate_plan_integrity(saved_plan)
    saved_repository, saved_step = _find_step(saved_plan, step_id)
    _require_saved_advice(config, saved_repository, saved_step)
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
        operation=replace(
            config.operation,
            require_advice_before_execution=False,
        ),
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


def _decision_context(
    step: Mapping[str, Any], repository_config: RepositoryConfig,
    supervisor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity_value = step.get("identity")
    identity = identity_value if isinstance(identity_value, Mapping) else {}
    checks_value = step.get("status_checks")
    checks = checks_value if isinstance(checks_value, list) else []
    observed_checks: dict[str, str] = {}
    for item in checks:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            observed_checks[str(item["name"]).casefold()] = str(
                item.get("state") or "unknown"
            )
    advice_value = step.get("advice")
    advice = advice_value if isinstance(advice_value, Mapping) else None
    return {
        "title": str(identity.get("title") or f"PR #{identity.get('number') or 'unknown'}"),
        "classification": str(
            step.get("deterministic_classification") or "unknown"
        ),
        "decision_source": "supervised_policy" if supervisor else "model_informed_policy" if advice else "deterministic_policy",
        "model_used": advice is not None or supervisor is not None,
        "advice_assurance": advice.get("assurance") if advice else None,
        **({"supervisor": dict(supervisor)} if supervisor else {}),
        "required_checks": [
            {
                "name": name,
                "state": observed_checks.get(name.casefold(), "unknown"),
            }
            for name in repository_config.workflow.required_checks
        ],
    }


def _post_action_state(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    repository_config: RepositoryConfig,
) -> dict[str, Any]:
    deterministic_config = replace(
        config,
        repositories=(repository_config,),
        advisory=AdvisoryConfig(),
        operation=replace(
            config.operation,
            require_advice_before_execution=False,
        ),
    )
    try:
        refreshed = plan(deterministic_config, cast(GitHubClient, client))
        if refreshed.get("status") != "complete":
            raise ValueError("incomplete refreshed plan")
        repositories = refreshed.get("repositories")
        if not isinstance(repositories, list):
            raise ValueError("missing repositories")
        if (
            len(repositories) != 1
            or not isinstance(repositories[0], Mapping)
            or repositories[0].get("repository") != repository_config.name
        ):
            raise ValueError("unexpected refreshed repository scope")
        repository = repositories[0]
        if repository.get("status") != "complete":
            raise ValueError("incomplete refreshed repository")
        state = repository.get("state")
        recommended_order = repository.get("recommended_order")
        if not isinstance(state, Mapping) or not isinstance(recommended_order, list):
            raise ValueError("missing refreshed state")
        if any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("step_id"), str)
            or not isinstance(item.get("action"), str)
            or not isinstance(item.get("summary"), str)
            for item in recommended_order
        ):
            raise ValueError("invalid refreshed recommendation")
        count_fields = {
            "open_pull_requests": state.get("open_pull_request_count"),
            "open_alerts": state.get("open_alert_count"),
            "alerts_without_direct_open_pr_coverage": state.get(
                "unresolved_alert_count"
            ),
            "attention_required": state.get("attention_required_count"),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in count_fields.values()
        ):
            raise ValueError("invalid refreshed counts")
        next_step = recommended_order[0] if recommended_order else None
        return {
            "status": "complete",
            "observed_at": refreshed.get("generated_at"),
            "repository": repository_config.name,
            **count_fields,
            "recommended_steps": len(recommended_order),
            "next_step": (
                {
                    "action": next_step.get("action"),
                    "summary": next_step.get("summary"),
                }
                if next_step is not None
                else None
            ),
        }
    except Exception:
        # The remote mutation has already completed. Preserve that success while
        # making the failed follow-up observation explicit and actionable.
        return {
            "status": "unavailable",
            "repository": repository_config.name,
            "reason": "Post-action reinspection was incomplete or unavailable.",
            "next": "Run `dependabot-watcher inspect` and `dependabot-watcher plan` before another action.",
        }


def preflight_execution(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    saved_plan: Mapping[str, Any],
    step_id: str,
    *,
    state_directory: Path | None = None,
    config_path: Path | None = None,
    completion_controller: bool = False,
) -> dict[str, Any]:
    validate_control_locations(config, config_path, state_directory or default_state_directory())
    if config.operation.autonomy != "approval_required" or not config.operation.mutation_enabled:
        raise ExecutionError(
            "Execution is disabled; configure autonomy='approval_required' and mutation_enabled=true."
        )
    _validate_plan_integrity(saved_plan)
    saved_repository, _ = _find_step(saved_plan, step_id)
    repository = str(saved_repository["repository"])
    if _repository_config(config, repository).completion_policy == "selected_local_sync" and not completion_controller:
        raise ExecutionError("Selected local completion requires the durable action-review workflow, not direct execute.")
    # Runtime routing breaks the action-controller/executor import cycle.
    from .review_actions import require_prior_completion
    if state_directory is not None and os.path.lexists(state_directory.expanduser()):
        _require_current_reconciled_plan(state_directory.expanduser(), saved_plan)
    try:
        binding, _, fresh_step = _fresh_preflight(config, client, saved_plan, step_id)
    except GitHubError as exc:
        raise GitHubError(public_github_error_message("execution preflight")) from exc
    already_applied = bool(fresh_step.get("already_applied"))
    if not already_applied:
        require_prior_completion((state_directory or default_state_directory()).expanduser(), repository,
                                 datetime.now(timezone.utc))
    supervisor = None
    if not already_applied and config.supervisor.mode != "off":
        supervisor = _require_supervised_action(config, (state_directory or default_state_directory()).expanduser(), saved_plan, step_id)
    _, saved_step = _find_step(saved_plan, step_id)
    repository_config = _repository_config(config, str(binding["repository"]))
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
        "decision": _decision_context(saved_step, repository_config, supervisor),
        "action_id": _action_id(binding),
        "approval_token": None if already_applied else approval_token(binding),
        "next": (
            "No action is needed; GitHub reports this exact head as already merged."
            if already_applied
            else "Repeat execute with the displayed approval confirmation token to authorize this exact action."
        ),
    }


def _require_supervised_action(
    config: WatcherConfig, directory: Path, saved_plan: Mapping[str, Any], step_id: str,
) -> dict[str, Any] | None:
    try:
        return require_supervisor_recommendation(config, directory, saved_plan, step_id)
    except (ReviewQueueError, RuntimeStateError) as exc:
        raise ExecutionError(str(exc)) from exc


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _prepare_state_directory(path: Path) -> Path:
    expanded = path.expanduser()
    if os.name == "nt":
        try:
            return ensure_private_directory(expanded)
        except RuntimeStateError as exc:
            raise ExecutionError(str(exc)) from exc
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
    if os.name != "nt":
        if hasattr(os, "getuid") and state.st_uid != os.getuid():
            raise ExecutionError(f"{label} must be owned by the current user.")
        if stat.S_IMODE(state.st_mode) & 0o077:
            raise ExecutionError(f"{label} permissions must not allow group or other access.")
    return state


def _validate_windows_path_matches_descriptor(
    path: Path,
    descriptor: int,
    *,
    maximum_bytes: int,
    label: str,
) -> None:
    try:
        path_state = validate_private_file(path, maximum_bytes=maximum_bytes)
    except RuntimeStateError as exc:
        raise ExecutionError(str(exc)) from exc
    opened = os.fstat(descriptor)
    if (path_state.st_dev, path_state.st_ino) != (opened.st_dev, opened.st_ino):
        raise ExecutionError(f"{label} changed during safe open.")


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
        if os.name == "nt":
            secure_private_file(lock_path)
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


@contextmanager
def _operation_locks(state_directory: Path) -> Iterator[None]:
    """Serialize reconciliation and execution before taking the execution lock."""

    try:
        with run_lock(state_directory):
            with _execution_lock(state_directory):
                yield
    except RuntimeStateError as exc:
        raise ExecutionError(str(exc)) from exc


def _require_current_reconciled_plan(
    state_directory: Path,
    saved_plan: Mapping[str, Any],
) -> None:
    """Reject authority from a plan superseded or revoked by reconciliation."""

    try:
        state = load_run_state(state_directory)
    except RuntimeStateError as exc:
        raise ExecutionError(str(exc)) from exc
    latest = state.get("latest_plan")
    if not isinstance(latest, Mapping):
        return
    latest_plan_id = latest.get("plan_id")
    saved_plan_id = saved_plan.get("plan_id")
    revoked_reason = latest.get("revoked_reason")
    superseded_plan_id = latest.get("supersedes_plan_id")
    invalidated_plan_ids = {
        item
        for item in latest.get("invalidated_plan_ids") or []
        if isinstance(item, str)
    }
    if isinstance(superseded_plan_id, str):
        invalidated_plan_ids.add(superseded_plan_id)
    if (
        revoked_reason is not None
        and (
            saved_plan_id in invalidated_plan_ids
            or latest.get("lineage_overflow") is True
        )
    ):
        raise ExecutionError(
            "The latest reconciled plan was revoked; run Watcher again and request fresh approval."
        )
    if revoked_reason is not None:
        return
    if (
        isinstance(latest_plan_id, str)
        and latest_plan_id
        and saved_plan_id != latest_plan_id
    ):
        raise ExecutionError(
            "This plan was superseded by a newer reconciliation; request approval from the current plan."
        )


def _append_audit(
    state_directory: Path,
    event: Mapping[str, Any],
    *,
    reserve_bytes: int = 0,
) -> Path:
    audit_path = state_directory / _AUDIT_FILE
    existed = os.path.lexists(audit_path)
    if os.name == "nt" and existed:
        try:
            validate_private_file(audit_path, maximum_bytes=_MAX_AUDIT_BYTES)
        except RuntimeStateError as exc:
            raise ExecutionError(str(exc)) from exc
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
        if os.name == "nt" and not existed:
            try:
                secure_private_file(audit_path, maximum_bytes=_MAX_AUDIT_BYTES)
            except RuntimeStateError as exc:
                raise ExecutionError(str(exc)) from exc
        if os.name == "nt":
            _validate_windows_path_matches_descriptor(
                audit_path,
                descriptor,
                maximum_bytes=_MAX_AUDIT_BYTES,
                label="Execution audit",
            )
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
    if os.name == "nt" and os.path.lexists(audit_path):
        try:
            validate_private_file(audit_path, maximum_bytes=_MAX_AUDIT_BYTES)
        except RuntimeStateError as exc:
            raise ExecutionError(str(exc)) from exc
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
        if os.name == "nt":
            _validate_windows_path_matches_descriptor(
                audit_path,
                descriptor,
                maximum_bytes=_MAX_AUDIT_BYTES,
                label="Execution audit",
            )
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


def exact_action_audit(state_directory: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Read validated audit evidence for one exact binding without acting.

    Unlike the bounded overview, this lookup never drops an older action merely
    because other actions were recorded afterward.
    """
    matching = [event for event in _read_audit_events(state_directory)
                if event["action_id"] == _action_id(binding) and event["binding"] == dict(binding)]
    return {
        "latest_event": matching[-1]["event_type"] if matching else None,
        "success_recorded": any(event["event_type"] == "execution_succeeded" for event in matching),
    }


def execution_observation(
    state_directory: Path,
    inspection: Mapping[str, Any],
) -> dict[str, Any]:
    """Summarize terminal and interrupted execution events against current evidence."""

    events = _read_audit_events(state_directory)
    repositories: dict[str, dict[str, Any]] = {}
    for raw_repository in inspection.get("repositories") or []:
        if not isinstance(raw_repository, Mapping):
            continue
        name = raw_repository.get("repository")
        if not isinstance(name, str):
            continue
        open_heads: dict[int, str] = {}
        for pull_request in raw_repository.get("pull_requests") or []:
            if not isinstance(pull_request, Mapping):
                continue
            identity = pull_request.get("candidate_identity")
            if not isinstance(identity, Mapping):
                continue
            number = identity.get("number")
            head_sha = identity.get("head_sha")
            if isinstance(number, int) and isinstance(head_sha, str):
                open_heads[number] = head_sha
        repository_key = name.casefold()
        if repository_key in repositories:
            raise ExecutionError("Execution observation contains duplicate repository aliases.")
        repositories[repository_key] = {
            "complete": not bool(
                raw_repository.get("errors") or raw_repository.get("truncations")
            ),
            "open_heads": open_heads,
        }

    action_events: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        action_id = event.get("action_id")
        if isinstance(action_id, str):
            action_events.setdefault(action_id, []).append(event)

    observations: list[dict[str, Any]] = []
    for action_id, grouped_events in action_events.items():
        latest = grouped_events[-1]
        binding = latest.get("binding")
        binding_data = binding if isinstance(binding, Mapping) else {}
        repository = binding_data.get("repository")
        pull_request = binding_data.get("pull_request")
        head_sha = binding_data.get("head_sha")
        event_type = latest.get("event_type")
        repository_state = repositories.get(str(repository).casefold())
        recovery_required = False
        if event_type == "execution_started":
            status = "interrupted"
            recovery_required = True
        elif event_type == "execution_failed":
            status = "failed"
            recovery_required = True
        elif event_type == "execution_reconciled":
            status = "already_applied"
        elif event_type == "execution_succeeded":
            if repository_state is None or repository_state.get("complete") is not True:
                status = "observation_incomplete"
                recovery_required = True
            else:
                current_open_heads = repository_state.get("open_heads")
                current_head = (
                    current_open_heads.get(pull_request)
                    if isinstance(current_open_heads, dict)
                    and isinstance(pull_request, int)
                    else None
                )
                if current_head is None:
                    status = "success_observed"
                elif current_head == head_sha:
                    status = "success_not_reflected"
                    recovery_required = True
                else:
                    status = "repository_state_changed"
                    recovery_required = True
        else:
            status = "unknown"
            recovery_required = True
        observations.append(
            {
                "action_id": action_id,
                "repository": repository,
                "pull_request": pull_request,
                "head_sha": head_sha,
                "latest_event": event_type,
                "latest_outcome": latest.get("outcome"),
                "status": status,
                "recovery_required": recovery_required,
                "recovery": latest.get("recovery"),
            }
        )

    bounded = observations[-20:]
    revision_payload = json.dumps(
        observations,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "event_count": len(events),
        "action_count": len(observations),
        "recovery_required_count": sum(
            1 for item in observations if item["recovery_required"]
        ),
        "recovery_required": any(
            item["recovery_required"] for item in observations
        ),
        "truncated": len(observations) > len(bounded),
        "revision": f"execution_{hashlib.sha256(revision_payload).hexdigest()[:24]}",
        "observations": bounded,
    }


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


def _terminal_audit_reserve(binding: Mapping[str, Any], supervisor: Mapping[str, Any] | None = None) -> int:
    provenance = {"supervisor": dict(supervisor)} if supervisor else {}
    success = _audit_event(
        "execution_succeeded",
        binding,
        outcome="merged",
        merge_commit_sha="f" * 64,
        **provenance,
    )
    failure = _audit_event(
        "execution_failed",
        binding,
        outcome="github_error",
        recovery="Reinspect GitHub state and create a fresh plan before retrying.",
        **provenance,
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


def _require_unchanged_configuration(config: WatcherConfig, path: Path | None) -> None:
    """Revoke a file-backed action when its authoritative policy changes."""
    if path is None:
        # Programmatic callers may own an immutable policy without a profile file.
        return
    try:
        current = load_config(path)
    except ConfigError as exc:
        raise ExecutionError("Configuration is unavailable; request a fresh review.") from exc
    if current != config:
        raise ExecutionError("Configuration changed; request a fresh review.")


def execute_approved(
    config: WatcherConfig,
    client: ExecutionGitHubClient,
    saved_plan: Mapping[str, Any],
    step_id: str,
    supplied_approval_token: str,
    *,
    state_directory: Path | None = None,
    config_path: Path | None = None,
    before_execution: Callable[[], None] | None = None,
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
    if repository_config.completion_policy == "selected_local_sync" and before_execution is None:
        raise ExecutionError("Selected local completion requires the durable action-review workflow, not direct execute.")
    saved_binding = _binding(saved_repository, saved_step, repository_config)
    if not hmac.compare_digest(
        supplied_approval_token, approval_token(saved_binding)
    ):
        raise ExecutionError(
            "Approval confirmation token does not authorize the current exact action."
        )

    validate_control_locations(config, config_path, state_directory or default_state_directory())
    state_path = _prepare_state_directory(state_directory or default_state_directory())
    with _operation_locks(state_path):
        _require_unchanged_configuration(config, config_path)
        _require_current_reconciled_plan(state_path, saved_plan)
        validate_audit_history(state_path)
        from .review_actions import PendingMaintenanceError, require_prior_completion
        completion_blocker: PendingMaintenanceError | None = None
        try:
            require_prior_completion(state_path, repository_name, datetime.now(timezone.utc))
        except PendingMaintenanceError as exc:
            completion_blocker = exc
        # Preserve consume-before-live-read semantics for executable actions.
        # A blocked action may only perform an exact already-applied observation.
        if completion_blocker is None:
            if before_execution is not None:
                before_execution()
            if repository_config.completion_policy == "selected_local_sync":
                from .review_actions import require_completion_intent
                require_completion_intent(config, state_path, _action_id(saved_binding))
        try:
            binding, _, current = _fresh_preflight(config, client, saved_plan, step_id)
        except GitHubError as exc:
            raise GitHubError(public_github_error_message("execution preflight")) from exc
        if current.get("already_applied"):
            if completion_blocker is not None and before_execution is not None:
                before_execution()
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
                "decision": _decision_context(saved_step, repository_config),
                "action_id": _action_id(binding),
                "audit_path": str(audit_path),
                "next": "No action is needed; GitHub reports this exact head as already merged.",
            }
        if completion_blocker is not None:
            raise completion_blocker
        expected_token = approval_token(binding)
        if not hmac.compare_digest(supplied_approval_token, expected_token):
            raise ExecutionError("Action changed during the final recheck; approval is invalid.")

        # Live collection can take time. Reload again before recording/issuing
        # the action so a policy revoked during that collection fails closed.
        _require_unchanged_configuration(config, config_path)
        supervisor = _require_supervised_action(config, state_path, saved_plan, step_id)
        _require_unchanged_configuration(config, config_path)
        provenance = {"supervisor": dict(supervisor)} if supervisor else {}
        audit_path = _append_audit(
            state_path,
            _audit_event("execution_started", binding, outcome="pending", **provenance),
            reserve_bytes=_terminal_audit_reserve(binding, supervisor),
        )
        try:
            _require_unchanged_configuration(config, config_path)
        except ExecutionError:
            _append_audit(state_path, _audit_event("execution_failed", binding,
                outcome="configuration_revoked", **provenance))
            raise
        try:
            raw_result = client.merge_pull_request(
                str(binding["repository"]),
                int(binding["pull_request"]),
                head_sha=str(binding["head_sha"]),
                merge_method=str(binding["merge_method"]),
            )
        except GitHubError as exc:
            _append_audit(
                state_path,
                _audit_event(
                    "execution_failed",
                    binding,
                    outcome="github_error",
                    recovery="Reinspect GitHub state and create a fresh plan before retrying.",
                    **provenance,
                ),
            )
            raise GitHubError(public_github_error_message("merge operation")) from exc
        result = _sanitized_merge_result(raw_result)
        if result["merged"] is not True:
            _append_audit(
                state_path,
                _audit_event(
                    "execution_failed",
                    binding,
                    outcome="merge_not_confirmed",
                    recovery="Reinspect GitHub state and create a fresh plan before retrying.",
                    **provenance,
                ),
            )
            raise ExecutionError(
                "GitHub did not confirm that the approved pull request was merged."
            )
        _append_audit(
            state_path,
            _audit_event(
                "execution_succeeded",
                binding,
                outcome="merged",
                merge_commit_sha=result.get("merge_commit_sha"),
                **provenance,
            ),
        )
        post_action_state = _post_action_state(config, client, repository_config)
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
            "decision": _decision_context(saved_step, repository_config, supervisor),
            "action_id": _action_id(binding),
            "result": result,
            "post_action_state": post_action_state,
            "audit_path": str(audit_path),
            "recovery": [
                "Reinspect the repository before taking another action.",
                "Verify configured downstream checks or deployment state outside this command.",
            ],
        }
