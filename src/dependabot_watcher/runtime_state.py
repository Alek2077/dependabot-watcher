from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
import errno
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .windows_helpers import WindowsHelperError, trusted_windows_executable


RUN_STATE_SCHEMA_VERSION = 1
_RUN_STATE_FILE = "run-state.json"
_RUN_LOCK_FILE = "run.lock"
_MAX_STATE_BYTES = 5_000_000
_MAX_ARTIFACT_BYTES = 50_000_000
_MAX_HISTORY_RECORDS = 100
_MAX_PLAN_ARTIFACTS = 20
_PLAN_ARTIFACT = re.compile(r"^plan_[a-f0-9]+\.json$")


class RuntimeStateError(RuntimeError):
    """Raised when private reconciler state cannot be trusted or updated safely."""


class RuntimeLockBusyError(RuntimeStateError):
    """An actual exclusive-lock holder, not a filesystem/validation error."""


def _windows_platform() -> bool:
    return os.name == "nt"


def _trusted_windows_powershell() -> str:
    try:
        return str(
            trusted_windows_executable("WindowsPowerShell/v1.0/powershell.exe")
        )
    except WindowsHelperError as exc:
        raise RuntimeStateError(str(exc)) from exc


def _windows_acl(path: Path, *, directory: bool, apply: bool) -> None:
    script = r"""
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$path = [string]$payload.path
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = Get-Acl -LiteralPath $path -ErrorAction Stop
if ($payload.apply -eq $true) {
  $acl.SetAccessRuleProtection($true, $false)
  $identities = @($acl.Access | ForEach-Object { $_.IdentityReference } | Select-Object -Unique)
  foreach ($identity in $identities) { $acl.PurgeAccessRules($identity) }
  $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
  if ($payload.directory -eq $true) {
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  }
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $current,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
  )
  $acl.SetOwner($current)
  $acl.SetAccessRule($rule)
  Set-Acl -LiteralPath $path -AclObject $acl -ErrorAction Stop
  $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
}
if ($acl.AreAccessRulesProtected -ne $true) { exit 11 }
$ownerAccount = [System.Security.Principal.NTAccount]::new([string]$acl.Owner)
$ownerSid = $ownerAccount.Translate([System.Security.Principal.SecurityIdentifier])
if ($ownerSid.Value -ne $current.Value) { exit 16 }
$rules = @($acl.Access)
if ($rules.Count -ne 1) { exit 12 }
$ruleSid = $rules[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier])
if ($ruleSid.Value -ne $current.Value) { exit 13 }
if ($rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { exit 14 }
$required = [System.Security.AccessControl.FileSystemRights]::FullControl
if (($rules[0].FileSystemRights -band $required) -ne $required) { exit 15 }
""".strip()
    try:
        result = subprocess.run(
            [_trusted_windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
            input=json.dumps(
                {"path": str(path), "directory": directory, "apply": apply}
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeStateError(
            f"Windows privacy controls could not be checked for {path}."
        ) from exc
    if result.returncode != 0:
        raise RuntimeStateError(
            f"Windows privacy controls are not restricted to the current user: {path}."
        )


def _is_reparse_point(path: Path) -> bool:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(state, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def default_state_directory() -> Path:
    override = os.getenv("DEPENDABOT_WATCHER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "dependabot-watcher"
    state_home = Path(os.getenv("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return state_home / "dependabot-watcher"


def _ensure_private_directory(path: Path) -> Path:
    expanded = path.expanduser()
    existed = _path_exists(expanded)
    if existed and (expanded.is_symlink() or _is_reparse_point(expanded)):
        raise RuntimeStateError(f"Runtime state directory cannot be a symbolic link: {expanded}.")
    try:
        expanded.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = expanded.stat()
    except OSError as exc:
        raise RuntimeStateError(f"Runtime state directory cannot be created: {expanded}.") from exc
    if not stat.S_ISDIR(state.st_mode):
        raise RuntimeStateError(f"Runtime state path is not a directory: {expanded}.")
    if _windows_platform():
        _windows_acl(expanded, directory=True, apply=not existed)
    else:
        try:
            os.chmod(expanded, 0o700)
            state = expanded.stat()
        except OSError as exc:
            raise RuntimeStateError(f"Runtime state directory cannot be made private: {expanded}.") from exc
        if hasattr(os, "getuid") and state.st_uid != os.getuid():
            raise RuntimeStateError(
                f"Runtime state directory is not owned by the current user: {expanded}."
            )
        if stat.S_IMODE(state.st_mode) & 0o077:
            raise RuntimeStateError(f"Runtime state directory is not private: {expanded}.")
    return expanded


def ensure_private_directory(path: Path) -> Path:
    """Create or verify a private state directory for the current platform."""

    return _ensure_private_directory(path)


def validate_user_owned_directory(path: Path) -> Path:
    """Verify an existing directory cannot be replaced by another local principal."""

    expanded = path.expanduser()
    try:
        state = expanded.lstat()
    except OSError as exc:
        raise RuntimeStateError(f"Owned directory cannot be inspected: {expanded}.") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode) or _is_reparse_point(expanded):
        raise RuntimeStateError(f"Owned path is not a trusted directory: {expanded}.")
    if _windows_platform():
        _windows_acl(expanded, directory=True, apply=False)
    else:
        if hasattr(os, "getuid") and state.st_uid != os.getuid():
            raise RuntimeStateError(f"Owned directory has a different owner: {expanded}.")
        if stat.S_IMODE(state.st_mode) & 0o022:
            raise RuntimeStateError(
                f"Owned directory permissions allow another principal to replace files: {expanded}."
            )
    return expanded


def ensure_user_owned_directory(path: Path) -> Path:
    """Create or verify a current-user directory without requiring read privacy."""

    expanded = path.expanduser()
    existed = _path_exists(expanded)
    try:
        expanded.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeStateError(f"Owned directory cannot be created: {expanded}.") from exc
    if _windows_platform() and not existed:
        _windows_acl(expanded, directory=True, apply=True)
    return validate_user_owned_directory(expanded)


def _validate_regular_private_file(
    path: Path,
    *,
    maximum_bytes: int = _MAX_STATE_BYTES,
) -> os.stat_result:
    try:
        state = path.lstat()
    except OSError as exc:
        raise RuntimeStateError(f"Runtime state file cannot be inspected: {path}.") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise RuntimeStateError(f"Runtime state file is not a regular file: {path}.")
    if _windows_platform():
        if _is_reparse_point(path):
            raise RuntimeStateError(f"Runtime state file is not a regular file: {path}.")
        _windows_acl(path, directory=False, apply=False)
    else:
        if hasattr(os, "getuid") and state.st_uid != os.getuid():
            raise RuntimeStateError(f"Runtime state file is not owned by the current user: {path}.")
        if stat.S_IMODE(state.st_mode) & 0o077:
            raise RuntimeStateError(f"Runtime state file is not private: {path}.")
    if state.st_size > maximum_bytes:
        raise RuntimeStateError("Runtime file reached its configured safety ceiling.")
    return state


def validate_private_file(
    path: Path,
    *,
    maximum_bytes: int = _MAX_ARTIFACT_BYTES,
) -> os.stat_result:
    """Verify one existing private regular file without repairing permission drift."""

    return _validate_regular_private_file(path, maximum_bytes=maximum_bytes)


def secure_private_file(
    path: Path,
    *,
    maximum_bytes: int = _MAX_ARTIFACT_BYTES,
) -> os.stat_result:
    """Apply the platform privacy boundary to a newly created owned file."""

    try:
        if _windows_platform():
            _windows_acl(path, directory=False, apply=True)
        else:
            os.chmod(path, 0o600)
    except OSError as exc:
        raise RuntimeStateError(f"Runtime state file cannot be made private: {path}.") from exc
    return _validate_regular_private_file(path, maximum_bytes=maximum_bytes)


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "last_attempt": None,
        "last_success": None,
        "latest_plan": None,
        "history": [],
    }


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous keys at every nesting level without exposing values."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeStateError("Runtime artifact contains ambiguous duplicate keys.")
        result[key] = value
    return result


def _read_private_json_object(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    inspected = _validate_regular_private_file(path, maximum_bytes=maximum_bytes)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        state = os.fstat(descriptor)
        if not stat.S_ISREG(state.st_mode) or state.st_size > maximum_bytes:
            raise RuntimeStateError("Runtime artifact is not a trusted bounded file.")
        if _windows_platform():
            if _is_reparse_point(path):
                raise RuntimeStateError("Runtime artifact is not a trusted regular file.")
            _windows_acl(path, directory=False, apply=False)
            if (inspected.st_dev, inspected.st_ino) != (state.st_dev, state.st_ino):
                raise RuntimeStateError("Runtime artifact changed during safe open.")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise RuntimeStateError("Runtime artifact reached its configured safety ceiling.")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except RuntimeStateError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise RuntimeStateError("Runtime artifact is malformed or unreadable.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise RuntimeStateError("Runtime artifact must contain a JSON object.")
    return value


def load_run_state(state_directory: Path) -> dict[str, Any]:
    directory = _ensure_private_directory(state_directory)
    path = directory / _RUN_STATE_FILE
    if not _path_exists(path):
        return _empty_state()
    try:
        value = _read_private_json_object(path, maximum_bytes=_MAX_STATE_BYTES)
    except RuntimeStateError as exc:
        raise RuntimeStateError("Runtime state is malformed or unreadable.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
        raise RuntimeStateError("Runtime state has an unsupported schema.")
    if set(value) != {
        "schema_version",
        "last_attempt",
        "last_success",
        "latest_plan",
        "history",
    }:
        raise RuntimeStateError("Runtime state has an invalid field set.")
    history = value.get("history")
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise RuntimeStateError("Runtime state history is malformed.")
    if len(history) > _MAX_HISTORY_RECORDS:
        raise RuntimeStateError("Runtime state history exceeds its retention boundary.")
    for field in ("last_attempt", "last_success", "latest_plan"):
        if value.get(field) is not None and not isinstance(value[field], dict):
            raise RuntimeStateError(f"Runtime state {field} record is malformed.")
    return value


def load_private_json(
    state_directory: Path,
    relative_path: Path,
    *,
    maximum_bytes: int = _MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    if relative_path.is_absolute() or not relative_path.parts or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        raise RuntimeStateError("Runtime artifact path must remain inside the state directory.")
    root = _ensure_private_directory(state_directory)
    directory = root
    for part in relative_path.parts[:-1]:
        directory = validate_user_owned_directory(directory / part)
    path = directory / relative_path.name
    return _read_private_json_object(path, maximum_bytes=maximum_bytes)


def delete_private_artifact(
    state_directory: Path,
    relative_path: Path,
    *,
    maximum_bytes: int = _MAX_ARTIFACT_BYTES,
) -> bool:
    """Delete one verified private artifact contained by the runtime-state root."""

    if relative_path.is_absolute() or not relative_path.parts or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        raise RuntimeStateError("Runtime artifact path must remain inside the state directory.")
    root = _ensure_private_directory(state_directory)
    directory = root
    for part in relative_path.parts[:-1]:
        directory = validate_user_owned_directory(directory / part)
    path = directory / relative_path.name
    if not _path_exists(path):
        return False
    validate_user_owned_directory(directory)
    _validate_regular_private_file(path, maximum_bytes=maximum_bytes)
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeStateError("Runtime artifact could not be removed safely.") from exc
    return True


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise RuntimeStateError("Runtime state could not be written completely.")
        offset += written


def save_private_json(
    state_directory: Path,
    relative_path: Path,
    value: Mapping[str, Any],
    *,
    maximum_bytes: int = _MAX_ARTIFACT_BYTES,
) -> Path:
    if relative_path.is_absolute() or not relative_path.parts or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        raise RuntimeStateError("Runtime artifact path must remain inside the state directory.")
    root = _ensure_private_directory(state_directory)
    directory = root
    for part in relative_path.parts[:-1]:
        directory = _ensure_private_directory(directory / part)
    path = directory / relative_path.name
    payload = (
        json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) > maximum_bytes:
        raise RuntimeStateError("Runtime artifact reached its configured safety ceiling.")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".run-state-",
        suffix=".tmp",
        dir=directory,
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_path, path)
        if _windows_platform():
            _windows_acl(path, directory=False, apply=True)
        else:
            os.chmod(path, 0o600)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        _validate_regular_private_file(path, maximum_bytes=maximum_bytes)
    except (OSError, RuntimeStateError) as exc:
        if isinstance(exc, RuntimeStateError):
            raise
        raise RuntimeStateError("Runtime state could not be replaced atomically.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def save_run_state(state_directory: Path, state_value: Mapping[str, Any]) -> Path:
    return save_private_json(
        state_directory,
        Path(_RUN_STATE_FILE),
        state_value,
        maximum_bytes=_MAX_STATE_BYTES,
    )


def prune_plan_artifacts(state_directory: Path, retained_plan_ids: list[str]) -> None:
    directory = _ensure_private_directory(state_directory) / "plans"
    if not _path_exists(directory):
        return
    try:
        directory = _ensure_private_directory(directory)
    except RuntimeStateError as exc:
        raise RuntimeStateError("Runtime plans path is not a trusted directory.") from exc
    ordered = list(dict.fromkeys(reversed(retained_plan_ids)))[:_MAX_PLAN_ARTIFACTS]
    keep = {f"{plan_id}.json" for plan_id in ordered if plan_id.startswith("plan_")}
    for path in directory.iterdir():
        if _PLAN_ARTIFACT.fullmatch(path.name) and path.name not in keep:
            if path.is_symlink() or not path.is_file():
                raise RuntimeStateError("Runtime plan artifact is not a trusted regular file.")
            path.unlink()


def append_run_record(
    state_value: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    successful: bool,
    latest_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    history = [
        *list(state_value.get("history") or []),
        dict(record),
    ][-_MAX_HISTORY_RECORDS:]
    return {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "last_attempt": dict(record),
        "last_success": dict(record) if successful else state_value.get("last_success"),
        "latest_plan": (
            dict(latest_plan) if latest_plan is not None else state_value.get("latest_plan")
        ),
        "history": history,
    }


@contextmanager
def run_lock(state_directory: Path, *, wait_seconds: float = 0) -> Iterator[Path]:
    """Exclusive state lock; scheduled/action callers remain fail-fast by default.

    Small interactive journal exchanges may explicitly wait a bounded interval
    for another short read/write transaction, never for external model work.
    """
    if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 5:
        raise RuntimeStateError("Runtime lock wait must be bounded between zero and five seconds.")
    deadline = time.monotonic() + wait_seconds
    directory = _ensure_private_directory(state_directory)
    path = directory / _RUN_LOCK_FILE
    existed = _path_exists(path)
    if existed and (path.is_symlink() or _is_reparse_point(path)):
        raise RuntimeStateError(f"Runtime lock is not a trusted regular file: {path}.")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        raise RuntimeStateError(f"Runtime lock cannot be opened: {path}.") from exc
    try:
        if _windows_platform():
            _windows_acl(path, directory=False, apply=not existed)
            validated = _validate_regular_private_file(path)
        else:
            os.fchmod(descriptor, 0o600)
            validated = None
        state = os.fstat(descriptor)
        if not stat.S_ISREG(state.st_mode):
            raise RuntimeStateError(f"Runtime lock is not a regular file: {path}.")
        if _windows_platform() and _is_reparse_point(path):
            raise RuntimeStateError(f"Runtime lock is not a trusted regular file: {path}.")
        if validated is not None and (validated.st_dev, validated.st_ino) != (
            state.st_dev,
            state.st_ino,
        ):
            raise RuntimeStateError("Runtime lock changed during safe open.")
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise RuntimeStateError("Runtime lock could not be acquired.") from exc
                    if time.monotonic() >= deadline:
                        raise RuntimeLockBusyError("Saved state is busy; another operation holds its lock.") from exc
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise RuntimeStateError("Runtime lock could not be acquired.") from exc
                    if time.monotonic() >= deadline:
                        raise RuntimeLockBusyError("Saved state is busy; another operation holds its lock.") from exc
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
        metadata = json.dumps(
            {"pid": os.getpid(), "host": socket.gethostname()},
            sort_keys=True,
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, metadata)
        os.fsync(descriptor)
        yield path
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)
