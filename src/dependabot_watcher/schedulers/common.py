from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping, Protocol, Sequence


SCHEDULE_SCHEMA_VERSION = 1
SCHEDULE_JOB_ID_PATTERN = re.compile(r"^dependabot-watcher-[0-9a-f]{12}$")


class ScheduleError(RuntimeError):
    """Raised when a native schedule cannot be represented or managed safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        ...


class SubprocessRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        try:
            result = subprocess.run(
                list(command),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScheduleError(f"Could not run scheduler command {command[0]!r}.") from exc
        return CommandResult(result.returncode, result.stdout, result.stderr)


@dataclass(frozen=True)
class ScheduleSpec:
    platform: str
    job_id: str
    executable: Path
    config_path: Path
    state_directory: Path
    interval_minutes: int
    notification_mode: str
    run_argv: tuple[str, ...]
    distro: str | None = None
    linux_user: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "job_id": self.job_id,
            "executable": str(self.executable),
            "config_path": str(self.config_path),
            "state_directory": str(self.state_directory),
            "interval_minutes": self.interval_minutes,
            "notification_mode": self.notification_mode,
            "run_argv": list(self.run_argv),
            "distro": self.distro,
            "linux_user": self.linux_user,
        }


class SchedulerAdapter(Protocol):
    def inventory(self) -> dict[str, Any]:
        ...

    def preview(self, spec: ScheduleSpec) -> dict[str, Any]:
        ...

    def install(self, spec: ScheduleSpec) -> dict[str, Any]:
        ...

    def status(self, spec: ScheduleSpec) -> dict[str, Any]:
        ...

    def disable(self, spec: ScheduleSpec) -> dict[str, Any]:
        ...

    def remove(self, spec: ScheduleSpec) -> dict[str, Any]:
        ...


def schedule_job_id(config_path: Path) -> str:
    canonical = os.path.normcase(str(config_path.expanduser().resolve(strict=False)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"dependabot-watcher-{digest}"


def validate_schedule_job_id(value: object) -> str:
    if not isinstance(value, str) or SCHEDULE_JOB_ID_PATTERN.fullmatch(value) is None:
        raise ScheduleError("Native schedule identity is not a valid Watcher job ID.")
    return value


def _executable_name(value: str) -> str:
    return PureWindowsPath(value).name if "\\" in value else Path(value).name


def _is_absolute_executable(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _watcher_run_command(
    arguments: Sequence[str],
) -> tuple[Path, Path, int, str]:
    if len(arguments) not in {11, 13}:
        raise ScheduleError("Watcher schedule command has an unexpected argument shape.")
    executable = arguments[0]
    if (
        not _is_absolute_executable(executable)
        or _executable_name(executable).casefold()
        not in {"dependabot-watcher", "dependabot-watcher.exe"}
    ):
        raise ScheduleError("Watcher schedule command has an invalid executable.")
    if (
        arguments[1] != "run"
        or arguments[2] != "--config"
        or arguments[4] != "--state-dir"
        or arguments[6] != "--expected-interval-minutes"
    ):
        raise ScheduleError("Watcher schedule command has an unexpected argument order.")
    if arguments[8:11] != ("--quiet", "--format", "json"):
        raise ScheduleError("Watcher schedule command has an unexpected output contract.")
    config_value = arguments[3]
    state_value = arguments[5]
    interval_value = arguments[7]
    if not config_value or not state_value:
        raise ScheduleError("Watcher schedule command has an empty path.")
    try:
        interval_minutes = int(interval_value)
    except ValueError as exc:
        raise ScheduleError("Watcher schedule command has an invalid interval.") from exc
    if interval_minutes < 15 or interval_minutes > 10_080:
        raise ScheduleError("Watcher schedule command interval is outside the supported range.")
    config_path = Path(config_value).expanduser().resolve(strict=False)
    state_directory = Path(state_value).expanduser().resolve(strict=False)
    if (
        config_value != str(config_path)
        or state_value != str(state_directory)
        or interval_value != str(interval_minutes)
    ):
        raise ScheduleError("Watcher schedule command is not in canonical generated form.")
    notification_mode = "off"
    if len(arguments) == 13:
        if arguments[11:] != ("--notify", "desktop"):
            raise ScheduleError("Watcher schedule command has an invalid notification mode.")
        notification_mode = "desktop"
    return (
        config_path,
        state_directory,
        interval_minutes,
        notification_mode,
    )


def inventory_registration(
    *,
    platform: str,
    job_id: str,
    run_argv: Sequence[str],
    native_identity: str,
    authority: str,
    native_interval_minutes: object,
    enabled: bool | None,
    active: bool | None,
    native_complete: bool = True,
    expected_wsl_executable: str | None = None,
    expected_wsl_user: str | None = None,
    expected_wsl_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate one embedded Watcher command and return recovery evidence."""

    validated_job_id = validate_schedule_job_id(job_id)
    arguments = tuple(run_argv)
    distro = None
    watcher_arguments = arguments
    if platform == "wsl":
        if len(arguments) < 9:
            raise ScheduleError("Watcher WSL schedule command has an incomplete wrapper.")
        wrapper = arguments[0]
        if (
            not _is_absolute_executable(wrapper)
            or _executable_name(wrapper).casefold() != "wsl.exe"
            or expected_wsl_executable is None
            or os.path.normcase(os.path.normpath(wrapper))
            != os.path.normcase(os.path.normpath(expected_wsl_executable))
        ):
            raise ScheduleError("Watcher WSL schedule command has an untrusted wrapper.")
        if (
            arguments[1] != "--distribution"
            or not arguments[2]
            or arguments[3] != "--user"
            or not arguments[4]
            or arguments[5:7] != ("--exec", "/usr/bin/env")
            or not arguments[7].startswith("PATH=")
            or not arguments[7].removeprefix("PATH=")
        ):
            raise ScheduleError("Watcher WSL schedule command has an invalid wrapper shape.")
        distro = arguments[2]
        watcher_arguments = arguments[8:]
    config_path, state_directory, interval_minutes, notification_mode = (
        _watcher_run_command(watcher_arguments)
    )
    if platform == "wsl":
        expected_path = (
            expected_wsl_paths.get(notification_mode)
            if expected_wsl_paths is not None
            else None
        )
        if (
            expected_wsl_user is None
            or arguments[4] != expected_wsl_user
            or expected_path is None
            or arguments[7] != f"PATH={expected_path}"
        ):
            raise ScheduleError("Watcher WSL schedule command has untrusted user or PATH metadata.")
    if schedule_job_id(config_path) != validated_job_id:
        raise ScheduleError("Watcher schedule identity does not match its embedded configuration path.")
    if (
        isinstance(native_interval_minutes, bool)
        or not isinstance(native_interval_minutes, int)
        or native_interval_minutes < 15
        or native_interval_minutes > 10_080
    ):
        raise ScheduleError("Native schedule interval is missing or outside the supported range.")
    drifted = not native_complete or native_interval_minutes != interval_minutes
    recovery_command = [
        "dependabot-watcher",
        "schedule",
        "remove",
        "--platform",
        platform,
        "--config",
        str(config_path),
        "--state-dir",
        str(state_directory),
        "--interval-minutes",
        str(interval_minutes),
    ]
    if platform == "wsl":
        assert distro is not None
        recovery_command.extend(["--distro", distro])
    if notification_mode != "off":
        recovery_command.extend(["--notify", notification_mode])
    return {
        "platform": platform,
        "job_id": validated_job_id,
        "native_identity": native_identity,
        "authority": authority,
        "config_path": str(config_path),
        "state_directory": str(state_directory),
        "interval_minutes": interval_minutes,
        "native_interval_minutes": native_interval_minutes,
        "native_complete": native_complete,
        "notification_mode": notification_mode,
        "distro": distro,
        "enabled": enabled,
        "active": active,
        "drifted": drifted,
        "command": list(arguments),
        "recovery_command": recovery_command,
    }


