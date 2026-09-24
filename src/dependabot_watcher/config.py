from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

tomllib = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")


SCHEMA_VERSION = 1
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
class RepositoryConfig:
    name: str
    default_branch: str = "main"
    path_policy: PathPolicyOverrides = field(default_factory=PathPolicyOverrides)
    workflow: RepositoryWorkflowConfig = field(default_factory=RepositoryWorkflowConfig)


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
    report_verbosity: str = "concise"
    machine_output: str = "json"


@dataclass(frozen=True)
class ProfileMetadata:
    source: str | None = None
    created_at: str | None = None
    confirmed_at: str | None = None
    last_verified_at: str | None = None


@dataclass(frozen=True)
class WatcherConfig:
    schema_version: int
    repositories: tuple[RepositoryConfig, ...]
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    path_policy: PathPolicyOverrides = field(default_factory=PathPolicyOverrides)
    advisory: AdvisoryConfig = field(default_factory=AdvisoryConfig)
    operation: OperationConfig = field(default_factory=OperationConfig)
    profile: ProfileMetadata = field(default_factory=ProfileMetadata)


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
        {"autonomy", "mutation_enabled", "report_verbosity", "machine_output"},
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
    _reject_unknown(data, {"name", "default_branch", "path_policy", "workflow"}, location)
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
    return RepositoryConfig(
        name=name,
        default_branch=branch,
        path_policy=_path_policy(data.get("path_policy", {}), f"{location}.path_policy"),
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
    return WatcherConfig(
        schema_version=SCHEMA_VERSION,
        repositories=repositories,
        collection=_collection(data.get("collection", {})),
        path_policy=_path_policy(data.get("path_policy", {}), "path_policy"),
        advisory=_advisory(data.get("advisory", {})),
        operation=_operation(data.get("operation", {})),
        profile=_profile(data.get("profile", {})),
    )


def load_config(path: Path) -> WatcherConfig:
    try:
        with path.expanduser().open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration file not found: {path.expanduser()}. Copy config.example.toml and edit it."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read configuration {path.expanduser()}: {exc}") from exc
    return parse_config(raw)
