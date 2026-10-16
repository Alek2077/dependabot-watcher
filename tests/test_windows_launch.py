from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from dependabot_watcher.schedulers.common import ScheduleError
from dependabot_watcher.schedulers.windows_launch import (
    launcher_path,
    launcher_source,
    verified_launcher_argv,
)
from test_scheduling import _WindowsRunner, _spec, _windows_adapter


ROOT = "C:\\Users\\test\\AppData\\Local\\DependabotWatcher\\schedules"
JOB = "dependabot-watcher-0123456789ab"


def test_fixed_launcher_preserves_quoted_argv_wait_and_exit() -> None:
    argv = ["C:\\Windows\\System32\\wsl.exe", "--exec", "/path with spaces/tool", 'one"quote', "trailing\\", "& literal"]
    source = launcher_source(argv)
    path = launcher_path(ROOT, JOB, source)
    assert verified_launcher_argv(ROOT, JOB, path, source) == argv
    assert json.dumps(subprocess.list2cmdline(argv)) + ", 0, true)" in source
    assert "WScript.Quit(shell.Run(" in source
    assert "WScript.Arguments.length !== 0" in source


@pytest.mark.parametrize("argument", ["%PATH%", "line\nbreak", "\x00", "\r"])
def test_launcher_rejects_expansion_or_controls(argument: str) -> None:
    with pytest.raises(ScheduleError):
        launcher_source(["tool", argument])


@pytest.mark.parametrize("mutation", ["content", "path", "missing"])
def test_inventory_rejects_changed_launcher(mutation: str) -> None:
    source = launcher_source(["tool", "run"])
    path = launcher_path(ROOT, JOB, source)
    with pytest.raises(ScheduleError):
        verified_launcher_argv(ROOT, JOB, path + (".other" if mutation == "path" else ""), None if mutation == "missing" else source + ("// changed\n" if mutation == "content" else ""))


def test_preview_does_not_write_and_install_detects_launcher_drift(tmp_path: Path) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner)
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu")
    preview = adapter.preview(spec)
    assert not runner.launchers
    assert preview["definition"]["normalized"]["command"].endswith("wscript.exe")
    assert adapter.install(spec)["status"] == "active"
    path = preview["definition"]["launcher"]["path"]
    runner.launchers[path] += "// modified\n"
    assert adapter.status(spec)["status"] == "drifted"
    assert adapter.inventory()["registration_count"] == 0
    with pytest.raises(ScheduleError):
        adapter.install(spec)


@pytest.mark.parametrize(("platform", "distro"), [("wsl", "Ubuntu"), ("windows", None)])
def test_legacy_direct_task_can_be_migrated_and_removed(tmp_path: Path, platform: str, distro: str | None) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform=platform)
    spec = _spec(tmp_path, platform=platform, distro=distro)
    definition = adapter.preview(spec)["definition"]
    root = ET.fromstring(definition["xml"])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = root.find("t:Actions/t:Exec/t:Command", ns)
    arguments = root.find("t:Actions/t:Exec/t:Arguments", ns)
    assert command is not None and arguments is not None
    command.text = definition["command"]
    arguments.text = subprocess.list2cmdline(definition["arguments"])
    runner.xml = ET.tostring(root, encoding="unicode")
    runner.enabled = True
    assert adapter.inventory()["registration_count"] == 1
    assert adapter.inventory()["status"] == "attention"
    assert adapter.inventory()["registrations"][0]["drifted"] is True
    assert adapter.status(spec)["status"] == "drifted"
    assert adapter.install(spec)["status"] == "active"
    assert adapter.remove(spec)["status"] == "removed"


def test_command_size_is_bounded_before_provisioning() -> None:
    with pytest.raises(ScheduleError, match="command-line limit"):
        launcher_source(["tool", "a" * 32767])


@pytest.mark.parametrize("missing", ["_wscript", "_launcher_root"])
@pytest.mark.parametrize("operation", ["status", "disable", "remove"])
def test_owned_legacy_management_without_replacement_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str, operation: str,
) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner)
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu")
    definition = adapter.preview(spec)["definition"]
    root = ET.fromstring(definition["xml"])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = root.find("t:Actions/t:Exec/t:Command", ns)
    arguments = root.find("t:Actions/t:Exec/t:Arguments", ns)
    assert command is not None and arguments is not None
    command.text = definition["command"]
    arguments.text = subprocess.list2cmdline(definition["arguments"])
    runner.xml = ET.tostring(root, encoding="unicode")
    runner.enabled = True

    def unavailable() -> str:
        raise ScheduleError("Replacement prerequisite unavailable")

    monkeypatch.setattr(adapter, missing, unavailable)
    report = getattr(adapter, operation)(spec)
    assert report["status"] == {"status": "drifted", "disable": "disabled", "remove": "removed"}[operation]
    if operation == "status":
        assert "Replacement prerequisite unavailable" in report["next"]
        assert runner.enabled and runner.xml is not None
    elif operation == "disable":
        assert not runner.enabled and runner.xml is not None
    else:
        assert runner.xml is None
    assert not runner.launchers

    # Recovery still refuses a task with foreign ownership, even without a
    # usable replacement launcher. No disable/unregister call may escape.
    runner.xml = ET.tostring(root, encoding="unicode").replace("Dependabot Watcher", "Other owner")
    before = len(runner.commands)
    with pytest.raises(ScheduleError, match="not owned"):
        getattr(adapter, operation)(spec)
    assert not any("Disable-ScheduledTask" in call[-1] or "Unregister-ScheduledTask" in call[-1]
                   for call in runner.commands[before:])


@pytest.mark.parametrize(("old", "new"), [("LeastPrivilege", "HighestAvailable"), ("InteractiveToken", "Password"), ("IgnoreNew", "Parallel"), ("PT2H", "PT72H")])
def test_inventory_detects_principal_and_runtime_drift(tmp_path: Path, old: str, new: str) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform="wsl")
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu")
    assert adapter.install(spec)["status"] == "active"
    assert runner.xml is not None
    runner.xml = runner.xml.replace(old, new)
    report = adapter.inventory()
    assert report["status"] == "attention"
    assert report["registrations"][0]["drifted"] is True


@pytest.mark.parametrize(("parent", "field", "value"), [("Triggers/TimeTrigger", "RandomDelay", "PT10M"), ("Settings", "WakeToRun", "true"), ("Settings", "RunOnlyIfIdle", "true"), ("Actions/Exec", "WorkingDirectory", "C:\\Other")])
def test_status_and_inventory_detect_added_behavior_fields(tmp_path: Path, parent: str, field: str, value: str) -> None:
    runner = _WindowsRunner()
    adapter = _windows_adapter(tmp_path, runner, inventory_platform="wsl")
    spec = _spec(tmp_path, platform="wsl", distro="Ubuntu")
    adapter.install(spec)
    assert runner.xml is not None
    root = ET.fromstring(runner.xml)
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    target = root.find("/".join("{" + ns + "}" + part for part in parent.split("/")))
    assert target is not None
    ET.SubElement(target, "{" + ns + "}" + field).text = value
    runner.xml = ET.tostring(root, encoding="unicode")
    assert adapter.status(spec)["status"] == "drifted"
    assert adapter.inventory()["registrations"][0]["drifted"] is True