def inventory_report(
    *,
    platform: str,
    registrations: Sequence[Mapping[str, Any]],
    untrusted_candidates: Sequence[str] = (),
) -> dict[str, Any]:
    normalized_registrations = [dict(item) for item in registrations]
    needs_attention = bool(untrusted_candidates) or any(
        item.get("drifted") for item in normalized_registrations
    )
    return {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "mode": "schedule",
        "action": "inventory",
        "status": "attention" if needs_attention else "complete",
        "changed": False,
        "mutation_authorized": False,
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "platform": platform,
        "registration_count": len(normalized_registrations),
        "registrations": normalized_registrations,
        "untrusted_candidates": list(untrusted_candidates),
        "next": (
            "Use only a listed recovery_command after verifying the embedded paths; "
            "untrusted candidates are never removal authority."
        ),
    }


def resolve_executable(executable: str = "dependabot-watcher") -> Path:
    requested = Path(executable).expanduser()
    if requested.is_absolute():
        path = requested.resolve(strict=False)
    else:
        resolved = shutil.which(executable)
        path = Path(resolved).resolve(strict=False) if resolved is not None else requested
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ScheduleError(
            "The requested dependabot-watcher executable is unavailable or not executable; install the product before scheduling it."
        )
    return path


def schedule_spec(
    *,
    platform: str,
    config_path: Path,
    state_directory: Path,
    interval_minutes: int,
    notification_mode: str = "off",
    distro: str | None = None,
    linux_user: str | None = None,
    executable: str = "dependabot-watcher",
    require_executable: bool = True,
) -> ScheduleSpec:
    if interval_minutes < 15 or interval_minutes > 10_080:
        raise ScheduleError("Schedule interval must be between 15 minutes and 7 days.")
    if notification_mode not in {"off", "desktop"}:
        raise ScheduleError(f"Unsupported notification mode: {notification_mode}.")
    config = config_path.expanduser().resolve(strict=False)
    state = state_directory.expanduser().resolve(strict=False)
    if require_executable:
        binary = resolve_executable(executable)
    else:
        resolved = shutil.which(executable)
        binary = Path(resolved or executable).expanduser().resolve(strict=False)
    argv = (
        str(binary),
        "run",
        "--config",
        str(config),
        "--state-dir",
        str(state),
        "--expected-interval-minutes",
        str(interval_minutes),
        "--quiet",
        "--format",
        "json",
        *(('--notify', notification_mode) if notification_mode != "off" else ()),
    )
    return ScheduleSpec(
        platform=platform,
        job_id=schedule_job_id(config),
        executable=binary,
        config_path=config,
        state_directory=state,
        interval_minutes=interval_minutes,
        notification_mode=notification_mode,
        run_argv=argv,
        distro=distro,
        linux_user=linux_user,
    )


