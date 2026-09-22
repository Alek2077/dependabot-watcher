from __future__ import annotations

import argparse
import sys
from enum import IntEnum
from pathlib import Path

from . import __version__
from .advisory import AdvisoryError, advise
from .config import ConfigError, WatcherConfig, default_config_path, load_config
from .diagnostics import doctor
from .github import GhCliClient, GitHubError
from .inspector import inspect
from .operational_output import render_operational
from .output import render, render_advisory, render_plan
from .planning import PlanningError, plan
from .setup import SetupError, SetupRequest, setup


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
    doctor_parser = commands.add_parser(
        "doctor",
        help="Run a read-only environment and repository preflight.",
    )
    setup_parser = commands.add_parser(
        "setup",
        help="Create a safe, non-secret operational profile.",
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
            "--verbosity",
            choices=("concise", "standard", "detailed"),
            default=None,
            help="Human output depth; JSON is always complete.",
        )
        command_parser.add_argument(
            "--repo",
            action="append",
            dest="repositories",
            help="Use only a configured owner/repository; repeat to select several.",
        )
    doctor_parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="TOML configuration path (missing configuration is reported, not created).",
    )
    doctor_parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        dest="output_format",
    )
    doctor_parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip authentication and repository network checks.",
    )
    setup_parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Configuration path to create (existing files are never overwritten).",
    )
    setup_parser.add_argument(
        "--repo",
        action="append",
        required=True,
        dest="repositories",
        help="Repository in owner/name form; repeat for several repositories.",
    )
    setup_parser.add_argument(
        "--branch",
        action="append",
        default=[],
        metavar="OWNER/REPO=BRANCH",
        help="Explicit branch override for one selected repository.",
    )
    setup_parser.add_argument(
        "--verbosity",
        choices=("concise", "standard", "detailed"),
        default="concise",
        help="Default human report depth.",
    )
    setup_parser.add_argument(
        "--advisory-mode",
        choices=("off", "economy", "reviewed"),
        default="off",
    )
    setup_parser.add_argument("--triage-model")
    setup_parser.add_argument("--triage-reasoning")
    setup_parser.add_argument("--review-model")
    setup_parser.add_argument("--review-reasoning")
    setup_parser.add_argument(
        "--acknowledge-model-use",
        action="store_true",
        help="Acknowledge that later advice can consume provider quota or incur cost.",
    )
    setup_parser.add_argument(
        "--offline",
        action="store_true",
        help="Create an unverified profile without authentication or repository calls.",
    )
    setup_parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        dest="output_format",
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
        operation=config.operation,
        profile=config.profile,
    )


def _branch_overrides(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ConfigError("--branch must use OWNER/REPO=BRANCH form.")
        repository, branch = value.split("=", 1)
        repository = repository.strip()
        branch = branch.strip()
        if not repository or not branch or any(character.isspace() for character in branch):
            raise ConfigError("--branch must use OWNER/REPO=BRANCH form.")
        if repository in result:
            raise ConfigError(f"Duplicate --branch override for {repository}.")
        result[repository] = branch
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            report = doctor(args.config, offline=args.offline)
            sys.stdout.write(render_operational(report, args.output_format))
            return (
                ExitCode.SUCCESS
                if report["status"] == "ready"
                else ExitCode.PARTIAL_INSPECTION
            )
        if args.command == "setup":
            report = setup(
                args.config,
                SetupRequest(
                    repositories=tuple(args.repositories),
                    branches=_branch_overrides(args.branch),
                    report_verbosity=args.verbosity,
                    advisory_mode=args.advisory_mode,
                    triage_model=args.triage_model,
                    triage_reasoning=args.triage_reasoning,
                    review_model=args.review_model,
                    review_reasoning=args.review_reasoning,
                    acknowledge_model_use=args.acknowledge_model_use,
                    offline=args.offline,
                ),
            )
            sys.stdout.write(render_operational(report, args.output_format))
            return ExitCode.SUCCESS
        config = _select_repositories(load_config(args.config), args.repositories)
        client = GhCliClient(timeout_seconds=config.collection.command_timeout_seconds)
        verbosity = args.verbosity or config.operation.report_verbosity
        if args.command == "inspect":
            report = inspect(config, client)
            rendered = render(report, args.output_format, verbosity=verbosity)
        elif args.command == "advise":
            report = advise(config, client)
            rendered = render_advisory(report, args.output_format, verbosity=verbosity)
        elif args.command == "plan":
            report = plan(config, client)
            rendered = render_plan(report, args.output_format, verbosity=verbosity)
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
    except SetupError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return ExitCode.CONFIGURATION_ERROR
    except Exception as exc:  # defensive CLI boundary
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR

    sys.stdout.write(rendered)
    if report["status"] == "partial":
        return ExitCode.PARTIAL_INSPECTION
    return ExitCode.SUCCESS
