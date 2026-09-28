from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


SCHEDULE_SCHEMA_VERSION = 1


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
