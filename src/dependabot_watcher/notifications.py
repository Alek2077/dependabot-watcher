from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .terminal import sanitize_human_value
from .config import ConfigError
from .native_review import NativeReviewError, native_notification
from .runtime_state import RuntimeStateError
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


def _profile_identity(report: Mapping[str, Any]) -> str | None:
    state_path = report.get("state_path") or report.get("report_path")
    if not isinstance(state_path, str) or not state_path:
        return None
    digest = hashlib.sha256(state_path.encode("utf-8")).hexdigest()[:12]
    return f"state-{digest}"


def _content(report: Mapping[str, Any], *, review_available: bool = False) -> tuple[str, str]:
    notification = report.get("notification")
    data = notification if isinstance(notification, Mapping) else {}
    titles = {
        "action_recovery_required": "Dependabot Watcher: recovery needed",
        "incomplete_evidence_or_advice": "Dependabot Watcher: run incomplete",
        "approval_ready": "Dependabot Watcher: approval ready",
        "review_required": "Dependabot Watcher: review needed",
    }
    title = titles.get(str(data.get("reason")), "Dependabot Watcher: action needed")
    summary_value = report.get("summary")
    summary = summary_value if isinstance(summary_value, Mapping) else {}
    lines = [
        sanitize_human_value(
            data.get("message") or "The Dependabot maintenance queue needs attention."
        ),
        (
            f"{sanitize_human_value(summary.get('repository_count', 0))} repositories | "
            f"{sanitize_human_value(summary.get('open_pull_request_count', 0))} PRs | "
            f"{sanitize_human_value(summary.get('open_alert_count', 0))} alerts | "
            f"{sanitize_human_value(summary.get('attention_required_count', 0))} need attention"
        ),
    ]
    profile_id = _profile_identity(report)
    if profile_id:
        lines.append(f"Profile: {sanitize_human_value(profile_id)}")
    run_id = data.get("run_id") or report.get("run_id")
    if run_id:
        lines.append(f"Run: {sanitize_human_value(run_id)}")
    coverage_value = report.get("advisory_coverage")
    coverage = coverage_value if isinstance(coverage_value, Mapping) else {}
    mode = str(coverage.get("mode") or "off")
    usage_value = report.get("provider_usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    if mode == "off":
        lines.append("AI: off (no model used)")
    else:
        call_count = int(usage.get("call_count") or 0)
        unit = "call" if call_count == 1 else "calls"
        lines.append(f"AI: {sanitize_human_value(mode)} ({call_count} model {unit} this run)")
    lines.append("Open your saved review queue to inspect the next step."
                 if review_available else "Ask your agent to review the latest Dependabot Watcher result.")
    body = "\n".join(lines)
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
    config_path: Path | None = None,
    state_directory: Path | None = None,
) -> dict[str, Any]:
    """Attempt one generic local desktop notification without exposing raw evidence."""

    notification = report.get("notification")
    data = notification if isinstance(notification, Mapping) else {}
    if data.get("needed") is not True:
        return {
            "mode": "desktop",
            "status": "not_needed",
            "assurance": "not_attempted",
            "attempted_at": None,
            "delivered_at": None,
            "error": None,
        }
    title, body = _content(report)
    active_runner = runner or SubprocessNotificationRunner()
    platform = _platform()
    attempted_at = _timestamp()
    if platform == "windows":
        if os.getenv("WSL_DISTRO_NAME") and config_path is not None and state_directory is not None:
            # A configured WSL run must not silently fall back to the transient
            # legacy balloon when its selected companion fails or has changed.
            try:
                receipt = native_notification(config_path, state_directory,
                    operation="run", run_id=str(report.get("run_id")))
                returncode = 0 if receipt["status"] == "accepted" else 1
            except (NativeReviewError, RuntimeStateError, ConfigError):
                returncode = 1
        else:
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
        "assurance": "adapter_accepted_only" if returncode == 0 else "not_accepted",
        "attempted_at": attempted_at,
        "delivered_at": delivered_at,
        "error": None if returncode == 0 else "desktop_notification_unavailable",
    }
