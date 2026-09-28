from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from ..runtime_state import (
    RuntimeStateError,
    ensure_user_owned_directory,
    validate_user_owned_directory,
)
from .common import (
    CommandRunner,
    ScheduleError,
    ScheduleSpec,
    SubprocessRunner,
    inventory_registration,
    inventory_report,
    require_success,
    schedule_report,
    validate_schedule_job_id,
)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _atomic_text(path: Path, value: str) -> None:
    try:
        ensure_user_owned_directory(path.parent)
    except RuntimeStateError as exc:
        raise ScheduleError(str(exc)) from exc
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_definition(path: Path) -> str:
    try:
        validate_user_owned_directory(path.parent)
        state = path.lstat()
    except (OSError, RuntimeStateError) as exc:
        raise ScheduleError("Systemd schedule definition cannot be inspected safely.") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ScheduleError("Systemd schedule definition is not a trusted regular file.")
    if hasattr(os, "getuid") and state.st_uid != os.getuid():
        raise ScheduleError("Systemd schedule definition is not owned by the current user.")
    if stat.S_IMODE(state.st_mode) & 0o077:
        raise ScheduleError("Systemd schedule definition is not private.")
    try:
        if state.st_size > 1_000_000:
            raise ScheduleError("Systemd schedule definition exceeds its safety ceiling.")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ScheduleError("Systemd schedule definition cannot be read safely.") from exc


def _ownership_marker(spec: ScheduleSpec) -> str:
    return f"X-DependabotWatcherJobId={spec.job_id}"


def _read_owned_definition(path: Path, spec: ScheduleSpec) -> str | None:
    if not os.path.lexists(path):
        return None
    value = _read_definition(path)
    if _ownership_marker(spec) not in value.splitlines():
        raise ScheduleError(
            f"Systemd path is occupied by a definition not owned by Dependabot Watcher: {path}."
        )
    return value


