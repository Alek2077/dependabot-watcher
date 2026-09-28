from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from dependabot_watcher.notifications import deliver_desktop_notification


class _Runner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 30,
    ) -> int:
        self.commands.append(list(command))
        self.inputs.append(input_text)
        return self.returncode


def _report() -> dict[str, object]:
    return {
        "run_id": "run_example",
        "notification": {
            "needed": True,
            "reason": "approval_ready",
            "message": "A fresh Dependabot plan has an approval-gated next action.",
            "next": {
                "argv": [
                    "dependabot-watcher",
                    "execute",
                    "--plan-file",
                    "/private/state/plan.json",
                ]
            },
        },
    }


def test_wsl_desktop_notification_uses_bounded_powershell_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _Runner()
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(
        "dependabot_watcher.notifications._trusted_windows_powershell",
        lambda: "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    )

    result = deliver_desktop_notification(_report(), runner=runner)

    assert result["status"] == "delivered"
    assert result["adapter"] == "windows"
    assert runner.commands[0][:3] == [
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "-NoProfile",
        "-NonInteractive",
    ]
    payload = json.loads(runner.inputs[0] or "{}")
    assert payload["title"] == "Dependabot Watcher: action needed"
    assert "execute --plan-file" in payload["body"]
    assert "repositories" not in payload["body"]


def test_desktop_notification_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _Runner(returncode=1)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(
        "dependabot_watcher.notifications._trusted_windows_powershell",
        lambda: "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    )

    result = deliver_desktop_notification(_report(), runner=runner)

    assert result["status"] == "failed"
    assert result["error"] == "desktop_notification_unavailable"
    assert "stderr" not in result


def test_no_action_never_invokes_desktop_adapter(tmp_path: Path) -> None:
    runner = _Runner()
    report = _report()
    report["notification"] = {
        "needed": False,
        "reason": "unchanged",
        "message": None,
        "next": None,
    }

    result = deliver_desktop_notification(report, runner=runner)

    assert result["status"] == "not_needed"
    assert runner.commands == []
