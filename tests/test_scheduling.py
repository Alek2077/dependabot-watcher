from __future__ import annotations

import json
import os
import plistlib
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

import pytest

from dependabot_watcher.schedulers import launchd as launchd_module
from dependabot_watcher.schedulers.common import CommandResult, ScheduleError, schedule_spec
from dependabot_watcher.schedulers.launchd import LaunchdAdapter
from dependabot_watcher.schedulers.systemd import SystemdUserAdapter
from dependabot_watcher.schedulers.windows import (
    WindowsTaskSchedulerAdapter,
    _split_windows_command_line,
)
from dependabot_watcher.scheduling import (
    detect_schedule_platform,
    inventory_schedules,
    manage_schedule,
    recurring_profile_summary,
)
from dependabot_watcher.config import parse_config
from dependabot_watcher.scheduling_output import render_schedule
from dependabot_watcher import scheduling_output
from dependabot_watcher.runtime_state import save_private_json


class _SuccessRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.launchd_job_id: str | None = None

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        self.commands.append(list(command))
        if "bootstrap" in command:
            stored = plistlib.loads(Path(command[-1]).read_bytes())
            self.launchd_job_id = stored["EnvironmentVariables"][
                "DEPENDABOT_WATCHER_JOB_ID"
            ]
            return CommandResult(0, "", "")
        if "bootout" in command:
            self.launchd_job_id = None
            return CommandResult(0, "", "")
        if "print" in command:
            if command[-1] == "gui/501":
                label = (
                    ""
                    if self.launchd_job_id is None
                    else "com.dependabot-watcher."
                    + self.launchd_job_id.removeprefix("dependabot-watcher-")
                )
                return CommandResult(0, label + "\n" if label else "", "")
            if self.launchd_job_id is None:
                return CommandResult(113, "", "not found")
            return CommandResult(
                0,
                f"DEPENDABOT_WATCHER_JOB_ID = {self.launchd_job_id}\n",
                "",
            )
        if "show" in command and command[-1].startswith("--property="):
            unit = next(
                item for item in command if item.endswith((".timer", ".service"))
            )
            suffix = "timer" if unit.endswith(".timer") else "service"
            job_id = unit.removesuffix(f".{suffix}")
            description = (
                f"Schedule Dependabot Watcher reconciliation [{job_id}]"
                if suffix == "timer"
                else f"Dependabot Watcher reconciliation [{job_id}]"
            )
            return CommandResult(
                0,
                "\n".join(
                    [
                        "LoadState=loaded",
                        "ActiveState=active",
                        "UnitFileState=enabled",
                        f"Description={description}",
                    ]
                )
                + "\n",
                "",
            )
        return CommandResult(0, "active\n", "")


