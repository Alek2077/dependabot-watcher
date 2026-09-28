from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

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
        return WindowsTaskSchedulerAdapter(runner=runner)
    if platform == "systemd":
        return SystemdUserAdapter(runner=runner)
    if platform == "launchd":
        return LaunchdAdapter(runner=runner)
    raise ScheduleError(f"Unsupported scheduling platform: {platform}.")


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
        import pwd

        distro = distro or os.getenv("WSL_DISTRO_NAME")
        if not distro:
            raise ScheduleError("WSL scheduling requires --distro or WSL_DISTRO_NAME.")
        linux_user = pwd.getpwuid(os.getuid()).pw_name
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
    return operation(spec)
