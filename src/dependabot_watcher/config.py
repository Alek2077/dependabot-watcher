from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .runtime_state import (
    RuntimeStateError,
    validate_private_file,
    validate_user_owned_directory,
)

SCHEMA_VERSION = 1
_MAX_CONFIG_BYTES = 5_000_000
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """Raised when user configuration cannot be trusted."""


def normalize_repository_name(value: object, location: str = "repository") -> str:
    """Return a validated GitHub owner/repository identity."""

    if (
        not isinstance(value, str)
        or value.strip().startswith("-")
        or not _REPOSITORY_NAME.fullmatch(value.strip())
    ):
        raise ConfigError(f"{location} must use the 'owner/repository' form.")
    return value.strip()


def normalize_branch_name(value: object, location: str = "branch") -> str:
    """Return a bounded single-line branch name suitable for encoded API use."""

    if not isinstance(value, str):
        raise ConfigError(f"{location} must be a non-empty branch name.")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in normalized
        )
    ):
        raise ConfigError(f"{location} must be a non-empty single-line branch name.")
    return normalized


def repository_names_equal(left: str, right: str) -> bool:
    """Match GitHub repository scope without rewriting saved evidence identity."""
    return left.casefold() == right.casefold()


@dataclass(frozen=True)
class PathPolicyOverrides:
    dependency_patterns: tuple[str, ...] = ()
    sensitive_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryWorkflowConfig:
    merge_method: str = "repository_default"
    promotion_branch: str | None = None
    required_checks: tuple[str, ...] = ()
    deployment_environments: tuple[str, ...] = ()
    validation_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalSyncCheckout:
    """Selected local completion capability, never standing action consent."""

    directory: str
    branch: str


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    default_branch: str = "main"
    path_policy: PathPolicyOverrides = field(default_factory=PathPolicyOverrides)
    workflow: RepositoryWorkflowConfig = field(default_factory=RepositoryWorkflowConfig)
    local_sync: tuple[LocalSyncCheckout, ...] = ()
    completion_policy: str = "remote_only"


@dataclass(frozen=True)
class CollectionConfig:
    history_lookback_days: int = 30
    max_open_prs: int = 100
    max_open_alerts: int = 500
    max_history_items: int = 200
    max_files_per_pull_request: int = 500
    max_manifest_files: int = 50
    max_manifest_file_bytes: int = 131072
    max_manifest_depth: int = 5
    command_timeout_seconds: int = 120


@dataclass(frozen=True)
class AdvisoryBudgetConfig:
    max_candidates: int = 0
    batch_size: int = 0
    max_triage_calls: int = 0
    max_review_candidates: int = 0
    max_review_calls: int = 0


@dataclass(frozen=True)
class CodexStageConfig:
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = 900


@dataclass(frozen=True)
class CodexProviderConfig:
    executable: str = "codex"
    triage: CodexStageConfig | None = None
    review: CodexStageConfig | None = None


@dataclass(frozen=True)
class AdvisoryConfig:
    mode: str = "off"
    provider: str | None = None
    budget: AdvisoryBudgetConfig = field(default_factory=AdvisoryBudgetConfig)
    codex_cli: CodexProviderConfig | None = None


@dataclass(frozen=True)
class OperationConfig:
    autonomy: str = "read_only"
    mutation_enabled: bool = False
    require_advice_before_execution: bool = False
    report_verbosity: str = "concise"
    machine_output: str = "json"


@dataclass(frozen=True)
class ProfileMetadata:
    source: str | None = None
    created_at: str | None = None
    confirmed_at: str | None = None
    last_verified_at: str | None = None


@dataclass(frozen=True)
class SupervisorWorkspaceConfig:
    repository: str
    directory: str
    instruction_sources: tuple[str, ...] = ()
    refresh_authorized: bool = False