class _WindowsRunner:
    def __init__(self) -> None:
        self.xml: str | None = None
        self.enabled = False
        self.task_path = "\\"
        self.has_run = False
        self.next_run_at = "2026-09-30T00:00:00.0000000+02:00"
        self.last_run_at: str | None = None
        self.last_result = 267011
        self.missed_runs = 0
        self.commands: list[list[str]] = []
        self.payloads: list[dict[str, Any]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        self.commands.append(list(command))
        script = command[-1]
        payload = json.loads(input_text or "{}")
        self.payloads.append(payload)
        if "-TaskPath" in script:
            assert "$payload.task_path" in script
            assert payload["task_path"] == "\\"
        if "@{tasks=$tasks}" in script:
            tasks: list[dict[str, Any]] = []
            if self.xml is not None:
                root = ET.fromstring(self.xml)
                namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                uri = root.find("t:RegistrationInfo/t:URI", namespace)
                assert uri is not None and uri.text is not None
                tasks.append(
                    {
                        "name": uri.text.removeprefix("\\"),
                        "task_path": self.task_path,
                        "state": "Ready" if self.enabled else "Disabled",
                        "enabled": self.enabled,
                        "xml": self.xml,
                    }
                )
            return CommandResult(0, json.dumps({"tasks": tasks}), "")
        if "Register-ScheduledTask" in script:
            self.xml = payload["xml"]
            self.enabled = True
        elif "Disable-ScheduledTask" in script and self.xml is not None:
            self.enabled = False
            return CommandResult(0, json.dumps({"installed": True, "changed": True}), "")
        elif "Unregister-ScheduledTask" in script and self.xml is not None:
            self.xml = None
            self.enabled = False
            return CommandResult(0, json.dumps({"installed": False, "changed": True}), "")
        if self.xml is None:
            body: dict[str, Any] = {
                "installed": False,
                "state": "Missing",
                "enabled": False,
                "xml": None,
            }
        else:
            body = {
                "installed": True,
                "state": "Ready" if self.enabled else "Disabled",
                "enabled": self.enabled,
                "task_path": self.task_path,
                "xml": self.xml,
                "has_run": self.has_run,
                "next_run_at": self.next_run_at,
                "last_run_at": self.last_run_at,
                "last_result": self.last_result,
                "missed_runs": self.missed_runs,
            }
        return CommandResult(0, json.dumps(body), "")


class _LaunchdOrphanRunner:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.loaded = True
        self.commands: list[list[str]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        self.commands.append(list(command))
        if "bootout" in command:
            self.loaded = False
            return CommandResult(0, "", "")
        if "bootstrap" in command:
            self.loaded = True
            return CommandResult(0, "", "")
        if "print" in command:
            if not self.loaded:
                return CommandResult(113, "", "not found")
            return CommandResult(
                0,
                f"DEPENDABOT_WATCHER_JOB_ID => {self.job_id}\n",
                "",
            )
        return CommandResult(0, "", "")


class _LaunchdStickyRunner(_LaunchdOrphanRunner):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        if "bootout" in command:
            self.commands.append(list(command))
            return CommandResult(3, "", "not found")
        return super().run(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )


class _LaunchdBootstrapFailureRunner(_SuccessRunner):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_bootstrap = False

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        if "bootstrap" in command and self.fail_next_bootstrap:
            self.fail_next_bootstrap = False
            super().run(
                command,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
            )
            return CommandResult(5, "", "simulated bootstrap failure")
        return super().run(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )


class _ForeignNativeRunner:
    def __init__(self, manager: str) -> None:
        self.manager = manager
        self.commands: list[list[str]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        self.commands.append(list(command))
        if self.manager == "systemd" and "show" in command:
            return CommandResult(
                0,
                "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                "Description=Unrelated job\n",
                "",
            )
        if self.manager == "launchd" and "print" in command:
            return CommandResult(0, "label = unrelated\n", "")
        raise AssertionError("Foreign native identity must be rejected before mutation.")


class _SystemdServiceOrphanRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        self.commands.append(list(command))
        if "show" in command:
            unit = next(
                item for item in command if item.endswith((".timer", ".service"))
            )
            if unit.endswith(".timer"):
                return CommandResult(
                    0,
                    "LoadState=not-found\nActiveState=inactive\n"
                    "UnitFileState=disabled\nDescription=\n",
                    "",
                )
            job_id = unit.removesuffix(".service")
            return CommandResult(
                0,
                "LoadState=loaded\nActiveState=active\nUnitFileState=static\n"
                f"Description=Dependabot Watcher reconciliation [{job_id}]\n",
                "",
            )
        return CommandResult(0, "", "")


class _SystemdInactiveEnabledRunner(_SuccessRunner):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        result = super().run(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )
        if "show" in command and any(item.endswith(".timer") for item in command):
            return CommandResult(
                0,
                result.stdout.replace("ActiveState=active", "ActiveState=inactive"),
                result.stderr,
            )
        return result


class _SystemdInventoryCandidateRunner:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        if "list-units" in command:
            return CommandResult(
                0,
                f"{self.job_id}.service loaded active running Watcher\n",
                "",
            )
        if "list-unit-files" in command:
            return CommandResult(0, "", "")
        raise AssertionError(f"Unexpected command: {command}")


class _WindowsInventoryFailureRunner(_WindowsRunner):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        if "@{tasks=$tasks}" in command[-1]:
            return CommandResult(1, "", "access denied")
        return super().run(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )


class _WindowsLifecycleFailureRunner(_WindowsRunner):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        if "Get-ScheduledTask" in command[-1] and "@{tasks=$tasks}" not in command[-1]:
            return CommandResult(1, "", "access denied")
        return super().run(
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )


def _spec(
    tmp_path: Path,
    *,
    platform: str,
    distro: str | None = None,
    notification_mode: str = "off",
) -> Any:
    executable = tmp_path / "bin/dependabot-watcher"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("placeholder", encoding="utf-8")
    executable.chmod(0o700)
    return schedule_spec(
        platform=platform,
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        interval_minutes=60,
        notification_mode=notification_mode,
        distro=distro,
        linux_user="alex" if platform == "wsl" else None,
        executable=str(executable),
    )


def _windows_adapter(
    tmp_path: Path,
    runner: _WindowsRunner,
    *,
    windows_user: str = "DESKTOP\\alex",
    inventory_platform: str | None = None,
) -> WindowsTaskSchedulerAdapter:
    powershell = tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    wsl = tmp_path / "Windows/System32/wsl.exe"
    powershell.parent.mkdir(parents=True, exist_ok=True)
    powershell.write_text("placeholder", encoding="utf-8")
    wsl.parent.mkdir(parents=True, exist_ok=True)
    wsl.write_text("placeholder", encoding="utf-8")
    return WindowsTaskSchedulerAdapter(
        runner=runner,
        windows_user=windows_user,
        powershell_path=powershell,
        wsl_path=wsl,
        inventory_platform=inventory_platform,
        expected_wsl_user="alex" if inventory_platform == "wsl" else None,
    )


def test_schedule_spec_is_stable_and_binds_exact_run_command(tmp_path: Path) -> None:
    first = _spec(tmp_path, platform="systemd")
    second = _spec(tmp_path, platform="systemd")

    assert first.job_id == second.job_id
    assert first.job_id.startswith("dependabot-watcher-")
    assert first.run_argv == (
        str(first.executable),
        "run",
        "--config",
        str((tmp_path / "config.toml").resolve()),
        "--state-dir",
        str((tmp_path / "state").resolve()),
        "--expected-interval-minutes",
        "60",
        "--quiet",
        "--format",
        "json",
    )

    notified = _spec(tmp_path, platform="systemd", notification_mode="desktop")
    assert notified.run_argv[-2:] == ("--notify", "desktop")


def test_systemd_inventory_recovers_owned_profile_without_loading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(
        runner=_SuccessRunner(), unit_directory=tmp_path / "systemd"
    )
    definition = adapter.preview(spec)["definition"]
    service_path = Path(definition["service_path"])
    timer_path = Path(definition["timer_path"])
    service_path.parent.mkdir(parents=True)
    service_path.write_text(definition["service"], encoding="utf-8")
    timer_path.write_text(definition["timer"], encoding="utf-8")
    service_path.chmod(0o600)
    timer_path.chmod(0o600)

    report = adapter.inventory()

    assert report["status"] == "complete"
    assert report["registration_count"] == 1
    registration = report["registrations"][0]
    assert registration["job_id"] == spec.job_id
    assert registration["config_path"] == str(spec.config_path)
    assert registration["state_directory"] == str(spec.state_directory)
    assert registration["native_complete"] is True
    assert registration["drifted"] is False
    assert registration["recovery_command"][:4] == [
        "dependabot-watcher",
        "schedule",
        "remove",
        "--platform",
    ]


def test_systemd_inventory_never_trusts_name_matching_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="systemd")
    directory = tmp_path / "systemd"
    directory.mkdir()
    candidate = directory / f"{spec.job_id}.service"
    candidate.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    candidate.chmod(0o600)

    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    report = SystemdUserAdapter(
        runner=_SuccessRunner(), unit_directory=directory
    ).inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert spec.job_id.removeprefix("dependabot-watcher-") in report[
        "untrusted_candidates"
    ][0]


def test_systemd_inventory_reports_manager_only_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    report = SystemdUserAdapter(
        runner=_SystemdInventoryCandidateRunner(spec.job_id),
        unit_directory=tmp_path / "missing-systemd",
    ).inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert spec.job_id in report["untrusted_candidates"][0]


def test_systemd_lifecycle_is_idempotent_and_reports_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(
        runner=runner,
        unit_directory=tmp_path / "systemd",
    )

    preview = adapter.preview(spec)
    first = adapter.install(spec)
    second = adapter.install(spec)
    timer_path = Path(preview["definition"]["timer_path"])
    timer_path.write_text(
        str(preview["definition"]["timer"]).replace(
            "OnUnitActiveSec=60m",
            "OnUnitActiveSec=61m",
        ),
        encoding="utf-8",
    )
    drifted = adapter.status(spec)
    removed = adapter.remove(spec)

    assert preview["changed"] is False
    assert "OnStartupSec=1m" in preview["definition"]["timer"]
    assert "OnBootSec=" not in preview["definition"]["timer"]
    assert "Persistent=" not in preview["definition"]["timer"]
    assert first["status"] == "active"
    assert first["changed"] is True
    assert first["capabilities"]["persistent_local_writes"] is True
    assert second["changed"] is False
    assert drifted["status"] == "drifted"
    assert removed["status"] == "removed"
    assert not Path(preview["definition"]["service_path"]).exists()
    assert not timer_path.exists()


def test_systemd_refuses_foreign_definition_and_handles_owned_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(runner=runner, unit_directory=tmp_path / "systemd")
    preview = adapter.preview(spec)
    service_path = Path(preview["definition"]["service_path"])
    timer_path = Path(preview["definition"]["timer_path"])
    service_path.parent.mkdir(parents=True)
    service_path.write_text("[Unit]\nDescription=unrelated\n", encoding="utf-8")
    service_path.chmod(0o600)

    with pytest.raises(ScheduleError, match="not owned"):
        adapter.install(spec)
    assert runner.commands and all("show" in command for command in runner.commands)

    service_path.write_text(preview["definition"]["service"], encoding="utf-8")
    partial = adapter.status(spec)
    removed = adapter.remove(spec)

    assert partial["status"] == "drifted"
    assert removed["status"] == "removed"
    assert not service_path.exists()
    assert not timer_path.exists()
    assert any("disable" in command for command in runner.commands)


def test_systemd_reports_and_removes_loaded_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(runner=runner, unit_directory=tmp_path / "systemd")

    status = adapter.status(spec)
    removed = adapter.remove(spec)

    assert status["status"] == "drifted"
    assert status["native_status"]["orphaned"] is True
    assert removed["status"] == "removed"
    assert any("disable" in command for command in runner.commands)


def test_systemd_remove_stops_loaded_service_only_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SystemdServiceOrphanRunner()
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(runner=runner, unit_directory=tmp_path / "systemd")

    removed = adapter.remove(spec)

    assert removed["status"] == "removed"
    assert any(
        command[-2:] == ["stop", f"{spec.job_id}.service"]
        for command in runner.commands
    )


def test_systemd_install_refuses_loaded_unowned_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ForeignNativeRunner("systemd")
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(runner=runner, unit_directory=tmp_path / "systemd")

    with pytest.raises(ScheduleError, match="unowned loaded unit"):
        adapter.install(spec)

    assert not (tmp_path / "systemd").exists()


def test_systemd_reports_enabled_but_inactive_timer_as_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SystemdInactiveEnabledRunner()
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(runner=runner, unit_directory=tmp_path / "systemd")
    preview = adapter.preview(spec)
    service_path = Path(preview["definition"]["service_path"])
    timer_path = Path(preview["definition"]["timer_path"])
    service_path.parent.mkdir(parents=True)
    service_path.write_text(preview["definition"]["service"], encoding="utf-8")
    service_path.chmod(0o600)
    timer_path.write_text(preview["definition"]["timer"], encoding="utf-8")
    timer_path.chmod(0o600)

    status = adapter.status(spec)

    assert status["status"] == "drifted"
    assert status["native_status"]["enabled"] is True
    assert status["native_status"]["active"] is False
    assert status["native_status"]["drifted"] is True


def test_launchd_lifecycle_reads_back_owned_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )

    installed = adapter.install(spec)
    status = adapter.status(spec)
    disabled = adapter.disable(spec)
    disabled_status = adapter.status(spec)
    removed = adapter.remove(spec)

    assert installed["status"] == "active"
    assert installed["capabilities"]["persistent_local_writes"] is True
    assert status["native_status"]["drifted"] is False
    assert disabled["status"] == "disabled"
    assert disabled_status["status"] == "disabled"
    assert disabled_status["native_status"]["persistent_disabled"] is True
    assert removed["status"] == "removed"
    assert any("disable" in command for command in runner.commands)
    assert any("enable" in command for command in runner.commands)
    assert not Path(installed["definition"]["path"]).exists()
    assert not (spec.state_directory / "schedules" / f"launchd-{spec.job_id}.json").exists()


def test_launchd_inventory_recovers_owned_profile_without_state_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=_SuccessRunner(),
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    definition = adapter.preview(spec)["definition"]
    path = Path(definition["path"])
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(definition["plist"]))
    path.chmod(0o600)

    report = adapter.inventory()

    assert report["status"] == "complete"
    assert report["registration_count"] == 1
    registration = report["registrations"][0]
    assert registration["job_id"] == spec.job_id
    assert registration["native_identity"] == definition["label"]
    assert registration["native_interval_minutes"] == 60
    assert registration["recovery_command"][4:6] == ["launchd", "--config"]


