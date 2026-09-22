from __future__ import annotations

import argparse
import sys
from enum import IntEnum
from pathlib import Path

from . import __version__
from .advisory import AdvisoryError, advise
from .config import ConfigError, WatcherConfig, default_config_path, load_config
from .github import GhCliClient, GitHubError
from .inspector import inspect
from .output import render, render_advisory, render_plan
from .planning import PlanningError, plan


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION_ERROR = 2
    GITHUB_ERROR = 3
    PARTIAL_INSPECTION = 4
    ADVISORY_ERROR = 5
    PLANNING_ERROR = 6
    INTERNAL_ERROR = 70


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dependabot-watcher",
        description="Inspect Dependabot activity across configured GitHub repositories.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="Run a model-free inspection.")
    advise_parser = commands.add_parser(
        "advise",
        help="Inspect and optionally obtain bounded model advice.",
    )
    plan_parser = commands.add_parser(
        "plan",
        help="Build a non-mutating deterministic Recommended Order.",
    )
    for command_parser in (inspect_parser, advise_parser, plan_parser):
        command_parser.add_argument(
            "--config",
            type=Path,
            default=default_config_path(),
            help="TOML configuration path (default: platform user config path).",
        )
        command_parser.add_argument(
            "--format",
            choices=("text", "json", "markdown"),
            default="text",
            dest="output_format",
            help="Stdout format (default: text).",
        )
        command_parser.add_argument(
            "--repo",
            action="append",
            dest="repositories",
            help="Use only a configured owner/repository; repeat to select several.",
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
        advisory=config.advisory,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _select_repositories(load_config(args.config), args.repositories)
        client = GhCliClient(timeout_seconds=config.collection.command_timeout_seconds)
        if args.command == "inspect":
            report = inspect(config, client)
            rendered = render(report, args.output_format)
        elif args.command == "advise":
            report = advise(config, client)
            rendered = render_advisory(report, args.output_format)
        elif args.command == "plan":
            report = plan(config, client)
            rendered = render_plan(report, args.output_format)
        else:  # pragma: no cover - protected by argparse
            parser.error("A command is required.")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ExitCode.CONFIGURATION_ERROR
    except GitHubError as exc:
        print(f"GitHub error: {exc}", file=sys.stderr)
        return ExitCode.GITHUB_ERROR
    except AdvisoryError as exc:
        print(f"Advisory error: {exc}", file=sys.stderr)
        return ExitCode.ADVISORY_ERROR
    except PlanningError as exc:
        print(f"Planning error: {exc}", file=sys.stderr)
        return ExitCode.PLANNING_ERROR
    except Exception as exc:  # defensive CLI boundary
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR

    sys.stdout.write(rendered)
    if report["status"] == "partial":
        return ExitCode.PARTIAL_INSPECTION
    return ExitCode.SUCCESS
