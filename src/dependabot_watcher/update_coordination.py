"""Exact-owner graceful exit and reversible pause of an existing WSL task.

No process-name kill, task installation, trigger rewrite or Git operation lives
here. Replacement is coordinated by the updater under the existing state lock.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .native_review import NativeReviewError, _component, _unc_path, _wsl_identity
from .runtime_state import load_private_json
from .schedulers.common import ScheduleError, ScheduleSpec, schedule_job_id, schedule_spec
from .schedulers.windows import WindowsTaskSchedulerAdapter, _split_windows_command_line
from .windows_helpers import trusted_windows_executable, windows_host_path


class UpdateError(RuntimeError):
    """An update was refused or needs explicit recovery; no action replay."""


def task_definition_identity(xml: str) -> str:
    """Preserve the whole definition, ignoring only the temporary enabled bit."""
    root = ET.fromstring(xml)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    settings = root.find("t:Settings", namespace)
    if settings is not None:
        for enabled in settings.findall("t:Enabled", namespace):
            settings.remove(enabled)
    return hashlib.sha256(ET.canonicalize(ET.tostring(root, encoding="unicode"), strip_text=True).encode()).hexdigest()


class UpdateSchedule:
    """Use the observed task; never synthesize a replacement definition."""

    def __init__(self, config_path: Path, state: Path, installed_command: Path) -> None:
        self.config_path, self.state, self.installed_command = config_path, state, installed_command
        distro, user = _wsl_identity()
        self.adapter = WindowsTaskSchedulerAdapter(inventory_platform="wsl", expected_wsl_user=user)
        inventory = self.adapter.inventory()
        job_id = schedule_job_id(config_path)
        if any(job_id in item for item in inventory["untrusted_candidates"]):
            raise UpdateError("The selected task cannot be verified as Watcher-owned; it was not changed.")
        matches = [item for item in inventory["registrations"] if item["job_id"] == job_id]
        self.spec: ScheduleSpec | None = None
        if not matches:
            return
        if len(matches) != 1:
            raise UpdateError("The selected schedule is ambiguous.")
        observed = matches[0]
        command = observed["command"]
        if (observed["config_path"] != str(config_path) or observed["state_directory"] != str(state)
                or observed["distro"] != distro or observed["drifted"]
                or len(command) < 9 or Path(command[8]).resolve() != installed_command.resolve()):
            raise UpdateError("The existing schedule differs from the selected installation/profile; no task was changed.")
        self.spec = schedule_spec(platform="wsl", config_path=config_path, state_directory=state,
            interval_minutes=observed["interval_minutes"], notification_mode=observed["notification_mode"],
            distro=distro, linux_user=user, executable=str(installed_command), require_executable=False)

    def snapshot(self) -> dict[str, Any] | None:
        # Inventory again at every final admission/mutation boundary. The task
        # can have been installed, removed or rebound while the wheel built.
        current = UpdateSchedule(self.config_path, self.state, self.installed_command)
        if current.spec != self.spec:
            raise UpdateError("The selected schedule changed during update preparation; no replacement was attempted.")
        if self.spec is None:
            return None
        native = self.adapter._native_status(self.spec)
        self.adapter._management_definition(self.spec, native)
        if not native.get("installed") or type(native.get("enabled")) is not bool:
            raise UpdateError("The existing task disappeared or returned incomplete state.")
        if native.get("state") == "Running":
            raise UpdateError("A scheduled run is still active. Wait for it to finish; no task or process was stopped.")
        return {"job_id": self.spec.job_id, "authority": self.adapter._user(),
                "definition_sha256": task_definition_identity(native["xml"]), "enabled": native["enabled"]}

    def set_enabled(self, saved: dict[str, Any] | None, enabled: bool) -> None:
        current = self.snapshot()
        if (current is None) != (saved is None) or (current is not None and saved is not None
                and {key: value for key, value in current.items() if key != "enabled"}
                != {key: value for key, value in saved.items() if key != "enabled"}):
            raise UpdateError("The original schedule identity changed; its definition was not replaced.")
        if saved is None:
            if self.spec is not None:
                raise UpdateError("The update has no saved schedule identity.")
            return
        if self.spec is None or saved["job_id"] != self.spec.job_id or saved["authority"] != self.adapter._user():
            raise UpdateError("The saved schedule owner changed; no task was changed.")
        native = self.adapter._native_status(self.spec)
        self.adapter._management_definition(self.spec, native)
        if (not native.get("installed") or task_definition_identity(native["xml"]) != saved["definition_sha256"]
                or native.get("state") == "Running"):
            raise UpdateError("The saved task changed or is running. Its definition was preserved; update recovery is required.")
        if native["enabled"] != enabled:
            # Compare the observed definition again inside the native operation.
            # Windows offers no task-definition compare-and-swap primitive.
            script = r"""