def test_launchd_inventory_reports_manager_only_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="launchd")
    runner = _SuccessRunner()
    runner.launchd_job_id = spec.job_id
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    report = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "missing-LaunchAgents",
        user_id=501,
    ).inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert spec.job_id.removeprefix("dependabot-watcher-") in report[
        "untrusted_candidates"
    ][0]


def test_launchd_identical_install_does_not_reload_active_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    adapter.install(spec)
    mutations_before = [
        command
        for command in runner.commands
        if any(action in command for action in ("enable", "disable", "bootstrap", "bootout"))
    ]

    repeated = adapter.install(spec)
    mutations_after = [
        command
        for command in runner.commands
        if any(action in command for action in ("enable", "disable", "bootstrap", "bootout"))
    ]

    assert repeated["status"] == "active"
    assert repeated["changed"] is False
    assert mutations_after == mutations_before


def test_launchd_bootstrap_failure_restores_prior_loaded_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _LaunchdBootstrapFailureRunner()
    original = _spec(tmp_path, platform="launchd")
    updated = _spec(tmp_path, platform="launchd", notification_mode="desktop")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    adapter.install(original)
    runner.fail_next_bootstrap = True

    with pytest.raises(ScheduleError, match="LaunchAgent install failed"):
        adapter.install(updated)

    restored = adapter.status(original)
    assert restored["status"] == "active"
    assert restored["native_status"]["drifted"] is False


