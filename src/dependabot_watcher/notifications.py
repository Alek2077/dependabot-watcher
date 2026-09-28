from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .terminal import sanitize_human_value
from .windows_helpers import WindowsHelperError, trusted_windows_executable


class NotificationRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 30,
    ) -> int:
        ...


class SubprocessNotificationRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 30,
    ) -> int:
        try:
            result = subprocess.run(
                list(command),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 127
        return result.returncode


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _platform() -> str:
    if os.name == "nt" or os.getenv("WSL_DISTRO_NAME"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _next_command(notification: Mapping[str, Any]) -> str | None:
    next_action = notification.get("next")
    next_data = next_action if isinstance(next_action, Mapping) else {}
    argv = next_data.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        return None
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _content(report: Mapping[str, Any]) -> tuple[str, str]:
    notification = report.get("notification")
    data = notification if isinstance(notification, Mapping) else {}
    title = "Dependabot Watcher: action needed"
    body = sanitize_human_value(
        data.get("message") or "The Dependabot maintenance queue needs attention."
    )
    command = _next_command(data)
    if command:
        body = f"{body}\nNext: {sanitize_human_value(command)}"
    return title[:120], body[:1000]


def _trusted_windows_powershell() -> str | None:
    try:
        executable = trusted_windows_executable(
            "WindowsPowerShell/v1.0/powershell.exe"
        )
    except WindowsHelperError:
        return None
    return str(executable)


def _windows_delivery(
    runner: NotificationRunner,
    title: str,
    body: str,
) -> int:
    executable = _trusted_windows_powershell()
    if executable is None:
        return 127
    script = """
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notification = New-Object System.Windows.Forms.NotifyIcon
$notification.Icon = [System.Drawing.SystemIcons]::Information
$notification.BalloonTipTitle = [string]$payload.title
$notification.BalloonTipText = [string]$payload.body
$notification.Visible = $true
$notification.ShowBalloonTip(10000)
Start-Sleep -Milliseconds 1200
$notification.Dispose()
""".strip()
    return runner.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        input_text=json.dumps({"title": title, "body": body}),
    )


def _macos_delivery(
    runner: NotificationRunner,
    title: str,
    body: str,
) -> int:
    executable = shutil.which("osascript")
    if executable is None:
        return 127
    return runner.run(
        [
            executable,
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            body,
        ]
    )


def _linux_delivery(
    runner: NotificationRunner,
    title: str,
    body: str,
) -> int:
    executable = shutil.which("notify-send")
    if executable is None:
        return 127
    return runner.run([executable, "--app-name", "Dependabot Watcher", title, body])


def deliver_desktop_notification(
    report: Mapping[str, Any],
    *,
    runner: NotificationRunner | None = None,
) -> dict[str, Any]:
    """Attempt one generic local desktop notification without exposing raw evidence."""

    notification = report.get("notification")
    data = notification if isinstance(notification, Mapping) else {}
    if data.get("needed") is not True:
        return {
            "mode": "desktop",
            "status": "not_needed",
            "attempted_at": None,
            "delivered_at": None,
            "error": None,
        }
    title, body = _content(report)
    active_runner = runner or SubprocessNotificationRunner()
    platform = _platform()
    attempted_at = _timestamp()
    if platform == "windows":
        returncode = _windows_delivery(active_runner, title, body)
    elif platform == "macos":
        returncode = _macos_delivery(active_runner, title, body)
    elif platform == "linux":
        returncode = _linux_delivery(active_runner, title, body)
    else:
        returncode = 127
    delivered_at = _timestamp() if returncode == 0 else None
    return {
        "mode": "desktop",
        "adapter": platform,
        "status": "delivered" if returncode == 0 else "failed",
        "attempted_at": attempted_at,
        "delivered_at": delivered_at,
        "error": None if returncode == 0 else "desktop_notification_unavailable",
    }