class SystemdUserAdapter:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        unit_directory: Path | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._unit_directory = unit_directory or Path.home() / ".config/systemd/user"

    def _paths(self, spec: ScheduleSpec) -> tuple[Path, Path]:
        return (
            self._unit_directory / f"{spec.job_id}.service",
            self._unit_directory / f"{spec.job_id}.timer",
        )

    def _manager_candidate_names(self) -> set[str]:
        names: set[str] = set()
        systemctl = self._systemctl()
        for operation in ("list-units", "list-unit-files"):
            result = self._runner.run(
                [
                    systemctl,
                    "--user",
                    operation,
                    "--all",
                    "--plain",
                    "--no-legend",
                    "--full",
                    "dependabot-watcher-*.service",
                    "dependabot-watcher-*.timer",
                ]
            )
            require_success(result, f"systemd {operation} inventory")
            for line in result.stdout.splitlines():
                fields = line.split()
                if not fields:
                    continue
                unit = fields[1] if fields[0] == "●" and len(fields) > 1 else fields[0]
                match = re.fullmatch(
                    r"(dependabot-watcher-[^.]+)\.(?:service|timer)", unit
                )
                if match is not None:
                    names.add(match.group(1))
        return names

    def inventory(self) -> dict[str, Any]:
        names = self._manager_candidate_names()
        if os.path.lexists(self._unit_directory):
            try:
                validate_user_owned_directory(self._unit_directory)
            except RuntimeStateError as exc:
                raise ScheduleError("Systemd user unit directory cannot be trusted.") from exc
            names.update(
                path.name.rsplit(".", 1)[0]
                for pattern in ("dependabot-watcher-*.service", "dependabot-watcher-*.timer")
                for path in self._unit_directory.glob(pattern)
            )
        registrations: list[dict[str, Any]] = []
        untrusted: list[str] = []
        for name in sorted(names):
            try:
                job_id = validate_schedule_job_id(name)
            except ScheduleError:
                untrusted.append(name)
                continue
            try:
                service_path = self._unit_directory / f"{job_id}.service"
                timer_path = self._unit_directory / f"{job_id}.timer"
                service = (
                    _read_definition(service_path)
                    if os.path.lexists(service_path)
                    else None
                )
                timer = (
                    _read_definition(timer_path)
                    if os.path.lexists(timer_path)
                    else None
                )
                marker = f"X-DependabotWatcherJobId={job_id}"
                if service is None or marker not in service.splitlines():
                    raise ScheduleError("missing or unowned service definition")
                if timer is not None and marker not in timer.splitlines():
                    raise ScheduleError("unowned timer definition")
                exec_line = next(
                    (
                        line.removeprefix("ExecStart=")
                        for line in service.splitlines()
                        if line.startswith("ExecStart=")
                    ),
                    None,
                )
                if exec_line is None:
                    raise ScheduleError("owned service has no Watcher command")
                run_argv = tuple(value.replace("%%", "%") for value in shlex.split(exec_line))
                native_interval: int | None = None
                if timer is not None:
                    interval_line = next(
                        (
                            line
                            for line in timer.splitlines()
                            if line.startswith("OnUnitActiveSec=")
                        ),
                        None,
                    )
                    if interval_line is None:
                        raise ScheduleError("owned timer has no interval")
                    match = re.fullmatch(r"OnUnitActiveSec=([0-9]+)m", interval_line)
                    if match is None:
                        raise ScheduleError("owned timer has an invalid interval")
                    native_interval = int(match.group(1))
                registrations.append(
                    inventory_registration(
                        platform="systemd",
                        job_id=job_id,
                        run_argv=run_argv,
                        native_identity=(
                            f"{job_id}.timer"
                            if timer is not None
                            else f"{job_id}.service"
                        ),
                        authority="current user",
                        native_interval_minutes=native_interval,
                        native_complete=timer is not None,
                        enabled=None,
                        active=None,
                    )
                )
            except (ScheduleError, ValueError) as exc:
                untrusted.append(f"{job_id} ({exc})")
        return inventory_report(
            platform="systemd",
            registrations=registrations,
            untrusted_candidates=untrusted,
        )

    def _definition(self, spec: ScheduleSpec) -> dict[str, Any]:
        service_path, timer_path = self._paths(spec)
        command = " ".join(_systemd_quote(item) for item in spec.run_argv)
        path_value = ":".join(
            [
                str(Path.home() / ".local/bin"),
                str(Path.home() / ".npm-global/bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        )
        service = "\n".join(
            [
                "[Unit]",
                f"Description=Dependabot Watcher reconciliation [{spec.job_id}]",
                _ownership_marker(spec),
                "",
                "[Service]",
                "Type=oneshot",
                f"Environment={_systemd_quote(f'PATH={path_value}')}",
                f"ExecStart={command}",
                "NoNewPrivileges=true",
                "PrivateTmp=true",
                "",
            ]
        )
        timer = "\n".join(
            [
                "[Unit]",
                f"Description=Schedule Dependabot Watcher reconciliation [{spec.job_id}]",
                _ownership_marker(spec),
                "",
                "[Timer]",
                "OnStartupSec=1m",
                f"OnUnitActiveSec={spec.interval_minutes}m",
                "AccuracySec=5m",
                f"Unit={spec.job_id}.service",
                "",
                "[Install]",
                "WantedBy=timers.target",
                "",
            ]
        )
        return {
            "service_path": str(service_path),
            "timer_path": str(timer_path),
            "service": service,
            "timer": timer,
            "authority": "current user",
            "output": "systemd user journal plus Watcher runtime state",
        }

    def preview(self, spec: ScheduleSpec) -> dict[str, Any]:
        return schedule_report(
            action="preview",
            spec=spec,
            status="preview",
            definition=self._definition(spec),
            next_step="Run `dependabot-watcher schedule install` to create this user timer.",
        )

    def _systemctl(self) -> str:
        command = shutil.which("systemctl")
        if command is None:
            raise ScheduleError("systemctl is unavailable; a systemd user timer cannot be managed.")
        return command

    def _manager_status(self, spec: ScheduleSpec) -> dict[str, Any]:
        units: dict[str, dict[str, Any]] = {}
        for suffix, expected_description in (
            ("timer", f"Schedule Dependabot Watcher reconciliation [{spec.job_id}]"),
            ("service", f"Dependabot Watcher reconciliation [{spec.job_id}]"),
        ):
            result = self._runner.run(
                [
                    self._systemctl(),
                    "--user",
                    "show",
                    f"{spec.job_id}.{suffix}",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=UnitFileState",
                    "--property=Description",
                ]
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
            if result.returncode != 0 and not values:
                raise ScheduleError("Systemd user manager status could not be queried safely.")
            load_state = values.get("LoadState")
            if load_state is None:
                raise ScheduleError("Systemd user manager returned malformed schedule status.")
            loaded = load_state != "not-found"
            units[suffix] = {
                "loaded": loaded,
                "owned": loaded and values.get("Description") == expected_description,
                "active": values.get("ActiveState") == "active",
                "enabled": values.get("UnitFileState") in {"enabled", "enabled-runtime"},
                "load_state": load_state,
            }
        loaded = any(unit["loaded"] for unit in units.values())
        owned = all(not unit["loaded"] or unit["owned"] for unit in units.values())
        timer = units["timer"]
        return {
            "loaded": loaded,
            "owned": owned,
            "complete": all(unit["loaded"] for unit in units.values()),
            "timer_loaded": timer["loaded"],
            "active": timer["active"],
            "enabled": timer["enabled"],
            "units": units,
        }

    def install(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        service_path, timer_path = self._paths(spec)
        manager = self._manager_status(spec)
        if manager["loaded"] and not manager["owned"]:
            raise ScheduleError(
                "The Watcher systemd unit identity is occupied by an unowned loaded unit."
            )
        existing_service = _read_owned_definition(service_path, spec)
        existing_timer = _read_owned_definition(timer_path, spec)
        changed = (
            existing_service != definition["service"]
            or existing_timer != definition["timer"]
        )
        _atomic_text(service_path, str(definition["service"]))
        _atomic_text(timer_path, str(definition["timer"]))
        systemctl = self._systemctl()
        require_success(
            self._runner.run([systemctl, "--user", "daemon-reload"]),
            "daemon reload",
        )
        require_success(
            self._runner.run(
                [systemctl, "--user", "enable", "--now", f"{spec.job_id}.timer"]
            ),
            "timer install",
        )
        report = self.status(spec)
        report.update(action="install", changed=changed)
        report["capabilities"]["persistent_local_writes"] = True
        return report

    def status(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        service_path, timer_path = self._paths(spec)
        manager = self._manager_status(spec)
        files_exist = os.path.lexists(service_path) or os.path.lexists(timer_path)
        if manager["loaded"] and not manager["owned"]:
            raise ScheduleError(
                "The Watcher systemd unit identity is occupied by an unowned loaded unit."
            )
        if not files_exist and not manager["loaded"]:
            return schedule_report(
                action="status",
                spec=spec,
                status="missing",
                definition=definition,
                native_status={"installed": False, "active": False, "enabled": False},
            )
        existing_service = _read_owned_definition(service_path, spec)
        existing_timer = _read_owned_definition(timer_path, spec)
        drifted = (
            existing_service != definition["service"]
            or existing_timer != definition["timer"]
            or not manager["complete"]
        )
        if not files_exist and manager["loaded"]:
            return schedule_report(
                action="status",
                spec=spec,
                status="drifted",
                definition=definition,
                native_status={
                    "installed": True,
                    "enabled": manager["enabled"],
                    "active": manager["active"],
                    "drifted": True,
                    "orphaned": True,
                },
            )
        lifecycle_drift = manager["active"] != manager["enabled"]
        status = (
            "drifted"
            if drifted or lifecycle_drift
            else "active"
            if manager["active"] and manager["enabled"]
            else "disabled"
        )
        return schedule_report(
            action="status",
            spec=spec,
            status=status,
            definition=definition,
            native_status={
                "installed": True,
                "enabled": manager["enabled"],
                "active": manager["active"],
                "drifted": drifted or lifecycle_drift,
            },
        )

    def disable(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        service_path, timer_path = self._paths(spec)
        manager = self._manager_status(spec)
        files_exist = os.path.lexists(service_path) or os.path.lexists(timer_path)
        if manager["loaded"] and not manager["owned"]:
            raise ScheduleError(
                "The Watcher systemd unit identity is occupied by an unowned loaded unit."
            )
        if not files_exist and not manager["loaded"]:
            return schedule_report(
                action="disable",
                spec=spec,
                status="missing",
                definition=definition,
                changed=False,
            )
        _read_owned_definition(service_path, spec)
        _read_owned_definition(timer_path, spec)
        changed = False
        if manager["timer_loaded"]:
            systemctl = self._systemctl()
            result = self._runner.run(
                [systemctl, "--user", "disable", "--now", f"{spec.job_id}.timer"]
            )
            require_success(result, "timer disable")
            changed = True
        return schedule_report(
            action="disable",
            spec=spec,
            status="disabled",
            definition=definition,
            changed=changed,
        )

    def remove(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        service_path, timer_path = self._paths(spec)
        manager = self._manager_status(spec)
        existed = os.path.lexists(service_path) or os.path.lexists(timer_path)
        if manager["loaded"] and not manager["owned"]:
            raise ScheduleError(
                "The Watcher systemd unit identity is occupied by an unowned loaded unit."
            )
        if existed:
            _read_owned_definition(service_path, spec)
            _read_owned_definition(timer_path, spec)
        if existed or manager["loaded"]:
            systemctl = self._systemctl()
            if manager["timer_loaded"]:
                require_success(
                    self._runner.run(
                        [systemctl, "--user", "disable", "--now", f"{spec.job_id}.timer"]
                    ),
                    "timer disable before removal",
                )
            if manager["units"]["service"]["loaded"]:
                require_success(
                    self._runner.run(
                        [systemctl, "--user", "stop", f"{spec.job_id}.service"]
                    ),
                    "service stop before removal",
                )
            service_path.unlink(missing_ok=True)
            timer_path.unlink(missing_ok=True)
            require_success(
                self._runner.run([systemctl, "--user", "daemon-reload"]),
                "daemon reload",
            )
        return schedule_report(
            action="remove",
            spec=spec,
            status="removed" if existed or manager["loaded"] else "missing",
            definition=definition,
            changed=existed or manager["loaded"],
            next_step="Watcher configuration, run history, plans, and execution audit were preserved.",
        )