def test_launchd_loaded_orphan_failure_unloads_attempt_and_preserves_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="launchd")
    runner = _LaunchdBootstrapFailureRunner()
    runner.launchd_job_id = spec.job_id
    runner.fail_next_bootstrap = True
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    registration = Path("schedules") / f"launchd-{spec.job_id}.json"
    save_private_json(
        spec.state_directory,
        registration,
        {
            "schema_version": 1,
            "platform": "launchd",
            "job_id": spec.job_id,
            "label": adapter._label(spec),
            "disabled": False,
        },
    )
    path = Path(adapter.preview(spec)["definition"]["path"])

    with pytest.raises(
        ScheduleError,
        match="prior state could not be restored safely",
    ):
        adapter.install(spec)

    assert runner.launchd_job_id is None
    assert not path.exists()
    assert (spec.state_directory / registration).is_file()


def test_launchd_definition_write_failure_restores_prior_loaded_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    original = _spec(tmp_path, platform="launchd")
    updated = _spec(tmp_path, platform="launchd", notification_mode="desktop")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    adapter.install(original)
    real_atomic_bytes = launchd_module._atomic_bytes
    calls = 0

    def fail_once(path: Path, value: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ScheduleError("simulated definition write failure")
        real_atomic_bytes(path, value)

    monkeypatch.setattr(launchd_module, "_atomic_bytes", fail_once)

    with pytest.raises(ScheduleError, match="simulated definition write failure"):
        adapter.install(updated)

    restored = adapter.status(original)
    assert restored["status"] == "active"
    assert restored["native_status"]["drifted"] is False


def test_launchd_reports_transient_unload_as_drift_not_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    adapter.install(spec)
    runner.launchd_job_id = None

    status = adapter.status(spec)

    assert status["status"] == "drifted"
    assert status["native_status"]["active"] is False
    assert status["native_status"]["persistent_disabled"] is False
    assert status["native_status"]["drifted"] is True


def test_launchd_rejects_untrusted_registration_parent_before_native_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    spec.state_directory.mkdir(mode=0o700)
    outside = tmp_path / "outside-schedules"
    outside.mkdir(mode=0o700)
    os.symlink(outside, spec.state_directory / "schedules")

    with pytest.raises(ScheduleError, match="ownership record path cannot be trusted"):
        adapter.install(spec)

    assert not any(
        operation in command
        for command in runner.commands
        for operation in ("enable", "bootstrap", "bootout")
    )


def test_launchd_registration_failure_rolls_back_bootstrapped_job_and_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    path = Path(adapter.preview(spec)["definition"]["path"])

    def fail_registration(*_args: Any, **_kwargs: Any) -> None:
        raise ScheduleError("simulated ownership record failure")

    monkeypatch.setattr(adapter, "_save_registration", fail_registration)

    with pytest.raises(ScheduleError, match="simulated ownership record failure"):
        adapter.install(spec)

    bootstrap_index = next(
        index for index, command in enumerate(runner.commands) if "bootstrap" in command
    )
    bootout_index = next(
        index for index, command in enumerate(runner.commands) if "bootout" in command
    )
    assert bootstrap_index < bootout_index
    assert runner.launchd_job_id is None
    assert not path.exists()


def test_launchd_refuses_foreign_definition_at_owned_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SuccessRunner()
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    path = Path(adapter.preview(spec)["definition"]["path"])
    path.parent.mkdir(parents=True)
    path.write_bytes(
        plistlib.dumps(
            {"Label": "com.example.unrelated", "ProgramArguments": ["/bin/true"]}
        )
    )
    path.chmod(0o600)

    with pytest.raises(ScheduleError, match="not owned"):
        adapter.install(spec)
    with pytest.raises(ScheduleError, match="not owned"):
        adapter.remove(spec)
    assert runner.commands == []


def test_launchd_reports_and_removes_loaded_owned_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="launchd")
    runner = _LaunchdOrphanRunner(spec.job_id)
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    save_private_json(
        spec.state_directory,
        Path("schedules") / f"launchd-{spec.job_id}.json",
        {
            "schema_version": 1,
            "platform": "launchd",
            "job_id": spec.job_id,
            "label": adapter._label(spec),
        },
    )

    status = adapter.status(spec)
    removed = adapter.remove(spec)

    assert status["status"] == "drifted"
    assert status["native_status"]["orphaned"] is True
    assert removed["status"] == "removed"
    assert any("bootout" in command for command in runner.commands)


