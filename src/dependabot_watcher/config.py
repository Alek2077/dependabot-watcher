from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

tomllib = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")


SCHEMA_VERSION = 1
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """Raised when user configuration cannot be trusted."""


@dataclass(frozen=True)
class PathPolicyOverrides:
    dependency_patterns: tuple[str, ...] = ()
    sensitive_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    default_branch: str = "main"
    path_policy: PathPolicyOverrides = field(default_factory=PathPolicyOverrides)


@dataclass(frozen=True)
class CollectionConfig:
    history_lookback_days: int = 30
    max_open_prs: int = 100
    max_open_alerts: int = 500
    max_history_items: int = 200
    max_files_per_pull_request: int = 500
    command_timeout_seconds: int = 120


@dataclass(frozen=True)
class WatcherConfig:
    schema_version: int
    repositories: tuple[RepositoryConfig, ...]
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    path_policy: PathPolicyOverrides = field(default_factory=PathPolicyOverrides)


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
        command_timeout_seconds=_positive_int(
            data.get("command_timeout_seconds", 120),
            "collection.command_timeout_seconds",
            maximum=3600,
        ),
    )


def _repository(value: object, index: int) -> RepositoryConfig:
    location = f"repositories[{index}]"
    data = _require_mapping(value, location)
    _reject_unknown(data, {"name", "default_branch", "path_policy"}, location)
    name = data.get("name")
    if not isinstance(name, str) or not _REPOSITORY_NAME.fullmatch(name.strip()):
        raise ConfigError(f"{location}.name must use the 'owner/repository' form.")
    branch = data.get("default_branch", "main")
    if not isinstance(branch, str) or not branch.strip() or any(char.isspace() for char in branch):
        raise ConfigError(f"{location}.default_branch must be a non-empty branch name.")
    return RepositoryConfig(
        name=name.strip(),
        default_branch=branch.strip(),
        path_policy=_path_policy(data.get("path_policy", {}), f"{location}.path_policy"),
    )


def parse_config(data: Mapping[str, Any]) -> WatcherConfig:
    _reject_unknown(data, {"schema_version", "repositories", "collection", "path_policy"}, "top-level")
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
