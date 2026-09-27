from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path
from typing import Iterator, TextIO

from . import __version__
from .advisory import AdvisoryError, advise
from .config import ConfigError, WatcherConfig, default_config_path, load_config
from .diagnostics import doctor
from .execution import ExecutionError, execute_approved, load_plan, preflight_execution
from .github import GhCliClient, GitHubError
from .inspector import inspect
from .operational_output import render_operational
from .output import render, render_advisory, render_execution, render_plan
from .planning import PlanningError, plan
from .setup import SetupError, SetupRequest, setup
from .terminal import (
    ProgressMode,
    TerminalPresentation,
    decorate_text_report,
    detect_terminal_presentation,
    format_cli_error,
    live_progress,
    text_for_stream,
)


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION_ERROR = 2
    GITHUB_ERROR = 3
    PARTIAL_INSPECTION = 4
    ADVISORY_ERROR = 5
    PLANNING_ERROR = 6
    EXECUTION_ERROR = 7
    INTERNAL_ERROR = 70


@contextmanager
def _live_progress(
    message: str,
    *,
    enabled: bool,
    stream: TextIO,
) -> Iterator[None]:
    """Show transient progress without contaminating redirected or structured output."""

    if not enabled:
        yield
        return
    presentation = TerminalPresentation(
        interactive=True,
        decorate=False,
        color=False,
        unicode_symbols=False,
        progress=ProgressMode.STATIC,
    )
    with live_progress(message, presentation=presentation, stream=stream):
        yield


def _progress_enabled(output_format: str, *, stdout: TextIO, stderr: TextIO) -> bool:
    """Return whether transient progress is safe for this output boundary."""

    presentation = detect_terminal_presentation(
        output_format,
        stdout=stdout,
        stderr=stderr,
    )
    return presentation.progress is not ProgressMode.HIDDEN


def _write_report(report: str, presentation: TerminalPresentation) -> None:
    rendered = decorate_text_report(report, presentation)
    sys.stdout.write(text_for_stream(rendered, sys.stdout))