def test_launchd_install_boots_out_loaded_owned_orphan_before_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="launchd")
    runner = _LaunchdOrphanRunner(spec.job_id)
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    save_private_json(
        spec.state_directory,
        Path("schedules") / f"launchd-{spec.job_id}.json",
        {
            "schema_version": 1,
            "platform": "launchd",
            "job_id": spec.job_id,
            "label": adapter._label(spec),
        },
    )

    adapter.install(spec)

    bootout_index = next(
        index for index, command in enumerate(runner.commands) if "bootout" in command
    )
    bootstrap_index = next(
        index for index, command in enumerate(runner.commands) if "bootstrap" in command
    )
    assert bootout_index < bootstrap_index


def test_launchd_install_refuses_loaded_unowned_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ForeignNativeRunner("launchd")
    spec = _spec(tmp_path, platform="launchd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )

    with pytest.raises(ScheduleError, match="unowned loaded job"):
        adapter.install(spec)

    assert not (tmp_path / "LaunchAgents").exists()


def test_launchd_remove_keeps_ownership_evidence_when_bootout_does_not_unload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="launchd")
    runner = _LaunchdStickyRunner(spec.job_id)
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.launchd.shutil.which",
        lambda command: f"/bin/{command}",
    )
    adapter = LaunchdAdapter(
        runner=runner,
        agent_directory=tmp_path / "LaunchAgents",
        user_id=501,
    )
    registration = Path("schedules") / f"launchd-{spec.job_id}.json"
    save_private_json(
        spec.state_directory,
        registration,
        {
            "schema_version": 1,
            "platform": "launchd",
            "job_id": spec.job_id,
            "label": adapter._label(spec),
        },
    )

    with pytest.raises(ScheduleError, match="still reports"):
        adapter.remove(spec)

    assert (spec.state_directory / registration).is_file()


def test_windows_wsl_lifecycle_preserves_exact_distro_and_readback(tmp_path: Path) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner)
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu-24.04")

    preview = adapter.preview(spec)
    installed = adapter.install(spec)
    status = adapter.status(spec)
    disabled = adapter.disable(spec)
    disabled_status = adapter.status(spec)
    removed = adapter.remove(spec)

    assert preview["definition"]["command"].endswith("Windows/System32/wsl.exe")
    assert not preview["definition"]["xml"].startswith("<?xml")
    assert preview["definition"]["normalized"]["interval"] == 60
    assert preview["definition"]["arguments"][:6] == [
        "--distribution",
        "Ubuntu-24.04",
        "--user",
        "alex",
        "--exec",
        "/usr/bin/env",
    ]
    assert preview["definition"]["arguments"][6].startswith("PATH=")
    assert preview["definition"]["arguments"][7] == str(spec.executable)
    assert installed["status"] == "active"
    assert installed["capabilities"]["persistent_local_writes"] is True
    assert installed["native_status"]["drifted"] is False
    assert installed["native_status"]["has_run"] is False
    assert installed["native_status"]["last_result"] is None
    assert installed["native_status"]["next_run_at"] == (
        "2026-09-30T00:00:00.0000000+02:00"
    )
    assert status["status"] == "active"
    assert disabled["status"] == "disabled"
    assert disabled_status["status"] == "disabled"
    assert removed["status"] == "removed"
    assert runner.xml is None
    assert all(
        payload["task_path"] == "\\"
        for command, payload in zip(runner.commands, runner.payloads, strict=True)
        if "-TaskPath" in command[-1]
    )