$ErrorActionPreference = 'Stop'
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -cne $p.authority) { throw 'Task owner changed.' }
$task = Get-ScheduledTask -TaskPath '\' -TaskName $p.name -ErrorAction Stop
$xml = Export-ScheduledTask -TaskPath '\' -TaskName $p.name -ErrorAction Stop
if ($task.TaskPath -cne '\' -or $xml -cne $p.expected_xml -or $task.State.ToString() -eq 'Running') { throw 'Task changed.' }
if ($p.enabled) { Enable-ScheduledTask -TaskPath '\' -TaskName $p.name -ErrorAction Stop | Out-Null }
else { Disable-ScheduledTask -TaskPath '\' -TaskName $p.name -ErrorAction Stop | Out-Null }
@{changed=$true} | ConvertTo-Json -Compress
""".strip()
            self.adapter._run_script(script, {"name": self.spec.job_id, "authority": saved["authority"],
                "expected_xml": native["xml"], "enabled": enabled})
        after = self.adapter._native_status(self.spec)
        self.adapter._management_definition(self.spec, after)
        if (not after.get("installed") or after.get("enabled") is not enabled
                or task_definition_identity(after["xml"]) != saved["definition_sha256"]):
            raise UpdateError("Schedule readback did not preserve the saved definition and enabled state.")


def _windows(script: str, payload: object, *, timeout: int = 40) -> Any:
    try:
        result = subprocess.run([str(trusted_windows_executable("WindowsPowerShell/v1.0/powershell.exe")),
            "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload),
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("Windows owner verification did not complete; no application process was killed. Check the saved update phase before retrying.") from exc
    if result.returncode != 0 or len(result.stdout.encode()) > 1_000_000:
        raise UpdateError("Windows could not verify or gracefully close the selected owner; no process was killed.")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise UpdateError("Windows returned an invalid owner receipt.") from exc


def matching_owners(config_path: Path, state: Path, installed_python: Path) -> list[dict[str, Any]]:
    """Match private profile, exact argv, executable and application hashes."""
    distribution, _user = _wsl_identity()
    directory = state / "native-review" / "profiles"
    profiles: dict[str, dict[str, Any]] = {}
    profile_root = ntpath.normcase(ntpath.normpath(_unc_path(directory, distribution))) + "\\"
    entries = list(directory.iterdir()) if directory.exists() else []
    if len(entries) > 1000:
        raise UpdateError("Native profile history exceeds the update inspection bound.")
    for profile in entries:
        if not profile.is_dir() or profile.is_symlink() or not (profile / "backend-launch.json").exists():
            continue
        launch = load_private_json(profile, Path("backend-launch.json"), maximum_bytes=65_536, read_only=True)
        arguments = launch.get("arguments")
        if (launch.get("schema_version") != 3 or launch.get("diagnostic_transport") is not False
                or not isinstance(arguments, list) or len(arguments) < 8
                or arguments[-4] != "--config" or arguments[-2] != "--state-dir"
                or not all(isinstance(arguments[index], str) and Path(arguments[index]).is_absolute() for index in (-3, -1))
                or Path(arguments[-3]).resolve() != config_path.resolve()
                or Path(arguments[-1]).resolve() != state.resolve()
                or arguments[-7:-4] != ["-I", "-m", "dependabot_watcher.review_bridge"]
                or arguments[-8] != str(installed_python)):
            continue
        identity = hashlib.sha256((json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        if identity != profile.name or not isinstance(launch.get("component"), dict):
            raise UpdateError("An applicable native profile changed; its owner was not closed.")
        profiles[_unc_path(profile / "backend-launch.json", distribution)] = {
            "component": launch["component"], "profile": str(profile), "profile_sha256": identity}
    script = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$items = @(Get-CimInstance Win32_Process -Filter "Name = 'WatcherNativeProof.exe'" | ForEach-Object {
    $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction Stop
    if ($owner.Sid -eq $sid) {
        $process = Get-Process -Id $_.ProcessId -ErrorAction Stop
        @{pid=[int]$_.ProcessId; path=$_.ExecutablePath; command_line=$_.CommandLine; started=$process.StartTime.ToUniversalTime().Ticks.ToString()}
    }
})
@{owners=$items} | ConvertTo-Json -Depth 4 -Compress
""".strip()
    response = _windows(script, {})
    if not isinstance(response, dict) or not isinstance(response.get("owners"), list):
        raise UpdateError("Native owner inventory is unavailable.")
    matches: list[dict[str, Any]] = []
    for owner in response["owners"]:
        if (not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 0
                or any(not isinstance(owner.get(key), str) for key in ("path", "command_line", "started"))):
            raise UpdateError("Native owner identity is incomplete.")
        argv = _split_windows_command_line(owner["command_line"])
        if len(argv) < 3 or argv[1] != "--launch-file":
            continue
        if argv[2] not in profiles:
            if ntpath.normcase(ntpath.normpath(argv[2])).startswith(profile_root):
                raise UpdateError("A running owner references an unavailable or unverified profile in the selected state. Preserve it and repair the profile before updating; no process was killed.")
            continue
        tail = argv[3:]
        scheduled = (len(tail) in {2, 4} and tail[0] == "--scheduled-run"
            and re.fullmatch(r"run_[a-f0-9]{20}", tail[1])
            and (len(tail) == 2 or tail[2:] in (["--notify-mode", "off"], ["--notify-mode", "desktop"])))
        notification = (len(tail) == 1 and tail[0] in {"--notification-test", "--notification-status", "--notification-remove"}
            or len(tail) == 2 and tail[0] == "--notification-run" and re.fullmatch(r"run_[a-f0-9]{20}", tail[1]))
        if tail and not scheduled and not notification:
            raise UpdateError("An applicable native owner has unsupported launch arguments; it was not closed or replaced.")
        selected = profiles[argv[2]]
        component = selected["component"]
        if owner["path"].casefold() != str(component.get("path", "")).casefold() or argv[0].casefold() != owner["path"].casefold():
            raise UpdateError("An applicable native owner differs from its registered executable.")
        # Hash through the verified Windows host, not an inferred drive mount.
        script = r"""
$ErrorActionPreference = 'Stop'
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
@{sha256=(Get-FileHash -LiteralPath $p.path -Algorithm SHA256).Hash.ToLowerInvariant(); assembly_sha256=(Get-FileHash -LiteralPath ([IO.Path]::ChangeExtension($p.path,'dll')) -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json -Compress
""".strip()
        hashes = _windows(script, owner)
        if hashes != {key: component.get(key) for key in ("sha256", "assembly_sha256")}:
            raise UpdateError("The selected owner's component files changed; it was not closed.")
        matches.append({**owner, **hashes, "profile": selected["profile"],
                        "profile_sha256": selected["profile_sha256"], "launch_file": argv[2]})
    return matches


def close_owner(owner: dict[str, Any]) -> None:
    """Invoke the app's own Exit action, including a hidden tray window."""
    payload = dict(owner)
    if "profile" in owner:
        profile = Path(owner["profile"])
        launch = load_private_json(profile, Path("backend-launch.json"), maximum_bytes=65_536, read_only=True)
        digest = hashlib.sha256((json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        distribution, _user = _wsl_identity()
        component = launch.get("component", {})
        if (digest != owner["profile_sha256"] or profile.name != digest
                or owner["launch_file"] != _unc_path(profile / "backend-launch.json", distribution)
                or any(component.get(key) != owner.get(key) for key in ("path", "sha256", "assembly_sha256"))):
            raise UpdateError("The owner's private activation profile changed; it was not reopened or closed.")
        payload["reopen_arguments"] = subprocess.list2cmdline(["--launch-file", owner["launch_file"]])
    script = r"""
$ErrorActionPreference = 'Stop'
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
$process = Get-Process -Id $p.pid -ErrorAction SilentlyContinue
if ($null -eq $process) { @{status='exited'} | ConvertTo-Json -Compress; exit 0 }
$cim = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$p.pid) -ErrorAction Stop
$owner = Invoke-CimMethod -InputObject $cim -MethodName GetOwnerSid -ErrorAction Stop
if ($owner.Sid -cne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -or $process.Path -ine $p.path -or $cim.CommandLine -cne $p.command_line -or $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $p.started) { throw 'Owner identity changed.' }
if ((Get-FileHash -LiteralPath $p.path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $p.sha256 -or (Get-FileHash -LiteralPath ([IO.Path]::ChangeExtension($p.path,'dll')) -Algorithm SHA256).Hash.ToLowerInvariant() -cne $p.assembly_sha256) { throw 'Owner component changed.' }
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class WatcherUpdateWindows {
    public delegate bool Visitor(IntPtr handle, IntPtr state);
    [DllImport("user32.dll")] private static extern bool EnumWindows(Visitor visit, IntPtr state);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr handle);
    public static IntPtr[] ForProcess(uint pid) {
        var windows = new List<IntPtr>();
        EnumWindows((handle, state) => { uint owner; GetWindowThreadProcessId(handle, out owner); if (owner == pid) windows.Add(handle); return true; }, IntPtr.Zero);
        return windows.ToArray();
    }
}
'@
function Find-ExitButtons {
    foreach ($handle in [WatcherUpdateWindows]::ForProcess([uint32]$p.pid)) {
        if (-not [WatcherUpdateWindows]::IsWindowVisible($handle)) { continue }
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
        if ($null -eq $root) { continue }
        $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Exit')
        $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        foreach ($button in $found) { if ($button.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) { $button } }
    }
}
$buttons = @(Find-ExitButtons)
if ($buttons.Count -eq 0 -and $p.reopen_arguments) {
    # WPF Hide removes automation children. Normal activation redirects to the
    # existing profile owner and shows its managed queue; Win32 ShowWindow does
    # not restore that WPF state. Never send scheduled/model/action arguments.
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $p.path
    $start.Arguments = $p.reopen_arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $activation = [Diagnostics.Process]::Start($start)
    $activation.Dispose()
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        if ($process.HasExited) { @{status='exited'} | ConvertTo-Json -Compress; exit 0 }
        Start-Sleep -Milliseconds 100
        $buttons = @(Find-ExitButtons)
    } while ($buttons.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)
}
if ($buttons.Count -ne 1) { throw 'One exact Watcher Exit control is required.' }
$invoke = $buttons[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
$invoke.Invoke()
if (-not $process.WaitForExit(30000)) { throw 'Watcher is still draining; no process was killed.' }
@{status='exited'} | ConvertTo-Json -Compress
""".strip()
    if _windows(script, payload) != {"status": "exited"}:
        raise UpdateError("The selected owner did not confirm graceful exit.")
