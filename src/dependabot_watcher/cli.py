from __future__ import annotations

import argparse
import sys
from enum import IntEnum
from pathlib import Path

from . import __version__
from .config import ConfigError, WatcherConfig, default_config_path, load_config
from .github import GhCliClient, GitHubError
from .inspector import inspect
from .output import render


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION_ERROR = 2
    GITHUB_ERROR = 3
    PARTIAL_INSPECTION = 4
    INTERNAL_ERROR = 70


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dependabot-watcher",
        description="Inspect Dependabot activity across configured GitHub repositories.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser(
        "inspect",
        help="Run a model-free, side-effect-free inspection.",
    )
    inspect_parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="TOML configuration path (default: platform user config path).",
    )
    inspect_parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        dest="output_format",
        help="Stdout format (default: text).",
    )
    inspect_parser.add_argument(
        "--repo",
        action="append",
        dest="repositories",
        help="Inspect only a configured owner/repository; repeat to select several.",
    )
    return parser


def _select_repositories(config: WatcherConfig, requested: list[str] | None) -> WatcherConfig:
    if not requested:
        return config
    requested_names = set(requested)
    repositories = tuple(
        repository for repository in config.repositories if repository.name in requested_names
    )
    missing = sorted(requested_names - {repository.name for repository in repositories})
    if missing:
        raise ConfigError(f"Requested repositories are not configured: {', '.join(missing)}.")
    return WatcherConfig(
        schema_version=config.schema_version,
        repositories=repositories,
        collection=config.collection,
        path_policy=config.path_policy,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "inspect":  # pragma: no cover - protected by argparse
        parser.error("A command is required.")
    try:
        config = _select_repositories(load_config(args.config), args.repositories)
        client = GhCliClient(timeout_seconds=config.collection.command_timeout_seconds)
        report = inspect(config, client)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ExitCode.CONFIGURATION_ERROR
    except GitHubError as exc:
        print(f"GitHub error: {exc}", file=sys.stderr)
        return ExitCode.GITHUB_ERROR
    except Exception as exc:  # defensive CLI boundary
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR

    sys.stdout.write(render(report, args.output_format))
    if report["status"] == "partial":
        return ExitCode.PARTIAL_INSPECTION
    return ExitCode.SUCCESS