@dataclass(frozen=True)
class SupervisorValidationConfig:
    """Explicit isolated preparation policy; never remote mutation authority."""

    preparation_authorized: bool = False
    network_authorized: bool = False
    maximum_bytes: int = 256 * 1024 * 1024
    minimum_free_bytes: int = 512 * 1024 * 1024
    command_timeout_seconds: int = 120
    storage_reserve_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupervisorConfig:
    """Explicit host/runtime policy, independent of metadata advice and merging."""

    mode: str = "off"
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    expected_account_type: str | None = None
    access: str = "inherit"
    permission_profile: str | None = None
    model_use_acknowledged: bool = False
    full_access_acknowledged: bool = False
    validation_authorized: bool = False
    timeout_seconds: int = 900
    max_jobs_per_run: int = 1
    workspaces: tuple[SupervisorWorkspaceConfig, ...] = ()
    validation: SupervisorValidationConfig = field(default_factory=SupervisorValidationConfig)


@dataclass(frozen=True)
class WatcherConfig:
    schema_version: int
    repositories: tuple[RepositoryConfig, ...]
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    path_policy: PathPolicyOverrides = field(default_factory=PathPolicyOverrides)
    advisory: AdvisoryConfig = field(default_factory=AdvisoryConfig)
    operation: OperationConfig = field(default_factory=OperationConfig)
    profile: ProfileMetadata = field(default_factory=ProfileMetadata)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)