def test_windows_command_line_parser_round_trips_generated_arguments() -> None:
    arguments = [
        "run",
        "",
        "plain",
        "two words",
        'embedded"quote',
        "trailing\\",
        'slashes\\\\before"quote',
    ]

    assert _split_windows_command_line(subprocess.list2cmdline(arguments)) == arguments


@pytest.mark.parametrize(
    ("platform", "distro"),
    [("windows", None), ("wsl", "Ubuntu-24.04")],
)
def test_windows_inventory_recovers_owned_current_user_task(
    tmp_path: Path,
    platform: str,
    distro: str | None,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(
        tmp_path,
        runner,
        inventory_platform=platform,
    )
    spec = _spec(tmp_path, platform=platform, distro=distro)
    runner.xml = adapter.preview(spec)["definition"]["xml"]
    runner.enabled = True

    report = adapter.inventory()

    assert report["status"] == "complete"
    assert report["registration_count"] == 1
    registration = report["registrations"][0]
    assert registration["platform"] == platform
    assert registration["job_id"] == spec.job_id
    assert registration["enabled"] is True
    assert registration["distro"] == distro
    assert registration["recovery_command"][4:6] == [platform, "--config"]


def test_windows_inventory_rejects_marker_owned_by_another_principal(
    tmp_path: Path,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(
        tmp_path,
        runner,
        windows_user="S-1-5-21-current",
        inventory_platform="windows",
    )
    spec = _spec(tmp_path, platform="windows")
    runner.xml = adapter.preview(spec)["definition"]["xml"].replace(
        "S-1-5-21-current",
        "S-1-5-21-other",
    )

    report = adapter.inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert spec.job_id in report["untrusted_candidates"][0]


def test_windows_inventory_fails_closed_when_task_query_fails(tmp_path: Path) -> None:
    adapter = _windows_adapter(tmp_path, _WindowsInventoryFailureRunner())

    with pytest.raises(ScheduleError, match="PowerShell operation failed"):
        adapter.inventory()


@pytest.mark.parametrize("operation", ["install", "status", "disable", "remove"])
def test_windows_lifecycle_fails_closed_when_task_query_fails(
    tmp_path: Path,
    operation: str,
) -> None:
    adapter = _windows_adapter(tmp_path, _WindowsLifecycleFailureRunner())

    with pytest.raises(ScheduleError, match="PowerShell operation failed"):
        getattr(adapter, operation)(_spec(tmp_path, platform="windows"))


def test_windows_inventory_rejects_unexpected_owned_command(tmp_path: Path) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform="windows")
    spec = _spec(tmp_path, platform="windows")
    expected = str(spec.executable)
    runner.xml = adapter.preview(spec)["definition"]["xml"].replace(
        expected,
        str(spec.executable.with_name("other-tool")),
    )

    report = adapter.inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert "invalid executable" in report["untrusted_candidates"][0]


def test_windows_inventory_rejects_missing_native_interval(tmp_path: Path) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform="windows")
    spec = _spec(tmp_path, platform="windows")
    runner.xml = adapter.preview(spec)["definition"]["xml"].replace(
        "<Interval>PT60M</Interval>",
        "",
    )

    report = adapter.inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert "interval is missing" in report["untrusted_candidates"][0]


def test_windows_inventory_rejects_untrusted_wsl_wrapper(tmp_path: Path) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform="wsl")
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu-24.04")
    expected = adapter.preview(spec)["definition"]["command"]
    runner.xml = adapter.preview(spec)["definition"]["xml"].replace(
        expected,
        str(tmp_path / "Untrusted/wsl.exe"),
    )

    report = adapter.inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert "untrusted wrapper" in report["untrusted_candidates"][0]


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("--user alex", "--user root"),
        ("PATH=", "PATH=/tmp#"),
    ],
)
def test_windows_inventory_rejects_altered_wsl_user_or_path(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform="wsl")
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu-24.04")
    xml = adapter.preview(spec)["definition"]["xml"]
    assert original in xml
    runner.xml = xml.replace(original, replacement, 1)

    report = adapter.inventory()

    assert report["status"] == "attention"
    assert report["registrations"] == []
    assert "untrusted user or PATH" in report["untrusted_candidates"][0]


def test_windows_wsl_desktop_schedule_binds_resolved_powershell_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(
        tmp_path,
        platform="wsl",
        distro="Ubuntu-24.04",
        notification_mode="desktop",
    )
    preview = _windows_adapter(tmp_path, _WindowsRunner()).preview(spec)

    assert preview["definition"]["arguments"][6].endswith(
        ":" + str(tmp_path / "Windows/System32/WindowsPowerShell/v1.0")
    )


def test_windows_scheduler_refuses_colliding_unowned_task(tmp_path: Path) -> None:
    runner = _WindowsRunner()
    runner.xml = (
        '<?xml version="1.0"?>'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<RegistrationInfo><Source>Another product</Source></RegistrationInfo>"
        "</Task>"
    )
    runner.enabled = True
    adapter = _windows_adapter(tmp_path, runner, windows_user="S-1-5-21-test")
    spec = _spec(tmp_path, platform="windows")

    with pytest.raises(ScheduleError, match="not owned"):
        adapter.status(spec)
    with pytest.raises(ScheduleError, match="not owned"):
        adapter.remove(spec)

    assert runner.xml is not None