def _write_error(category: str, message: object, presentation: TerminalPresentation) -> None:
    rendered = format_cli_error(category, message, presentation) + "\n"
    sys.stderr.write(text_for_stream(rendered, sys.stderr))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dependabot-watcher",
        description=(
            "Inspect, plan, and safely gate Dependabot activity across configured "
            "GitHub repositories."
        ),
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
        help="Run a read-only preflight for the environment and every configured repository.",
    )
    setup_parser = commands.add_parser(
        "setup",
        help="Create a safe, non-secret operational profile.",
    )
    execute_parser = commands.add_parser(
        "execute",
        help="Preflight or perform one exact approval-gated plan action.",
    )
    for command_parser in (
        inspect_parser,
        advise_parser,
        plan_parser,
        doctor_parser,
        setup_parser,
        execute_parser,
    ):
        command_parser.add_argument(
            "--plain",
            action="store_true",
            help="Disable animation and terminal styling for accessible human output.",
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
        "--autonomy",
        choices=("read_only", "approval_required"),
        default="read_only",
        help="Operational authority; approval_required enables only exact-token actions.",
    )
    setup_parser.add_argument(
        "--acknowledge-mutation",
        action="store_true",
        help="Acknowledge that a later exact-token execute call may mutate GitHub.",
    )
    setup_parser.add_argument(
        "--merge-method",
        action="append",
        default=[],
        metavar="OWNER/REPO=METHOD",
        help="Explicit merge, squash, or rebase method for one selected repository.",
    )
    setup_parser.add_argument(
        "--required-check",
        action="append",
        default=[],
        metavar="OWNER/REPO=CHECK",
        help="Required passing check for one selected repository; repeat as needed.",
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
    execute_parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="TOML configuration path (default: platform user config path).",
    )
    execute_parser.add_argument(
        "--plan-file",
        type=Path,
        required=True,
        help="Saved JSON output from `plan --format json`.",
    )
    execute_parser.add_argument("--step-id", required=True, help="Exact saved plan step ID.")
    execute_parser.add_argument(
        "--approval-token",
        help="Exact approval confirmation token from a preceding read-only execution preflight.",
    )
    execute_parser.add_argument(
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


def _merge_method_overrides(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ConfigError("--merge-method must use OWNER/REPO=METHOD form.")
        repository, method = (item.strip() for item in value.split("=", 1))
        if not repository or method not in {"merge", "squash", "rebase"}:
            raise ConfigError("--merge-method must name merge, squash, or rebase.")
        if repository in result:
            raise ConfigError(f"Duplicate --merge-method for {repository}.")
        result[repository] = method
    return result


def _required_check_overrides(values: list[str]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for value in values:
        if "=" not in value:
            raise ConfigError("--required-check must use OWNER/REPO=CHECK form.")
        raw_repository, check = value.split("=", 1)
        repository = raw_repository.strip()
        if (
            not repository
            or not check
            or check != check.strip()
            or "\n" in check
            or "\r" in check
        ):
            raise ConfigError("--required-check must use OWNER/REPO=CHECK form.")
        checks = grouped.setdefault(repository, [])
        if check in checks:
            raise ConfigError(f"Duplicate --required-check {check!r} for {repository}.")
        checks.append(check)
    return {repository: tuple(checks) for repository, checks in grouped.items()}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    presentation = detect_terminal_presentation(
        args.output_format,
        stdout=sys.stdout,
        stderr=sys.stderr,
        plain=args.plain,
    )
    try:
        if args.command == "doctor":
            doctor_message = (
                "Checking the local environment..."
                if args.offline
                else "Checking environment and repository readiness..."
            )
            with live_progress(
                doctor_message,
                presentation=presentation,
                stream=sys.stderr,
            ):
                report = doctor(args.config, offline=args.offline)
            _write_report(render_operational(report, args.output_format), presentation)
            return (
                ExitCode.SUCCESS
                if report["status"] == "ready"
                else ExitCode.PARTIAL_INSPECTION
            )
        if args.command == "setup":
            repository_count = len(args.repositories)
            repository_word = "repository" if repository_count == 1 else "repositories"
            setup_message = (
                "Creating the local operational profile..."
                if args.offline
                else (
                    f"Verifying GitHub access for {repository_count} {repository_word} "
                    "and creating the profile..."
                )
            )
            with live_progress(
                setup_message,
                presentation=presentation,
                stream=sys.stderr,
            ):
                report = setup(
                    args.config,
                    SetupRequest(
                        repositories=tuple(args.repositories),
                        branches=_branch_overrides(args.branch),
                        merge_methods=_merge_method_overrides(args.merge_method),
                        required_checks=_required_check_overrides(args.required_check),
                        report_verbosity=args.verbosity,
                        advisory_mode=args.advisory_mode,
                        triage_model=args.triage_model,
                        triage_reasoning=args.triage_reasoning,
                        review_model=args.review_model,
                        review_reasoning=args.review_reasoning,
                        acknowledge_model_use=args.acknowledge_model_use,
                        autonomy=args.autonomy,
                        acknowledge_mutation=args.acknowledge_mutation,
                        offline=args.offline,
                    ),
                )
            _write_report(render_operational(report, args.output_format), presentation)
            return ExitCode.SUCCESS
        if args.command == "execute":
            config = load_config(args.config)
            client = GhCliClient(timeout_seconds=config.collection.command_timeout_seconds)
            saved_plan = load_plan(args.plan_file)
            with live_progress(
                "Rechecking the exact plan action against live GitHub state...",
                presentation=presentation,
                stream=sys.stderr,
            ):
                if args.approval_token:
                    report = execute_approved(
                        config,
                        client,
                        saved_plan,
                        args.step_id,
                        args.approval_token,
                    )
                else:
                    report = preflight_execution(
                        config, client, saved_plan, args.step_id
                    )
            _write_report(render_execution(report, args.output_format), presentation)
            return ExitCode.SUCCESS
        config = _select_repositories(load_config(args.config), args.repositories)
        client = GhCliClient(timeout_seconds=config.collection.command_timeout_seconds)
        verbosity = args.verbosity or config.operation.report_verbosity
        repository_count = len(config.repositories)
        repository_word = "repository" if repository_count == 1 else "repositories"
        progress_messages = {
            "inspect": (
                f"Collecting live GitHub evidence for {repository_count} {repository_word}..."
            ),
            "advise": (
                f"Collecting evidence for {repository_count} {repository_word} and "
                "preparing optional advice..."
            ),
            "plan": (
                f"Collecting evidence for {repository_count} {repository_word} and "
                "building the maintenance plan..."
            ),
        }
        with live_progress(
            progress_messages[args.command],
            presentation=presentation,
            stream=sys.stderr,
        ):
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
        _write_error("Configuration", exc, presentation)
        return ExitCode.CONFIGURATION_ERROR
    except GitHubError as exc:
        _write_error("GitHub", exc, presentation)
        return ExitCode.GITHUB_ERROR
    except AdvisoryError as exc:
        _write_error("Advisory", exc, presentation)
        return ExitCode.ADVISORY_ERROR
    except PlanningError as exc:
        _write_error("Planning", exc, presentation)
        return ExitCode.PLANNING_ERROR
    except SetupError as exc:
        _write_error("Setup", exc, presentation)
        return ExitCode.CONFIGURATION_ERROR
    except ExecutionError as exc:
        _write_error("Execution", exc, presentation)
        return ExitCode.EXECUTION_ERROR
    except Exception as exc:  # defensive CLI boundary
        _write_error("Unexpected", exc, presentation)
        return ExitCode.INTERNAL_ERROR

    _write_report(rendered, presentation)
    if report["status"] == "partial":
        return ExitCode.PARTIAL_INSPECTION
    return ExitCode.SUCCESS
