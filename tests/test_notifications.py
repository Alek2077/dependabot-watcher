from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from dependabot_watcher.notifications import deliver_desktop_notification
from dependabot_watcher.native_review import NativeReviewError


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
        "state_path": "/private/profile-one/run-state.json",
        "report_path": "/private/profile-one/latest-run.json",
        "summary": {
            "repository_count": 3,
            "open_pull_request_count": 10,
            "open_alert_count": 7,
            "attention_required_count": 1,
        },
        "advisory_coverage": {"mode": "off"},
        "provider_usage": {"call_count": 0},
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
    assert payload["title"] == "Dependabot Watcher: approval ready"
    assert "3 repositories | 10 PRs | 7 alerts | 1 need attention" in payload["body"]
    assert "Run: run_example" in payload["body"]
    assert "Profile: state-" in payload["body"]
    assert "AI: off (no model used)" in payload["body"]
    assert "Ask your agent" in payload["body"]
    assert "execute --plan-file" not in payload["body"]
    assert result["assurance"] == "adapter_accepted_only"


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
    assert result["assurance"] == "not_accepted"
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
    assert result["assurance"] == "not_attempted"
    assert runner.commands == []


def test_desktop_notification_profile_identity_differs_across_state_paths() -> None:
    from dependabot_watcher.notifications import _content

    first = _report()
    second = _report()
    second["state_path"] = "/private/profile-two/run-state.json"

    first_body = _content(first)[1]
    second_body = _content(second)[1]

    first_profile = next(line for line in first_body.splitlines() if line.startswith("Profile:"))
    second_profile = next(line for line in second_body.splitlines() if line.startswith("Profile:"))
    assert first_profile != second_profile


def test_bound_wsl_delivery_uses_registered_component_not_legacy_balloon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Fixture-Ubuntu")
    calls: list[tuple[Path, Path, dict[str, Any]]] = []

    def notify(config: Path, state: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append((config, state, kwargs))
        return {"status": "accepted"}

    monkeypatch.setattr("dependabot_watcher.notifications.native_notification", notify)
    runner = _Runner()
    report = _report()
    report["run_id"] = "run_" + "a" * 20
    result = deliver_desktop_notification(report, runner=runner,
        config_path=Path("/private/config.toml"), state_directory=Path("/private/state"))
    assert result["status"] == "delivered" and result["assurance"] == "adapter_accepted_only"
    assert calls == [(Path("/private/config.toml"), Path("/private/state"),
                      {"operation": "run", "run_id": report["run_id"]})]
    assert runner.commands == []


def test_bound_wsl_component_failure_does_not_fall_back_or_expose_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Fixture-Ubuntu")

    def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NativeReviewError("private diagnostic marker")

    monkeypatch.setattr("dependabot_watcher.notifications.native_notification", fail)
    runner = _Runner()
    result = deliver_desktop_notification(_report(), runner=runner,
        config_path=Path("/private/config.toml"), state_directory=Path("/private/state"))
    assert result["status"] == "failed" and result["assurance"] == "not_accepted"
    assert "private diagnostic marker" not in json.dumps(result)
    assert runner.commands == []