def test_windows_scheduler_rejects_owned_markers_under_another_principal(
    tmp_path: Path,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, windows_user="S-1-5-21-current")
    spec = _spec(tmp_path, platform="windows")
    runner.xml = adapter.preview(spec)["definition"]["xml"].replace(
        "S-1-5-21-current",
        "S-1-5-21-other",
    )
    runner.enabled = True

    with pytest.raises(ScheduleError, match="not owned"):
        adapter.status(spec)
    with pytest.raises(ScheduleError, match="not owned"):
        adapter.remove(spec)

    assert runner.xml is not None


def test_windows_scheduler_reports_extra_trigger_and_action_as_drift(
    tmp_path: Path,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, windows_user="S-1-5-21-current")
    spec = _spec(tmp_path, platform="windows")
    adapter.install(spec)
    assert runner.xml is not None
    root = ET.fromstring(runner.xml)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    triggers = root.find("t:Triggers", namespace)
    actions = root.find("t:Actions", namespace)
    assert triggers is not None
    assert actions is not None
    ET.SubElement(triggers, "{http://schemas.microsoft.com/windows/2004/02/mit/task}TimeTrigger")
    ET.SubElement(actions, "{http://schemas.microsoft.com/windows/2004/02/mit/task}Exec")
    runner.xml = ET.tostring(root, encoding="unicode")

    status = adapter.status(spec)

    assert status["status"] == "drifted"


def test_windows_scheduler_normalizes_omitted_false_repetition_default(
    tmp_path: Path,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, windows_user="S-1-5-21-current")
    spec = _spec(tmp_path, platform="windows")
    adapter.install(spec)
    assert runner.xml is not None
    root = ET.fromstring(runner.xml)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    repetition = root.find("t:Triggers/t:TimeTrigger/t:Repetition", namespace)
    stop_at_duration_end = root.find(
        "t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd",
        namespace,
    )
    assert repetition is not None
    assert stop_at_duration_end is not None
    repetition.remove(stop_at_duration_end)
    runner.xml = ET.tostring(root, encoding="unicode")

    status = adapter.status(spec)

    assert status["status"] == "active"
    assert status["native_status"]["drifted"] is False


def test_windows_scheduler_reports_added_repetition_duration_as_drift(
    tmp_path: Path,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, windows_user="S-1-5-21-current")
    spec = _spec(tmp_path, platform="windows")
    adapter.install(spec)
    assert runner.xml is not None
    root = ET.fromstring(runner.xml)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    repetition = root.find("t:Triggers/t:TimeTrigger/t:Repetition", namespace)
    assert repetition is not None
    duration = ET.SubElement(
        repetition,
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Duration",
    )
    duration.text = "PT8H"
    runner.xml = ET.tostring(root, encoding="unicode")

    status = adapter.status(spec)

    assert status["status"] == "drifted"
    assert status["native_status"]["drifted"] is True


@pytest.mark.parametrize("operation", ["install", "status", "disable", "remove"])
def test_windows_scheduler_rejects_owned_task_outside_root_before_lifecycle_action(
    tmp_path: Path,
    operation: str,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, windows_user="S-1-5-21-current")
    spec = _spec(tmp_path, platform="windows")
    adapter.install(spec)
    runner.task_path = "\\Unexpected\\"
    command_count = len(runner.commands)

    with pytest.raises(ScheduleError, match="outside the expected root task path"):
        getattr(adapter, operation)(spec)

    new_scripts = [command[-1] for command in runner.commands[command_count:]]
    assert len(new_scripts) == 1
    assert "Get-ScheduledTask" in new_scripts[0]
    assert not any(
        mutation in new_scripts[0]
        for mutation in (
            "Register-ScheduledTask",
            "Disable-ScheduledTask",
            "Unregister-ScheduledTask",
        )
    )


