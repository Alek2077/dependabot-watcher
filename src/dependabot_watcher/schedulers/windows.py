from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from .common import (
    CommandRunner,
    ScheduleError,
    ScheduleSpec,
    SubprocessRunner,
    require_success,
    schedule_report,
)
from ..windows_helpers import (
    WindowsHelperError,
    trusted_windows_executable,
    windows_host_path,
)


_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_ROOT_TASK_PATH = "\\"
_DURATION = re.compile(
    r"^P(?:(?P<days>[0-9]+)D)?(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?)?$"
)
ET.register_namespace("", _TASK_NAMESPACE)


def _tag(name: str) -> str:
    return f"{{{_TASK_NAMESPACE}}}{name}"


def _text(parent: ET.Element, name: str, value: str) -> ET.Element:
    element = ET.SubElement(parent, _tag(name))
    element.text = value
    return element


def _interval_minutes(value: str | None) -> int | str | None:
    if value is None:
        return None
    match = _DURATION.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        return value
    return (
        int(match.group("days") or 0) * 1440
        + int(match.group("hours") or 0) * 60
        + int(match.group("minutes") or 0)
    )


class WindowsTaskSchedulerAdapter:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        windows_user: str | None = None,
        powershell_path: Path | None = None,
        wsl_path: Path | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._windows_user = windows_user
        self._powershell_path = powershell_path
        self._wsl_path = wsl_path

    def _powershell(self) -> str:
        if self._powershell_path is not None:
            if not self._powershell_path.is_file():
                raise ScheduleError("The configured Windows PowerShell executable is unavailable.")
            return str(self._powershell_path)
        try:
            return str(
                trusted_windows_executable("WindowsPowerShell/v1.0/powershell.exe")
            )
        except WindowsHelperError as exc:
            raise ScheduleError(str(exc)) from exc

    def _wsl(self) -> str:
        if self._wsl_path is not None:
            if not self._wsl_path.is_file():
                raise ScheduleError("The configured WSL executable is unavailable.")
            return str(self._wsl_path)
        try:
            return windows_host_path(trusted_windows_executable("wsl.exe"))
        except WindowsHelperError as exc:
            raise ScheduleError(str(exc)) from exc

    def _run_script(self, script: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self._runner.run(
            [self._powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
            input_text=json.dumps(dict(payload)),
        )
        require_success(result, "PowerShell operation")
        try:
            parsed = json.loads(result.stdout)
        except (ValueError, RecursionError) as exc:
            raise ScheduleError("Windows Task Scheduler returned malformed status output.") from exc
        if not isinstance(parsed, dict):
            raise ScheduleError("Windows Task Scheduler returned an invalid status shape.")
        return parsed

    def _user(self) -> str:
        if self._windows_user:
            return self._windows_user
        result = self._runner.run(
            [
                self._powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ]
        )
        require_success(result, "current-user discovery")
        value = result.stdout.strip()
        if not value or "\n" in value or "\r" in value:
            raise ScheduleError("Windows user identity could not be determined safely.")
        self._windows_user = value
        return value

    def _action(self, spec: ScheduleSpec) -> tuple[str, list[str]]:
        if spec.platform == "wsl":
            if not spec.distro:
                raise ScheduleError("WSL scheduling requires an explicit distribution name.")
            if not spec.linux_user:
                raise ScheduleError("WSL scheduling requires an explicit Linux user.")
            path_entries = [
                str(Path.home() / ".local/bin"),
                str(Path.home() / ".npm-global/bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
            if spec.notification_mode == "desktop":
                path_entries.append(str(Path(self._powershell()).parent))
            linux_path = ":".join(path_entries)
            return (
                self._wsl(),
                [
                    "--distribution",
                    spec.distro,
                    "--user",
                    spec.linux_user,
                    "--exec",
                    "/usr/bin/env",
                    f"PATH={linux_path}",
                    *spec.run_argv,
                ],
            )
        return str(spec.executable), list(spec.run_argv[1:])

    def _task_xml(self, spec: ScheduleSpec) -> str:
        command, arguments = self._action(spec)
        root = ET.Element(_tag("Task"), {"version": "1.4"})
        registration = ET.SubElement(root, _tag("RegistrationInfo"))
        _text(registration, "Source", "Dependabot Watcher")
        _text(registration, "URI", f"\\{spec.job_id}")
        _text(registration, "Description", "Dependabot Watcher reconciliation")
        triggers = ET.SubElement(root, _tag("Triggers"))
        trigger = ET.SubElement(triggers, _tag("TimeTrigger"))
        repetition = ET.SubElement(trigger, _tag("Repetition"))
        _text(repetition, "Interval", f"PT{spec.interval_minutes}M")
        _text(repetition, "StopAtDurationEnd", "false")
        _text(trigger, "StartBoundary", "2000-01-01T00:00:00")
        _text(trigger, "Enabled", "true")
        principals = ET.SubElement(root, _tag("Principals"))
        principal = ET.SubElement(principals, _tag("Principal"), {"id": "Author"})
        _text(principal, "UserId", self._user())
        _text(principal, "LogonType", "InteractiveToken")
        _text(principal, "RunLevel", "LeastPrivilege")
        settings = ET.SubElement(root, _tag("Settings"))
        _text(settings, "MultipleInstancesPolicy", "IgnoreNew")
        _text(settings, "DisallowStartIfOnBatteries", "false")
        _text(settings, "StopIfGoingOnBatteries", "false")
        _text(settings, "AllowHardTerminate", "true")
        _text(settings, "StartWhenAvailable", "true")
        _text(settings, "RunOnlyIfNetworkAvailable", "true")
        _text(settings, "Enabled", "true")
        _text(settings, "ExecutionTimeLimit", "PT2H")
        actions = ET.SubElement(root, _tag("Actions"), {"Context": "Author"})
        execution = ET.SubElement(actions, _tag("Exec"))
        _text(execution, "Command", command)
        _text(execution, "Arguments", subprocess.list2cmdline(arguments))
        return ET.tostring(root, encoding="unicode", xml_declaration=False)

    def _normalized_xml(self, xml: str) -> dict[str, Any]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise ScheduleError("Windows Task Scheduler XML could not be parsed safely.") from exc

        def value(path: str) -> str | None:
            item = root.find(path, {"t": _TASK_NAMESPACE})
            return item.text if item is not None else None

        triggers = root.findall("t:Triggers/*", {"t": _TASK_NAMESPACE})
        time_triggers = root.findall(
            "t:Triggers/t:TimeTrigger", {"t": _TASK_NAMESPACE}
        )
        actions = root.findall("t:Actions/*", {"t": _TASK_NAMESPACE})
        exec_actions = root.findall("t:Actions/t:Exec", {"t": _TASK_NAMESPACE})
        principals = root.findall(
            "t:Principals/t:Principal", {"t": _TASK_NAMESPACE}
        )
        actions_element = root.find("t:Actions", {"t": _TASK_NAMESPACE})

        return {
            "source": value("t:RegistrationInfo/t:Source"),
            "uri": value("t:RegistrationInfo/t:URI"),
            "interval": _interval_minutes(
                value("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval")
            ),
            "repetition_duration": value(
                "t:Triggers/t:TimeTrigger/t:Repetition/t:Duration"
            ),
            "stop_at_duration_end": value(
                "t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd"
            )
            or "false",
            "trigger_count": len(triggers),
            "time_trigger_count": len(time_triggers),
            "start_boundary": value("t:Triggers/t:TimeTrigger/t:StartBoundary"),
            "task_enabled": value("t:Settings/t:Enabled") or "true",
            "trigger_enabled": value("t:Triggers/t:TimeTrigger/t:Enabled") or "true",
            "start_when_available": value("t:Settings/t:StartWhenAvailable"),
            "multiple_instances": value("t:Settings/t:MultipleInstancesPolicy"),
            "network_required": value("t:Settings/t:RunOnlyIfNetworkAvailable"),
            "run_level": value("t:Principals/t:Principal/t:RunLevel") or "LeastPrivilege",
            "user_id": value("t:Principals/t:Principal/t:UserId"),
            "logon_type": value("t:Principals/t:Principal/t:LogonType"),
            "principal_count": len(principals),
            "execution_time_limit": value("t:Settings/t:ExecutionTimeLimit"),
            "disallow_start_on_batteries": value("t:Settings/t:DisallowStartIfOnBatteries"),
            "stop_on_batteries": value("t:Settings/t:StopIfGoingOnBatteries"),
            "allow_hard_terminate": (
                value("t:Settings/t:AllowHardTerminate") or "true"
            ),
            "actions_context": (
                actions_element.attrib.get("Context")
                if actions_element is not None
                else None
            ),
            "action_count": len(actions),
            "exec_action_count": len(exec_actions),
            "command": value("t:Actions/t:Exec/t:Command"),
            "arguments": value("t:Actions/t:Exec/t:Arguments"),
        }

    def _require_owned_xml(self, spec: ScheduleSpec, xml: object) -> str:
        if not isinstance(xml, str):
            raise ScheduleError("Installed Windows task did not return a readable definition.")
        normalized = self._normalized_xml(xml)
        if (
            normalized.get("source") != "Dependabot Watcher"
            or normalized.get("uri") != f"\\{spec.job_id}"
            or normalized.get("user_id") != self._user()
        ):
            raise ScheduleError(
                "The Watcher task name is occupied by a task not owned by Dependabot Watcher."
            )
        return xml

    @staticmethod
    def _require_root_task_path(native: Mapping[str, Any]) -> None:
        if native.get("task_path") != _ROOT_TASK_PATH:
            raise ScheduleError("Installed Windows task is outside the expected root task path.")

    def _definition(self, spec: ScheduleSpec) -> dict[str, Any]:
        xml = self._task_xml(spec)
        command, arguments = self._action(spec)
        return {
            "task_name": spec.job_id,
            "xml": xml,
            "normalized": self._normalized_xml(xml),
            "authority": self._user(),
            "command": command,
            "arguments": arguments,
            "output": "Watcher runtime state plus Windows Task Scheduler history",
        }

    def preview(self, spec: ScheduleSpec) -> dict[str, Any]:
        return schedule_report(
            action="preview",
            spec=spec,
            status="preview",
            definition=self._definition(spec),
            next_step="Run `dependabot-watcher schedule install` to create this current-user task.",
        )

    def _native_status(self, spec: ScheduleSpec) -> dict[str, Any]:
        script = """
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$task = Get-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name -ErrorAction SilentlyContinue
if ($null -eq $task) {
  @{installed=$false; state='Missing'; enabled=$false; xml=$null} | ConvertTo-Json -Compress
  exit 0
}
$xml = Export-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name
@{installed=$true; state=$task.State.ToString(); enabled=($task.Settings.Enabled -eq $true); task_path=$task.TaskPath; xml=$xml} | ConvertTo-Json -Compress
""".strip()
        return self._run_script(
            script,
            {"name": spec.job_id, "task_path": _ROOT_TASK_PATH},
        )

    def install(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        prior = self._native_status(spec)
        prior_xml = prior.get("xml")
        if prior.get("installed"):
            self._require_root_task_path(prior)
            prior_xml = self._require_owned_xml(spec, prior_xml)
        changed = not prior.get("installed") or (
            self._normalized_xml(str(prior_xml)) != definition["normalized"]
        )
        script = """
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
Register-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name -Xml $payload.xml -Force | Out-Null
$task = Get-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name -ErrorAction Stop
$xml = Export-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name
@{installed=$true; state=$task.State.ToString(); enabled=($task.Settings.Enabled -eq $true); task_path=$task.TaskPath; xml=$xml} | ConvertTo-Json -Compress
""".strip()
        installed = self._run_script(
            script,
            {
                "name": spec.job_id,
                "task_path": _ROOT_TASK_PATH,
                "xml": definition["xml"],
            },
        )
        self._require_root_task_path(installed)
        report = self.status(spec)
        report.update(action="install", changed=changed)
        report["capabilities"]["persistent_local_writes"] = True
        return report

    def status(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        native = self._native_status(spec)
        if not native.get("installed"):
            return schedule_report(
                action="status",
                spec=spec,
                status="missing",
                definition=definition,
                native_status={"installed": False, "active": False, "enabled": False},
            )
        self._require_root_task_path(native)
        xml = self._require_owned_xml(spec, native.get("xml"))
        installed_definition = self._normalized_xml(xml)
        drifted = any(
            value != installed_definition.get(key)
            for key, value in definition["normalized"].items()
            if key != "task_enabled"
        )
        enabled = bool(native.get("enabled"))
        status = "drifted" if drifted else "active" if enabled else "disabled"
        return schedule_report(
            action="status",
            spec=spec,
            status=status,
            definition=definition,
            native_status={
                "installed": True,
                "enabled": enabled,
                "state": native.get("state"),
                "drifted": drifted,
            },
        )

    def disable(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        prior = self._native_status(spec)
        if not prior.get("installed"):
            return schedule_report(
                action="disable",
                spec=spec,
                status="missing",
                definition=definition,
                changed=False,
            )
        self._require_root_task_path(prior)
        self._require_owned_xml(spec, prior.get("xml"))
        script = """
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$task = Get-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name -ErrorAction SilentlyContinue
if ($null -eq $task) {
  @{installed=$false; changed=$false} | ConvertTo-Json -Compress
  exit 0
}
Disable-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name | Out-Null
@{installed=$true; changed=$true} | ConvertTo-Json -Compress
""".strip()
        native = self._run_script(
            script,
            {"name": spec.job_id, "task_path": _ROOT_TASK_PATH},
        )
        return schedule_report(
            action="disable",
            spec=spec,
            status="disabled" if native.get("installed") else "missing",
            definition=definition,
            changed=bool(native.get("changed")),
        )

    def remove(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        prior = self._native_status(spec)
        if not prior.get("installed"):
            return schedule_report(
                action="remove",
                spec=spec,
                status="missing",
                definition=definition,
                changed=False,
                next_step="Watcher configuration, run history, plans, and execution audit were preserved.",
            )
        self._require_root_task_path(prior)
        self._require_owned_xml(spec, prior.get("xml"))
        script = """
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$task = Get-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name -ErrorAction SilentlyContinue
if ($null -eq $task) {
  @{installed=$false; changed=$false} | ConvertTo-Json -Compress
  exit 0
}
Unregister-ScheduledTask -TaskPath $payload.task_path -TaskName $payload.name -Confirm:$false
@{installed=$false; changed=$true} | ConvertTo-Json -Compress
""".strip()
        native = self._run_script(
            script,
            {"name": spec.job_id, "task_path": _ROOT_TASK_PATH},
        )
        changed = bool(native.get("changed"))
        return schedule_report(
            action="remove",
            spec=spec,
            status="removed" if changed else "missing",
            definition=definition,
            changed=changed,
            next_step="Watcher configuration, run history, plans, and execution audit were preserved.",
        )