def default_config_path() -> Path:
    override = os.getenv("DEPENDABOT_WATCHER_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "dependabot-watcher" / "config.toml"
    config_home = Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "dependabot-watcher" / "config.toml"


def _require_mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a TOML table.")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"Unknown {location} key(s): {', '.join(unknown)}.")


def _positive_int(value: object, location: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{location} must be an integer.")
    if value < 1 or value > maximum:
        raise ConfigError(f"{location} must be between 1 and {maximum}.")
    return value


def _nonnegative_int(value: object, location: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{location} must be an integer.")
    if value < 0 or value > maximum:
        raise ConfigError(f"{location} must be between 0 and {maximum}.")
    return value


def _optional_string(value: object, location: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location} must be a non-empty string when configured.")
    return value.strip()


def _optional_timestamp(value: object, location: str) -> str | None:
    text = _optional_string(value, location)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{location} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ConfigError(f"{location} must include a timezone offset.")
    return text


def _operation(value: object) -> OperationConfig:
    data = _require_mapping({} if value is None else value, "operation")
    _reject_unknown(
        data,
        {
            "autonomy",
            "mutation_enabled",
            "require_advice_before_execution",
            "report_verbosity",
            "machine_output",
        },
        "operation",
    )
    autonomy = _optional_string(data.get("autonomy", "read_only"), "operation.autonomy")
    if autonomy not in {"read_only", "approval_required"}:
        raise ConfigError(
            "operation.autonomy must be one of: read_only, approval_required."
        )
    mutation_enabled = data.get("mutation_enabled", False)
    if not isinstance(mutation_enabled, bool):
        raise ConfigError("operation.mutation_enabled must be a boolean.")
    if mutation_enabled != (autonomy == "approval_required"):
        raise ConfigError(
            "operation.mutation_enabled must be true only when autonomy is "
            "'approval_required', and false for 'read_only'."
        )
    require_advice = data.get("require_advice_before_execution", False)
    if not isinstance(require_advice, bool):
        raise ConfigError("operation.require_advice_before_execution must be a boolean.")
    report_verbosity = _optional_string(
        data.get("report_verbosity", "concise"),
        "operation.report_verbosity",
    )
    if report_verbosity not in {"concise", "standard", "detailed"}:
        raise ConfigError(
            "operation.report_verbosity must be one of: concise, standard, detailed."
        )
    machine_output = _optional_string(
        data.get("machine_output", "json"),
        "operation.machine_output",
    )
    if machine_output != "json":
        raise ConfigError("operation.machine_output currently supports only 'json'.")
    return OperationConfig(
        autonomy=autonomy,
        mutation_enabled=mutation_enabled,
        require_advice_before_execution=require_advice,
        report_verbosity=report_verbosity,
        machine_output=machine_output,
    )


def _profile(value: object) -> ProfileMetadata:
    data = _require_mapping({} if value is None else value, "profile")
    _reject_unknown(
        data,
        {"source", "created_at", "confirmed_at", "last_verified_at"},
        "profile",
    )
    source = _optional_string(data.get("source"), "profile.source")
    if source is not None and source not in {"manual", "setup_quick", "setup_advanced"}:
        raise ConfigError(
            "profile.source must be one of: manual, setup_quick, setup_advanced."
        )
    return ProfileMetadata(
        source=source,
        created_at=_optional_timestamp(data.get("created_at"), "profile.created_at"),
        confirmed_at=_optional_timestamp(
            data.get("confirmed_at"), "profile.confirmed_at"
        ),
        last_verified_at=_optional_timestamp(
            data.get("last_verified_at"), "profile.last_verified_at"
        ),
    )


def _patterns(value: object, location: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{location} must be an array of strings.")
    normalized: list[str] = []
    for item in value:
        pattern = item.strip().replace("\\", "/")
        if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
            raise ConfigError(f"{location} contains an invalid repository-relative pattern.")
        normalized.append(pattern)
    return tuple(dict.fromkeys(normalized))


def _bounded_strings(
    value: object,
    location: str,
    *,
    maximum_items: int = 50,
    maximum_length: int = 300,
    require_exact: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{location} must be an array of strings.")
    if len(value) > maximum_items:
        raise ConfigError(f"{location} must contain at most {maximum_items} items.")
    normalized: list[str] = []
    for item in value:
        text = item.strip()
        if (
            not text
            or (require_exact and text != item)
            or len(text) > maximum_length
            or any(char in text for char in "\r\n\0")
        ):
            raise ConfigError(
                f"{location} items must be non-empty single-line strings up to "
                f"{maximum_length} characters."
            )
        normalized.append(text)
    return tuple(dict.fromkeys(normalized))


def _path_policy(value: object, location: str) -> PathPolicyOverrides:
    data = _require_mapping({} if value is None else value, location)
    _reject_unknown(data, {"additional_dependency_patterns", "additional_sensitive_patterns"}, location)
    return PathPolicyOverrides(
        dependency_patterns=_patterns(
            data.get("additional_dependency_patterns"),
            f"{location}.additional_dependency_patterns",
        ),
        sensitive_patterns=_patterns(
            data.get("additional_sensitive_patterns"),
            f"{location}.additional_sensitive_patterns",
        ),
    )


def _collection(value: object) -> CollectionConfig:
    data = _require_mapping({} if value is None else value, "collection")
    _reject_unknown(
        data,
        {
            "history_lookback_days",
            "max_open_prs",
            "max_open_alerts",
            "max_history_items",
            "max_files_per_pull_request",
            "max_manifest_files",
            "max_manifest_file_bytes",
            "max_manifest_depth",
            "command_timeout_seconds",
        },
        "collection",
    )
    return CollectionConfig(
        history_lookback_days=_positive_int(
            data.get("history_lookback_days", 30),
            "collection.history_lookback_days",
            maximum=365,
        ),
        max_open_prs=_positive_int(
            data.get("max_open_prs", 100), "collection.max_open_prs", maximum=10000
        ),
        max_open_alerts=_positive_int(
            data.get("max_open_alerts", 500), "collection.max_open_alerts", maximum=50000
        ),
        max_history_items=_positive_int(
            data.get("max_history_items", 200), "collection.max_history_items", maximum=10000
        ),
        max_files_per_pull_request=_positive_int(
            data.get("max_files_per_pull_request", 500),
            "collection.max_files_per_pull_request",
            maximum=2999,
        ),
        max_manifest_files=_positive_int(
            data.get("max_manifest_files", 50),
            "collection.max_manifest_files",
            maximum=500,
        ),
        max_manifest_file_bytes=_positive_int(
            data.get("max_manifest_file_bytes", 131072),
            "collection.max_manifest_file_bytes",
            maximum=1048576,
        ),
        max_manifest_depth=_positive_int(
            data.get("max_manifest_depth", 5),
            "collection.max_manifest_depth",
            maximum=20,
        ),
        command_timeout_seconds=_positive_int(
            data.get("command_timeout_seconds", 120),
            "collection.command_timeout_seconds",
            maximum=3600,
        ),
    )


_ADVISORY_PRESETS: dict[str, AdvisoryBudgetConfig] = {
    "off": AdvisoryBudgetConfig(),
    "economy": AdvisoryBudgetConfig(
        max_candidates=8,
        batch_size=4,
        max_triage_calls=2,
    ),
    "reviewed": AdvisoryBudgetConfig(
        max_candidates=8,
        batch_size=4,
        max_triage_calls=2,
        max_review_candidates=4,
        max_review_calls=1,
    ),
}


def _advisory_budget(value: object, mode: str) -> AdvisoryBudgetConfig:
    data = _require_mapping({} if value is None else value, "advisory.budget")
    _reject_unknown(
        data,
        {
            "max_candidates",
            "batch_size",
            "max_triage_calls",
            "max_review_candidates",
            "max_review_calls",
        },
        "advisory.budget",
    )
    preset = _ADVISORY_PRESETS[mode]
    budget = AdvisoryBudgetConfig(
        max_candidates=_nonnegative_int(
            data.get("max_candidates", preset.max_candidates),
            "advisory.budget.max_candidates",
            maximum=1000,
        ),
        batch_size=_nonnegative_int(
            data.get("batch_size", preset.batch_size),
            "advisory.budget.batch_size",
            maximum=100,
        ),
        max_triage_calls=_nonnegative_int(
            data.get("max_triage_calls", preset.max_triage_calls),
            "advisory.budget.max_triage_calls",
            maximum=1000,
        ),
        max_review_candidates=_nonnegative_int(
            data.get("max_review_candidates", preset.max_review_candidates),
            "advisory.budget.max_review_candidates",
            maximum=1000,
        ),
        max_review_calls=_nonnegative_int(
            data.get("max_review_calls", preset.max_review_calls),
            "advisory.budget.max_review_calls",
            maximum=1000,
        ),
    )
    if mode != "off":
        if budget.max_candidates == 0 or budget.batch_size == 0 or budget.max_triage_calls == 0:
            raise ConfigError(
                "Enabled advisory modes require positive candidate, batch, and triage-call budgets."
            )
        if budget.batch_size > budget.max_candidates:
            raise ConfigError("advisory.budget.batch_size cannot exceed max_candidates.")
    if mode == "reviewed":
        if budget.max_review_candidates == 0 or budget.max_review_calls == 0:
            raise ConfigError("reviewed mode requires positive reviewer budgets.")
    return budget


def _codex_stage(value: object, location: str) -> CodexStageConfig | None:
    if value is None:
        return None
    data = _require_mapping(value, location)
    _reject_unknown(
        data,
        {"model", "reasoning_effort", "timeout_seconds"},
        location,
    )
    return CodexStageConfig(
        model=_optional_string(data.get("model"), f"{location}.model"),
        reasoning_effort=_optional_string(
            data.get("reasoning_effort"), f"{location}.reasoning_effort"
        ),
        timeout_seconds=_positive_int(
            data.get("timeout_seconds", 900),
            f"{location}.timeout_seconds",
            maximum=3600,
        ),
    )


def _codex_provider(value: object) -> CodexProviderConfig | None:
    if value is None:
        return None
    data = _require_mapping(value, "advisory.codex_cli")
    _reject_unknown(data, {"executable", "triage", "review"}, "advisory.codex_cli")
    executable = (
        _optional_string(
            data.get("executable", "codex"), "advisory.codex_cli.executable"
        )
        or "codex"
    )
    if executable != "codex":
        raise ConfigError(
            "advisory.codex_cli.executable is fixed to 'codex'; configuration cannot "
            "redirect provider command execution."
        )
    return CodexProviderConfig(
        executable=executable,
        triage=_codex_stage(data.get("triage"), "advisory.codex_cli.triage"),
        review=_codex_stage(data.get("review"), "advisory.codex_cli.review"),
    )


def _advisory(value: object) -> AdvisoryConfig:
    data = _require_mapping({} if value is None else value, "advisory")
    _reject_unknown(data, {"mode", "provider", "budget", "codex_cli"}, "advisory")
    mode = _optional_string(data.get("mode", "off"), "advisory.mode") or "off"
    if mode not in _ADVISORY_PRESETS:
        raise ConfigError("advisory.mode must be one of: off, economy, reviewed.")
    provider = _optional_string(data.get("provider"), "advisory.provider")
    if provider is not None and provider != "codex_cli":
        raise ConfigError("advisory.provider currently supports only 'codex_cli'.")
    codex_cli = _codex_provider(data.get("codex_cli"))
    if mode != "off":
        if provider is None:
            raise ConfigError("Enabled advisory modes require advisory.provider.")
        if provider == "codex_cli" and (codex_cli is None or codex_cli.triage is None):
            raise ConfigError("Enabled Codex advisory modes require advisory.codex_cli.triage.")
        if codex_cli is not None and codex_cli.triage is not None:
            if codex_cli.triage.model is None or codex_cli.triage.reasoning_effort is None:
                raise ConfigError(
                    "Enabled Codex triage requires explicit model and reasoning_effort values."
                )
        if mode == "reviewed" and (codex_cli is None or codex_cli.review is None):
            raise ConfigError("reviewed Codex mode requires advisory.codex_cli.review.")
        if mode == "reviewed" and codex_cli is not None and codex_cli.review is not None:
            if codex_cli.review.model is None or codex_cli.review.reasoning_effort is None:
                raise ConfigError(
                    "Reviewed Codex mode requires explicit reviewer model and "
                    "reasoning_effort values."
                )
    return AdvisoryConfig(
        mode=mode,
        provider=provider,
        budget=_advisory_budget(data.get("budget"), mode),
        codex_cli=codex_cli,
    )


def _repository(value: object, index: int) -> RepositoryConfig:
    location = f"repositories[{index}]"
    data = _require_mapping(value, location)
    _reject_unknown(data, {"name", "default_branch", "path_policy", "workflow", "local_sync", "completion_policy"}, location)
    name = normalize_repository_name(data.get("name"), f"{location}.name")
    branch = normalize_branch_name(
        data.get("default_branch", "main"),
        f"{location}.default_branch",
    )
    workflow_data = _require_mapping(data.get("workflow", {}), f"{location}.workflow")
    _reject_unknown(
        workflow_data,
        {
            "merge_method",
            "promotion_branch",
            "required_checks",
            "deployment_environments",
            "validation_notes",
        },
        f"{location}.workflow",
    )
    merge_method = _optional_string(
        workflow_data.get("merge_method", "repository_default"),
        f"{location}.workflow.merge_method",
    ) or "repository_default"
    if merge_method not in {"repository_default", "merge", "squash", "rebase"}:
        raise ConfigError(
            f"{location}.workflow.merge_method must be one of: "
            "repository_default, merge, squash, rebase."
        )
    promotion_branch = _optional_string(
        workflow_data.get("promotion_branch"),
        f"{location}.workflow.promotion_branch",
    )
    if promotion_branch is not None and any(char.isspace() for char in promotion_branch):
        raise ConfigError(
            f"{location}.workflow.promotion_branch must not contain whitespace."
        )
    local_sync = _local_sync(data.get("local_sync", []), location)
    completion_policy = data.get("completion_policy", "remote_only")
    if not isinstance(completion_policy, str) or completion_policy not in {"remote_only", "selected_local_sync"}:
        raise ConfigError(f"{location}.completion_policy must be remote_only or selected_local_sync.")
    if completion_policy == "selected_local_sync" and not local_sync:
        raise ConfigError(f"{location}.completion_policy requires selected local_sync checkouts.")
    if completion_policy == "selected_local_sync" and any(target.branch != branch for target in local_sync):
        raise ConfigError(f"{location}.completion_policy currently supports only checkouts of the configured default_branch.")
    return RepositoryConfig(
        name=name,
        default_branch=branch,
        path_policy=_path_policy(data.get("path_policy", {}), f"{location}.path_policy"),
        local_sync=local_sync,
        completion_policy=completion_policy,
        workflow=RepositoryWorkflowConfig(
            merge_method=merge_method,
            promotion_branch=promotion_branch,
            required_checks=_bounded_strings(
                workflow_data.get("required_checks"),
                f"{location}.workflow.required_checks",
                require_exact=True,
            ),
            deployment_environments=_bounded_strings(
                workflow_data.get("deployment_environments"),
                f"{location}.workflow.deployment_environments",
            ),
            validation_notes=_bounded_strings(
                workflow_data.get("validation_notes"),
                f"{location}.workflow.validation_notes",
            ),
        ),
    )


def _local_sync(value: object, location: str) -> tuple[LocalSyncCheckout, ...]:
    if not isinstance(value, list) or len(value) > 10:
        raise ConfigError(f"{location}.local_sync must contain at most ten selected checkouts.")
    selected = []
    for item in value:
        entry = _require_mapping(item, f"{location}.local_sync")
        _reject_unknown(entry, {"directory", "branch"}, f"{location}.local_sync")
        directory = _supervisor_path(entry.get("directory"), f"{location}.local_sync.directory")
        branch = normalize_branch_name(entry.get("branch"), f"{location}.local_sync.branch")
        if branch.startswith("-"):
            raise ConfigError("Local synchronization branch cannot begin with '-'.")
        selected.append(LocalSyncCheckout(directory, branch))
    if len({item.directory for item in selected}) != len(selected):
        raise ConfigError("Local synchronization checkout paths must be unique.")
    return tuple(selected)


def _supervisor(value: object, repositories: tuple[RepositoryConfig, ...]) -> SupervisorConfig:
    data = _require_mapping(value, "supervisor")
    mode = data.get("mode", "off")
    if mode == "off":
        _reject_unknown(data, {"mode"}, "disabled supervisor")
        return SupervisorConfig()
    if mode != "approval_first":
        raise ConfigError("supervisor.mode must be off or approval_first; unattended actions are not supported.")
    _reject_unknown(data, {"mode", "provider", "model", "reasoning_effort", "expected_account_type", "access",
                          "permission_profile", "model_use_acknowledged", "full_access_acknowledged",
                          "validation_authorized", "timeout_seconds", "max_jobs_per_run", "workspaces", "validation"}, "supervisor")
    if data.get("provider") != "codex_app_server":
        raise ConfigError("supervisor.provider must explicitly select codex_app_server.")
    model = _optional_string(data.get("model"), "supervisor.model")
    effort = data.get("reasoning_effort")
    if (model is None or len(model) > 128 or any(ord(c) < 32 for c in model)
            or not isinstance(effort, str) or effort not in {"minimal", "low", "medium", "high", "xhigh"}):
        raise ConfigError("Supervisor requires an explicit bounded model and reasoning_effort.")
    account = data.get("expected_account_type")
    if not isinstance(account, str) or account not in {"chatgpt", "apiKey"}:
        raise ConfigError("supervisor.expected_account_type must explicitly select chatgpt or apiKey.")
    access = data.get("access")
    if not isinstance(access, str) or access not in {"inherit", "restricted", "full"}:
        raise ConfigError("supervisor.access must explicitly select inherit, restricted or full.")
    for key in ("model_use_acknowledged", "full_access_acknowledged", "validation_authorized"):
        if type(data.get(key, False)) is not bool:
            raise ConfigError(f"supervisor.{key} must be a boolean.")
    if data.get("model_use_acknowledged") is not True:
        raise ConfigError("Supervisor model use requires acknowledgement of private evidence transmission and provider usage.")
    full = data.get("full_access_acknowledged", False)
    if (access == "full") != full:
        raise ConfigError("Full supervisor access requires deliberate acknowledgement; other modes cannot retain it.")
    permission = _optional_string(data.get("permission_profile"), "supervisor.permission_profile")
    if (access == "restricted" and (permission is None or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", permission))
            or access != "restricted" and permission is not None):
        raise ConfigError("A named supervisor permission_profile is required only for restricted access.")
    raw_workspaces = data.get("workspaces")
    if not isinstance(raw_workspaces, list) or not 1 <= len(raw_workspaces) <= len(repositories):
        raise ConfigError("Supervisor requires explicitly registered repository workspaces.")
    configured = {repo.name.casefold(): repo.name for repo in repositories}
    seen: set[str] = set()
    directories: set[str] = set()
    workspaces: list[SupervisorWorkspaceConfig] = []
    for raw in raw_workspaces:
        workspace = _require_mapping(raw, "supervisor.workspaces entry")
        _reject_unknown(workspace, {"repository", "directory", "instruction_sources", "refresh_authorized"}, "supervisor workspace")
        name = normalize_repository_name(workspace.get("repository"), "supervisor workspace repository")
        if name.casefold() not in configured or name.casefold() in seen:
            raise ConfigError("Supervisor workspaces must identify distinct configured repositories.")
        directory = _supervisor_path(workspace.get("directory"), "supervisor workspace directory")
        directory_key = os.path.normcase(directory)
        if directory_key in directories:
            raise ConfigError("Each supervisor repository requires its own registered workspace.")
        sources = workspace.get("instruction_sources")
        if not isinstance(sources, list) or len(sources) > 32:
            raise ConfigError("Supervisor instruction_sources must explicitly list the expected trusted instruction chain.")
        instructions = tuple(_supervisor_path(source, "supervisor instruction source") for source in sources)
        if len(set(instructions)) != len(instructions):
            raise ConfigError("Supervisor instruction_sources contains duplicates.")
        seen.add(name.casefold())
        directories.add(directory_key)
        refresh = workspace.get("refresh_authorized", False)
        if type(refresh) is not bool:
            raise ConfigError("Supervisor workspace refresh_authorized must be a boolean.")
        workspaces.append(SupervisorWorkspaceConfig(configured[name.casefold()], directory, instructions, refresh))
    raw_validation = _require_mapping(data.get("validation", {}), "supervisor.validation")
    _reject_unknown(raw_validation, {"preparation_authorized", "network_authorized", "maximum_bytes",
                                   "minimum_free_bytes", "command_timeout_seconds", "storage_reserve_paths"}, "supervisor.validation")
    for key in ("preparation_authorized", "network_authorized"):
        if type(raw_validation.get(key, False)) is not bool:
            raise ConfigError(f"supervisor.validation.{key} must be a boolean.")
    preparation = raw_validation.get("preparation_authorized", False)
    network = raw_validation.get("network_authorized", False)
    if (preparation and data.get("validation_authorized", False) is not True
            or network and not preparation):
        raise ConfigError("Validation preparation requires test authority; network requires preparation authority.")
    reserve_paths = raw_validation.get('storage_reserve_paths', [])
    if not isinstance(reserve_paths, list) or len(reserve_paths) > 8:
        raise ConfigError('supervisor.validation.storage_reserve_paths must be a bounded list.')
    validation = SupervisorValidationConfig(
        preparation_authorized=preparation, network_authorized=network,
        maximum_bytes=_positive_int(raw_validation.get("maximum_bytes", 256 * 1024**2),
                                    "supervisor.validation.maximum_bytes", maximum=16 * 1024**3),
        minimum_free_bytes=_positive_int(raw_validation.get("minimum_free_bytes", 512 * 1024**2),
                                         "supervisor.validation.minimum_free_bytes", maximum=64 * 1024**3),
        command_timeout_seconds=_positive_int(raw_validation.get("command_timeout_seconds", 120),
                                              "supervisor.validation.command_timeout_seconds", maximum=3600),
        storage_reserve_paths=tuple(_supervisor_path(path, 'validation storage reserve path') for path in reserve_paths))
    return SupervisorConfig(mode=mode, provider="codex_app_server", model=model, reasoning_effort=effort,
        expected_account_type=account, access=access, permission_profile=permission, model_use_acknowledged=True,
        full_access_acknowledged=full, validation_authorized=data.get("validation_authorized", False),
        timeout_seconds=_positive_int(data.get("timeout_seconds", 900), "supervisor.timeout_seconds", maximum=3600),
        max_jobs_per_run=_positive_int(data.get("max_jobs_per_run", 1), "supervisor.max_jobs_per_run", maximum=20),
        workspaces=tuple(workspaces), validation=validation)


def _supervisor_path(value: object, location: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 for char in value) or not Path(value).is_absolute()
            or ".." in Path(value).parts):
        raise ConfigError(f"{location} must be a bounded absolute host path without traversal.")
    # Parse without reading repository files or resolving symlinks. Runtime
    # launch must separately establish ownership, repository identity and harness.
    return str(Path(value))


def parse_config(data: Mapping[str, Any]) -> WatcherConfig:
    _reject_unknown(
        data,
        {
            "schema_version",
            "repositories",
            "collection",
            "path_policy",
            "advisory",
            "operation",
            "profile",
            "supervisor",
        },
        "top-level",
    )
    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}.")
    raw_repositories = data.get("repositories")
    if not isinstance(raw_repositories, list) or not raw_repositories:
        raise ConfigError("repositories must contain at least one [[repositories]] entry.")
    repositories = tuple(_repository(item, index) for index, item in enumerate(raw_repositories))
    names = [repository.name.casefold() for repository in repositories]
    if len(names) != len(set(names)):
        raise ConfigError("repositories contains duplicate repository names.")
    checkout_paths = [target.directory for repository in repositories for target in repository.local_sync]
    if len(checkout_paths) != len(set(checkout_paths)):
        raise ConfigError("A local checkout cannot be registered to multiple repositories.")
    advisory = _advisory(data.get("advisory", {}))
    operation = _operation(data.get("operation", {}))
    if operation.require_advice_before_execution and advisory.mode == "off":
        raise ConfigError(
            "operation.require_advice_before_execution requires an enabled advisory mode."
        )
    return WatcherConfig(
        schema_version=SCHEMA_VERSION,
        repositories=repositories,
        collection=_collection(data.get("collection", {})),
        path_policy=_path_policy(data.get("path_policy", {}), "path_policy"),
        advisory=advisory,
        operation=operation,
        profile=_profile(data.get("profile", {})),
        supervisor=_supervisor(data.get("supervisor", {}), repositories),
    )


def load_config(path: Path) -> WatcherConfig:
    expanded = path.expanduser()
    descriptor = -1
    try:
        validate_user_owned_directory(expanded.parent)
        inspected = expanded.lstat()
        if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode):
            raise ConfigError(f"Configuration is not a trusted regular file: {expanded}.")
        if inspected.st_size > _MAX_CONFIG_BYTES:
            raise ConfigError("Configuration exceeds its 5 MB safety ceiling.")
        if os.name == "nt":
            validate_private_file(expanded, maximum_bytes=_MAX_CONFIG_BYTES)
        else:
            if hasattr(os, "getuid") and inspected.st_uid != os.getuid():
                raise ConfigError(f"Configuration is not owned by the current user: {expanded}.")
            if stat.S_IMODE(inspected.st_mode) & 0o022:
                raise ConfigError(
                    f"Configuration permissions allow another principal to modify it: {expanded}."
                )
        descriptor = os.open(
            expanded,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            inspected.st_dev,
            inspected.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ConfigError("Configuration changed during safe open.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration file not found: {expanded}. Copy config.example.toml and edit it."
        ) from exc
    except RuntimeStateError as exc:
        raise ConfigError(str(exc)) from exc
    except ConfigError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read configuration {expanded}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return parse_config(raw)