def definition_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def schedule_report(
    *,
    action: str,
    spec: ScheduleSpec,
    status: str,
    definition: Mapping[str, Any],
    native_status: Mapping[str, Any] | None = None,
    changed: bool = False,
    next_step: str | None = None,
) -> dict[str, Any]:
    missed_run_policy = "one current-state catch-up; missed intervals are not replayed"
    if spec.platform == "systemd":
        missed_run_policy = (
            "manager-start catch-up; suspend/resume catch-up remains a native-Linux validation gate"
        )
    elif spec.platform == "launchd":
        missed_run_policy = (
            "RunAtLoad catch-up; sleep/login catch-up remains a macOS validation gate"
        )
    return {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "mode": "schedule",
        "action": action,
        "status": status,
        "changed": changed,
        "mutation_authorized": False,
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": action in {"install", "disable", "remove"},
            "github_mutations": False,
            "notifications": False,
        },
        "schedule": spec.as_dict(),
        "definition": dict(definition),
        "definition_digest": definition_digest(definition),
        "native_status": dict(native_status or {}),
        "missed_run_policy": missed_run_policy,
        "overlap_policy": "do not start a second run while one reconciliation holds the runtime lock",
        "next": next_step,
    }


def require_success(result: CommandResult, operation: str) -> None:
    if result.returncode != 0:
        raise ScheduleError(
            f"Native scheduler {operation} failed with exit code {result.returncode}."
        )
