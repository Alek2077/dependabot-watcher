"""WSL-to-Windows owner submission; backend guards agent and action authority."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .runtime_state import RuntimeStateError, ensure_private_directory, load_private_json, load_run_state, save_private_json, run_lock
from .windows_helpers import WindowsHelperError, trusted_windows_executable, windows_host_path


class NativeReviewError(RuntimeError):
    """The optional native review component cannot safely be opened."""


def _wsl_identity() -> tuple[str, str]:
    distribution = os.getenv("WSL_DISTRO_NAME", "")
    if os.name != "posix" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}", distribution):
        raise NativeReviewError("The Windows review component currently requires a WSL installation of Watcher.")
    import pwd  # POSIX-only module; the portable CLI must still import on Windows.
    user = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise NativeReviewError("The current WSL user could not be identified safely.")
    return distribution, user


def _unc_path(path: Path, distribution: str) -> str:
    if not path.is_absolute() or "\x00" in str(path) or "\\" in str(path):
        raise NativeReviewError("The review profile requires an absolute WSL path.")
    return "\\\\wsl.localhost\\" + distribution + str(path).replace("/", "\\")


def _component(path: Path) -> tuple[Path, str, str]:
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.casefold() != ".exe":
        raise NativeReviewError("Select the installed optional Windows review executable, not a script or directory.")
    # Native component selection is deliberate local setup, not a field from a
    # PR, notification, model reply or arbitrary backend response.
    windows_host_path(candidate)
    with candidate.open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    assembly = candidate.with_suffix(".dll")
    if assembly.is_symlink() or not assembly.is_file():
        raise NativeReviewError("Select the complete Windows component directory, including its application DLL.")
    with assembly.open("rb") as stream:
        assembly_fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    return candidate, fingerprint, assembly_fingerprint


def verify_native_host() -> None:
    """Check the same host/user identity required by native launch, without writes."""
    _wsl_identity()


def verify_native_component(state_directory: Path) -> tuple[Path, str, str]:
    """Read and verify an existing selection without creating state or launching it."""
    state = state_directory.expanduser().absolute()
    private = state / "native-review"
    if not os.path.lexists(private / "component.json"):
        raise NativeReviewError("Select the Windows component required for WSL supervision with review --native-component PATH first.")
    registration = load_private_json(state, Path("native-review/component.json"), maximum_bytes=8192, read_only=True)
    if (not isinstance(registration, dict) or set(registration) != {"schema_version", "path", "sha256", "assembly_sha256"}
            or type(registration["schema_version"]) is not int or registration["schema_version"] != 2
            or not isinstance(registration["path"], str)
            or not isinstance(registration["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", registration["sha256"])
            or not isinstance(registration["assembly_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", registration["assembly_sha256"])):
        raise NativeReviewError("Reselect the complete Windows component with review --native-component PATH.")
    candidate, fingerprint, assembly_fingerprint = _component(Path(registration["path"]))
    if fingerprint != registration["sha256"] or assembly_fingerprint != registration["assembly_sha256"]:
        raise NativeReviewError("The registered Windows executable changed; deliberately select the updated component again.")
    return candidate, fingerprint, assembly_fingerprint


def _prepare_native_review(
    config_path: Path, state_directory: Path, *, component: Path | None = None,
    existing_only: bool = False, backend_python: Path | None = None, updating: bool = False,
) -> tuple[Path, Path, str]:
    """Bind the same immutable installed command for review and notification."""
    distribution, user = _wsl_identity()
    if not updating:
        from .updating import require_no_update
        require_no_update(state_directory.expanduser().absolute())
    interpreter = backend_python or Path(sys.executable).absolute()
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise NativeReviewError("The installed backend interpreter is unavailable.")
    config_path = Path(os.path.abspath(config_path.expanduser()))
    load_config(config_path)  # Fail before creating launch state for invalid policy.
    if existing_only and (component is not None
            or not (state_directory.expanduser() / "native-review/component.json").is_file()):
        raise NativeReviewError("Notification status/removal requires an existing registered component.")
    state = ensure_private_directory(Path(os.path.abspath(state_directory.expanduser())))
    private = ensure_private_directory(state / "native-review")
    if component is None:
        candidate, fingerprint, assembly_fingerprint = verify_native_component(state)
    else:
        if existing_only:
            raise NativeReviewError("Notification status/removal cannot select a new component.")
        candidate, fingerprint, assembly_fingerprint = _component(component)
    launch = {
        "schema_version": 3, "diagnostic_transport": False,
        "component": {"path": windows_host_path(candidate), "sha256": fingerprint, "assembly_sha256": assembly_fingerprint},
        "arguments": ["--distribution", distribution, "--user", user, "--exec", "/usr/bin/env",
            "-u", "PYTHONPATH",
            "PATH=" + ":".join((str(Path.home() / ".local/bin"), str(Path.home() / ".npm-global/bin"),
                                  "/usr/local/bin", "/usr/bin", "/bin")),
            str(interpreter), "-I", "-m", "dependabot_watcher.review_bridge",
            "--config", str(config_path), "--state-dir", str(state)],
    }
    # Never overwrite a launch profile under an existing window. Distinct
    # config/interpreter/WSL/component bindings get distinct native instances.
    # Match save_private_json's exact representation so the native consumer can
    # bind the one bounded byte snapshot without cross-language JSON rewriting.
    profile_id = hashlib.sha256(
        (json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    if existing_only and not (private / "profiles" / profile_id / "backend-launch.json").is_file():
        raise NativeReviewError("Notification status/removal requires the existing installed profile.")
    profiles = ensure_private_directory(private / "profiles")
    profile = ensure_private_directory(profiles / profile_id)
    if os.path.lexists(profile / "backend-launch.json"):
        if load_private_json(profile, Path("backend-launch.json"), maximum_bytes=65536) != launch:
            raise NativeReviewError("The saved native launch profile changed; do not reuse it.")
    else:
        if existing_only:
            raise NativeReviewError("Notification status/removal requires the existing installed profile.")
        save_private_json(profile, Path("backend-launch.json"), launch)
    # This records deliberate component selection, not successful activation.
    # Finish all required local writes before Windows can start a child; a
    # disk failure must not disguise an already submitted process as no launch.
    if not existing_only:
        save_private_json(private, Path("component.json"),
                          {"schema_version": 2, "path": str(candidate), "sha256": fingerprint,
                           "assembly_sha256": assembly_fingerprint})
    return candidate, profile, distribution


def open_native_review(
    config_path: Path, state_directory: Path, *, component: Path | None = None,
) -> dict[str, Any]:
    """Open an aggregate queue; process submission does not authorize work."""
    return _submit_native_owner(config_path, state_directory, component=component)


def submit_scheduled_supervisor(
    config_path: Path, state_directory: Path, run_id: str, *, notification_mode: str = "off",
) -> dict[str, Any]:
    """Submit the existing native owner, not proof that an agent completed."""
    if (not isinstance(run_id, str) or re.fullmatch(r"run_[a-f0-9]{20}", run_id) is None
            or not isinstance(notification_mode, str) or notification_mode not in {"off", "desktop"}):
        raise NativeReviewError("Scheduled supervision requires an exact run and supported notification mode.")
    config = load_config(config_path)
    if config.supervisor.mode != "approval_first":
        raise NativeReviewError("Scheduled supervision must be explicitly enabled in the saved configuration.")
    directory = state_directory.expanduser().absolute()
    with run_lock(directory, wait_seconds=2):
        attempt = load_run_state(directory).get("last_attempt")
        if load_config(config_path) != config or not isinstance(attempt, dict) or attempt.get("run_id") != run_id:
            raise NativeReviewError("Scheduled supervision no longer matches the latest saved run/configuration.")
    return _submit_native_owner(config_path, state_directory, scheduled_run_id=run_id, notification_mode=notification_mode)


def _submit_native_owner(
    config_path: Path, state_directory: Path, *, component: Path | None = None,
    scheduled_run_id: str | None = None, notification_mode: str = "off",
) -> dict[str, Any]:
    # The same lock covers the final update fence, profile selection and actual
    # process submission. A prechecked old profile cannot launch after an
    # updater's owner scan and then outlive fence removal.
    _wsl_identity()  # Unsupported hosts must refuse before creating lock/state directories.
    with run_lock(state_directory.expanduser().absolute(), wait_seconds=2):
        return _submit_native_owner_under_lock(config_path, state_directory, component=component,
            scheduled_run_id=scheduled_run_id, notification_mode=notification_mode)


def _submit_native_owner_under_lock(
    config_path: Path, state_directory: Path, *, component: Path | None = None,
    scheduled_run_id: str | None = None, notification_mode: str = "off",
) -> dict[str, Any]:
    candidate, profile, distribution = _prepare_native_review(config_path, state_directory, component=component)
    arguments = ["--launch-file", _unc_path(profile / "backend-launch.json", distribution)]
    submission_path: Path | None = None
    if scheduled_run_id is not None:
        submission_path = Path("scheduled-submissions") / f"{scheduled_run_id}.json"
        arguments.extend(["--scheduled-run", scheduled_run_id, "--notify-mode", notification_mode])
        # Reserve submission before process creation. Pending is deliberately
        # uncertain after a crash/write failure; never overwrite it and launch
        # another child merely because its terminal receipt was lost.
        with run_lock(profile / "scheduled-submissions", wait_seconds=2):
            if os.path.lexists(profile / submission_path):
                raise NativeReviewError("This run already has a submission record. Inspect saved agent state; no automatic retry was made.")
            save_private_json(profile, submission_path, {"schema_version": 1, "run_id": scheduled_run_id,
                "status": "submission_pending", "notification_mode": notification_mode})

    def unconfirmed() -> None:
        if submission_path is None:
            return
        try:
            save_private_json(profile, submission_path, {"schema_version": 1, "run_id": scheduled_run_id,
                "status": "submission_unconfirmed", "notification_mode": notification_mode})
        except (OSError, RuntimeStateError):
            # The durable pre-launch pending receipt still blocks resubmission.
            # Disk failure cannot prove that Windows did not start the child.
            pass
    # No shell interpolation of paths or user content. PowerShell reads typed
    # JSON and passes a Windows-quoted argument string to the selected exe.
    payload = {"executable": windows_host_path(candidate), "arguments": subprocess.list2cmdline(arguments)}
    script = r"""
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$process = Start-Process -FilePath ([string]$payload.executable) -ArgumentList ([string]$payload.arguments) -PassThru
@{pid=$process.Id; status='submitted'} | ConvertTo-Json -Compress
""".strip()
    try:
        process_result = subprocess.run([str(trusted_windows_executable("WindowsPowerShell/v1.0/powershell.exe")),
            "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload),
            capture_output=True, text=True, timeout=30, check=False)
        receipt = json.loads(process_result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        if submission_path is not None:
            unconfirmed()
            raise NativeReviewError("Windows did not confirm background submission. Inspect saved state; no automatic retry was made.") from exc
        raise NativeReviewError("Windows did not confirm the review launch. No agent or merge was requested.") from exc
    if (process_result.returncode != 0 or not isinstance(receipt, dict) or set(receipt) != {"pid", "status"}
            or type(receipt["pid"]) is not int or receipt["pid"] <= 0 or receipt["status"] != "submitted"):
        if submission_path is not None:
            unconfirmed()
            raise NativeReviewError("Windows did not confirm background submission. Inspect saved state; no automatic retry was made.")
        raise NativeReviewError("Windows did not confirm the review launch. No agent or merge was requested.")
    result: dict[str, Any] = {"schema_version": 1, "status": "submitted", "pid": receipt["pid"],
            "configuration_path": str(config_path.expanduser().absolute()),
            "state_directory": str(state_directory.expanduser().absolute()),
            "assurance": "process_submission_only", "model_calls": False, "github_mutations": False}
    if submission_path is not None:
        result.update({"run_id": scheduled_run_id, "notification_mode": notification_mode,
                       "model_calls": "not_observed", "model_launch_requested": True})
        try:
            save_private_json(profile, submission_path, result)
        except (OSError, RuntimeStateError) as exc:
            unconfirmed()
            raise NativeReviewError("Windows accepted background submission, but its receipt could not be saved. Inspect saved agent state; no automatic retry was made.") from exc
    return result


def native_notification(
    config_path: Path, state_directory: Path, *, operation: str,
    run_id: str | None = None, component: Path | None = None,
) -> dict[str, Any]:
    """Serialize profile selection and sender admission with replacement."""
    _wsl_identity()
    if operation in {"status", "remove"} and (component is not None
            or not (state_directory.expanduser() / "native-review/component.json").is_file()):
        raise NativeReviewError("Notification status/removal requires an existing registered component.")
    # Hold admission through the sender, but not the root journal lock: the
    # notification child asks that backend for its current attention summary.
    with run_lock(state_directory.expanduser().absolute() / "native-notifications", wait_seconds=2):
        return _native_notification_under_lock(config_path, state_directory,
            operation=operation, run_id=run_id, component=component)


def _native_notification_under_lock(
    config_path: Path, state_directory: Path, *, operation: str,
    run_id: str | None = None, component: Path | None = None,
) -> dict[str, Any]:
    """Use the selected component without opening a window or granting authority.

    Test deliberately bypasses reminder deduplication but changes no run/queue
    delivery state. Production publication independently reads the bound run.
    """
    flags = {"test": "--notification-test", "status": "--notification-status",
             "remove": "--notification-remove", "run": "--notification-run"}
    if operation not in flags or (operation == "run"
            and (not isinstance(run_id, str) or not re.fullmatch(r"run_[a-f0-9]{20}", run_id))):
        raise NativeReviewError("Select a supported notification operation and exact saved run.")
    if operation != "run" and run_id is not None:
        raise NativeReviewError("Notification diagnostics cannot select a run.")
    try:
        with run_lock(state_directory.expanduser().absolute(), wait_seconds=2):
            candidate, profile, distribution = _prepare_native_review(
                config_path, state_directory, component=component, existing_only=operation in {"status", "remove"},
            )
            executable = windows_host_path(candidate)
    except WindowsHelperError as exc:
        raise NativeReviewError("Windows path helpers are unavailable; notification was not submitted.") from exc
    arguments = ["--launch-file", _unc_path(profile / "backend-launch.json", distribution), flags[operation]]
    if run_id is not None:
        arguments.append(run_id)
    payload = {"executable": executable, "arguments": subprocess.list2cmdline(arguments)}
    script = r"""
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = [string]$payload.executable
$start.Arguments = [string]$payload.arguments
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($start)
try {
    $output = $process.StandardOutput.ReadToEndAsync()
    $errorOutput = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(60000)) { throw 'Notification component did not finish.' }
    $text = $output.GetAwaiter().GetResult()
    $null = $errorOutput.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) { throw 'Notification component refused the request.' }
    [Console]::Out.Write($text)
} finally {
    if (-not $process.HasExited) {
        # Only the owned sender: a click may have opened a separate review queue.
        $process.Kill()
        $null = $process.WaitForExit(10000)
    }
    $process.Dispose()
}
""".strip()
    try:
        result = subprocess.run([str(trusted_windows_executable("WindowsPowerShell/v1.0/powershell.exe")),
            "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload),
            capture_output=True, text=True, timeout=90, check=False)
        receipt = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, WindowsHelperError) as exc:
        raise NativeReviewError("Windows did not confirm the notification operation; saved work is unchanged.") from exc
    expected = {"test": {"accepted", "unavailable"}, "run": {"accepted", "unavailable", "not_needed"},
                "status": {"registered"}, "remove": {"removed"}}[operation]
    common = {"schema_version", "status", "profile_id"}
    fields = common | ({"setting", "review_count", "test_count"} if operation == "status"
                       else {"notification_id", "assurance"})
    if (result.returncode != 0 or not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("profile_id") != profile.name or receipt.get("status") not in expected):
        raise NativeReviewError("Windows notification receipt does not match this profile and operation.")
    if operation == "status":
        if (not isinstance(receipt["setting"], str) or len(receipt["setting"]) > 64
                or any(type(receipt[field]) is not int or not 0 <= receipt[field] <= 10000
                       for field in ("review_count", "test_count"))):
            raise NativeReviewError("Windows notification status is malformed.")
    elif (type(receipt["notification_id"]) is not int or not 0 <= receipt["notification_id"] < 2**32
            or receipt["assurance"] != "adapter_accepted_only"
            or (receipt["status"] == "accepted" and receipt["notification_id"] == 0)):
        raise NativeReviewError("Windows notification acceptance is malformed.")
    return receipt
