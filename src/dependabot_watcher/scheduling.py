from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import WatcherConfig
from .schedulers.common import (
    CommandRunner,
    ScheduleError,
    SchedulerAdapter,
    schedule_spec,
)
from .schedulers.launchd import LaunchdAdapter
from .schedulers.systemd import SystemdUserAdapter
from .schedulers.windows import WindowsTaskSchedulerAdapter


SUPPORTED_PLATFORMS = frozenset({"auto", "wsl", "windows", "systemd", "launchd"})
SCHEDULE_ACTIONS = frozenset({"preview", "install", "status", "disable", "remove"})


def recurring_profile_summary(config: WatcherConfig) -> dict[str, Any]:
    """Return non-secret recurring-operation choices for schedule reports."""

    provider = config.advisory.codex_cli
    return {
        "repository_count": len(config.repositories),
        "repositories": [repository.name for repository in config.repositories],
        "advisory": {
            "mode": config.advisory.mode,
            "provider": config.advisory.provider,
            "triage": (
                asdict(provider.triage)
                if provider is not None and provider.triage is not None
                else None
            ),
            "review": (
                asdict(provider.review)
                if provider is not None and provider.review is not None
                else None
            ),
            "budget": asdict(config.advisory.budget),
            "usage_effect": (
                "no_model_calls_or_provider_usage"
                if config.advisory.mode == "off"
                else "may_consume_provider_quota_or_incur_cost"
            ),
            "evidence_privacy": (
                "not_applicable_ai_off"
                if config.advisory.mode == "off"
                else "selected_repository_pr_alert_dependency_path_check_and_deterministic_assessment_evidence_may_be_sent_to_provider"
            ),
        },
        "operation": {
            "autonomy": config.operation.autonomy,
            "mutation_enabled": config.operation.mutation_enabled,
            "require_advice_before_execution": (
                config.operation.require_advice_before_execution
            ),
        },
    }


def _current_wsl_user() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def detect_schedule_platform(requested: str = "auto") -> str:
    if requested not in SUPPORTED_PLATFORMS:
        raise ScheduleError(f"Unsupported scheduling platform: {requested}.")
    if os.name == "nt":
        detected = "windows"
    elif sys.platform == "darwin":
        detected = "launchd"
    elif sys.platform.startswith("linux") and os.getenv("WSL_DISTRO_NAME"):
        detected = "wsl"
    elif sys.platform.startswith("linux"):
        detected = "systemd"
    else:
        raise ScheduleError("No supported native scheduler was detected on this platform.")
    if requested != "auto" and requested != detected:
        raise ScheduleError(
            f"Scheduling platform {requested!r} is incompatible with the current {detected!r} host."
        )
    if requested != "auto":
        return requested
    return detected


def scheduler_adapter(
    platform: str,
    *,
    runner: CommandRunner | None = None,
) -> SchedulerAdapter:
    if platform in {"wsl", "windows"}:
        return WindowsTaskSchedulerAdapter(
            runner=runner,
            inventory_platform=platform,
            expected_wsl_user=_current_wsl_user() if platform == "wsl" else None,
        )
    if platform == "systemd":
        return SystemdUserAdapter(runner=runner)
    if platform == "launchd":
        return LaunchdAdapter(runner=runner)
    raise ScheduleError(f"Unsupported scheduling platform: {platform}.")


def inventory_schedules(
    *,
    requested_platform: str = "auto",
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Discover current-user Watcher schedules without loading a profile or writing state."""

    platform = detect_schedule_platform(requested_platform)
    return scheduler_adapter(platform, runner=runner).inventory()


def _manual_verification_argv(spec: Any) -> list[str]:
    argv = [item for item in spec.run_argv if item != "--quiet"]
    if "--format" in argv:
        index = argv.index("--format")
        if index + 1 < len(argv):
            argv[index + 1] = "text"
    else:
        argv.extend(["--format", "text"])
    if "--notify" not in argv:
        argv.extend(["--notify", str(spec.notification_mode)])
    return argv


def manage_schedule(
    action: str,
    *,
    config_path: Path,
    state_directory: Path,
    interval_minutes: int,
    notification_mode: str = "off",
    requested_platform: str = "auto",
    distro: str | None = None,
    executable: str = "dependabot-watcher",
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    if action not in SCHEDULE_ACTIONS:
        raise ScheduleError(f"Unsupported schedule action: {action}.")
    platform = detect_schedule_platform(requested_platform)
    if platform == "wsl":
        distro = distro or os.getenv("WSL_DISTRO_NAME")
        if not distro:
            raise ScheduleError("WSL scheduling requires --distro or WSL_DISTRO_NAME.")
        linux_user = _current_wsl_user()
    elif distro is not None:
        raise ScheduleError("--distro is valid only for the WSL scheduler adapter.")
    else:
        linux_user = None
    spec = schedule_spec(
        platform=platform,
        config_path=config_path,
        state_directory=state_directory,
        interval_minutes=interval_minutes,
        notification_mode=notification_mode,
        distro=distro,
        linux_user=linux_user,
        executable=executable,
        require_executable=action in {"preview", "install"},
    )
    adapter = scheduler_adapter(platform, runner=runner)
    operation = getattr(adapter, action)
    report = operation(spec)
    if action == "install":
        report["verification"] = {
            "argv": _manual_verification_argv(spec),
            "purpose": "manual_read_only_run",
        }
        report["next"] = (
            "Run one read-only reconciliation now to verify collection, saved state, "
            "and notification behavior; a manual verification run does not prove that "
            "the native scheduler has fired."
        )
    return report