def test_platform_detection_distinguishes_wsl_and_native_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dependabot_watcher.scheduling.os.name", "posix")
    monkeypatch.setattr("dependabot_watcher.scheduling.sys.platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert detect_schedule_platform() == "wsl"

    monkeypatch.delenv("WSL_DISTRO_NAME")
    assert detect_schedule_platform() == "systemd"

    with pytest.raises(ScheduleError, match="incompatible"):
        detect_schedule_platform("windows")


def test_schedule_cleanup_does_not_require_resolving_current_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    class _Adapter:
        def remove(self, spec):
            captured.append(spec)
            return {"status": "missing"}

    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr("dependabot_watcher.schedulers.common.shutil.which", lambda command: None)
    monkeypatch.setattr(
        "dependabot_watcher.scheduling.scheduler_adapter",
        lambda platform, runner=None: _Adapter(),
    )

    result = manage_schedule(
        "remove",
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        interval_minutes=60,
        requested_platform="systemd",
    )

    assert result["status"] == "missing"
    assert captured[0].executable.name == "dependabot-watcher"


def test_schedule_install_requires_a_manual_verification_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "dependabot-watcher"
    executable.write_text("placeholder", encoding="utf-8")
    executable.chmod(0o700)

    class _Adapter:
        def install(self, spec):
            return {"status": "active", "next": None}

    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(
        "dependabot_watcher.scheduling.scheduler_adapter",
        lambda platform, runner=None: _Adapter(),
    )

    result = manage_schedule(
        "install",
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        interval_minutes=60,
        requested_platform="systemd",
        executable=str(executable),
    )

    assert "manual verification run does not prove" in result["next"]
    verification = result["verification"]["argv"]
    assert "--config" in verification
    assert "--state-dir" in verification
    assert "--expected-interval-minutes" in verification
    assert "--notify" in verification
    assert "--quiet" not in verification
    assert verification[verification.index("--format") + 1] == "text"


def test_schedule_inventory_requires_no_profile_or_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter:
        def inventory(self):
            return {"action": "inventory", "status": "complete"}

    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(
        "dependabot_watcher.scheduling.scheduler_adapter",
        lambda platform, runner=None: _Adapter(),
    )

    assert inventory_schedules(requested_platform="systemd") == {
        "action": "inventory",
        "status": "complete",
    }


def test_schedule_inventory_human_output_exposes_recovery_and_untrusted_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, platform="systemd")
    monkeypatch.setattr(
        "dependabot_watcher.schedulers.systemd.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    adapter = SystemdUserAdapter(
        runner=_SuccessRunner(), unit_directory=tmp_path / "systemd"
    )
    definition = adapter.preview(spec)["definition"]
    service_path = Path(definition["service_path"])
    timer_path = Path(definition["timer_path"])
    service_path.parent.mkdir(parents=True)
    service_path.write_text(definition["service"], encoding="utf-8")
    timer_path.write_text(definition["timer"], encoding="utf-8")
    service_path.chmod(0o600)
    timer_path.chmod(0o600)
    untrusted = service_path.parent / "dependabot-watcher-not-a-hash.service"
    untrusted.write_text("unrelated", encoding="utf-8")
    untrusted.chmod(0o600)
    report = adapter.inventory()

    text = render_schedule(report, "text")
    markdown = render_schedule(report, "markdown")

    assert "Verified registrations: 1" in text
    assert "Recovery: dependabot-watcher schedule remove" in text
    assert "Untrusted candidates (never removal authority)" in text
    assert "GitHub mutation authorized: **no**" in markdown


def test_schedule_human_output_exposes_authority_and_no_github_mutation(
    tmp_path: Path,
) -> None:
    report = _windows_adapter(tmp_path, _WindowsRunner()).preview(
        _spec(tmp_path, platform="wsl", distro="Ubuntu")
    )

    text = render_schedule(report, "text")
    markdown = render_schedule(report, "markdown")
    structured = json.loads(render_schedule(report, "json"))

    assert "Missed runs: one current-state catch-up" in text
    assert "GitHub mutation authorized: no" in text
    assert "Authority: DESKTOP\\alex" in text
    assert "'--distribution' 'Ubuntu' '--user' 'alex'" in text
    assert "Output: Watcher runtime state" in text
    assert "GitHub mutation authorized: **no**" in markdown
    assert structured["definition"]["authority"] == "DESKTOP\\alex"


def test_schedule_supervisor_disclosure_does_not_claim_all_ai_is_off(tmp_path: Path) -> None:
    config = parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}],
        "supervisor": {"mode": "approval_first", "provider": "codex_app_server", "model": "chosen-model",
            "reasoning_effort": "low", "expected_account_type": "chatgpt", "access": "inherit",
            "model_use_acknowledged": True, "workspaces": [{"repository": "example-org/example-app",
                "directory": str(tmp_path / "maintenance"), "instruction_sources": []}]}})
    profile = recurring_profile_summary(config)
    assert profile["advisory"]["mode"] == "off"
    assert profile["supervisor"]["assurance"] == "saved_policy_only"
    assert profile["supervisor"]["github_mutations_authorized"] is False
    lines = "\n".join(scheduling_output._profile_text(profile))
    assert "chosen-model" in lines and "low" in lines and "access inherit" in lines
    assert "subscription/API usage" in lines and "Saved policy only" in lines
    assert "AI during scheduled runs: off" not in lines
    assert "GitHub changes: disabled" in lines
    assert not (tmp_path / "maintenance").exists()


def test_schedule_install_output_exposes_recurring_choices_and_native_timing(
    tmp_path: Path,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner)
    report = adapter.install(_spec(tmp_path, platform="wsl", distro="Ubuntu"))
    report["profile"] = {
        "repository_count": 3,
        "repositories": ["one/example", "two/example"],
        "advisory": {"mode": "off", "provider": None},
        "operation": {"mutation_enabled": False},
    }

    text = render_schedule(report, "text")

    assert "Cadence: every 1 hour" in text
    assert "Repositories watched: one/example, two/example" in text
    assert "AI during scheduled runs: off" in text
    assert "GitHub changes: disabled" in text
    assert "Next scheduled run: 2026-09-30T00:00:00.0000000+02:00" in text
    assert "Last scheduled run: never" in text


def test_windows_recovery_command_is_powershell_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduling_output.os, "name", "nt")

    rendered = scheduling_output._command(
        ["dependabot-watcher", "schedule", "remove", "C:\\O'Brien; Remove-Item *"]
    )

    assert rendered == (
        "& 'dependabot-watcher' 'schedule' 'remove' "
        "'C:\\O''Brien; Remove-Item *'"
    )
