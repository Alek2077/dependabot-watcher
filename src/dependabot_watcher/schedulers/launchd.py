from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from ..runtime_state import (
    RuntimeStateError,
    delete_private_artifact,
    ensure_private_directory,
    ensure_user_owned_directory,
    load_private_json,
    save_private_json,
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


def _atomic_bytes(path: Path, value: bytes) -> None:
    try:
        ensure_user_owned_directory(path.parent)
    except RuntimeStateError as exc:
        raise ScheduleError(str(exc)) from exc
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if written <= 0:
                raise ScheduleError("Launchd definition could not be written completely.")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ScheduleError("Launchd definition could not be written safely.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_plist(path: Path) -> bytes:
    try:
        validate_user_owned_directory(path.parent)
        state = path.lstat()
    except (OSError, RuntimeStateError) as exc:
        raise ScheduleError("LaunchAgent definition cannot be inspected safely.") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ScheduleError("LaunchAgent definition is not a trusted regular file.")
    if hasattr(os, "getuid") and state.st_uid != os.getuid():
        raise ScheduleError("LaunchAgent definition is not owned by the current user.")
    if stat.S_IMODE(state.st_mode) & 0o077:
        raise ScheduleError("LaunchAgent definition is not private.")
    try:
        if state.st_size > 1_000_000:
            raise ScheduleError("LaunchAgent definition exceeds its safety ceiling.")
        return path.read_bytes()
    except OSError as exc:
        raise ScheduleError("LaunchAgent definition cannot be read safely.") from exc


class LaunchdAdapter:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        agent_directory: Path | None = None,
        user_id: int | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._agent_directory = agent_directory or Path.home() / "Library/LaunchAgents"
        self._user_id = os.getuid() if user_id is None else user_id

    def _label(self, spec: ScheduleSpec) -> str:
        return f"com.dependabot-watcher.{spec.job_id.rsplit('-', 1)[-1]}"

    def _path(self, spec: ScheduleSpec) -> Path:
        return self._agent_directory / f"{self._label(spec)}.plist"

    def _registration_path(self, spec: ScheduleSpec) -> Path:
        return Path("schedules") / f"launchd-{spec.job_id}.json"

    def _registration(self, spec: ScheduleSpec) -> dict[str, Any] | None:
        relative_path = self._registration_path(spec)
        path = spec.state_directory / relative_path
        schedules_directory = spec.state_directory / "schedules"
        try:
            ensure_private_directory(spec.state_directory)
            if os.path.lexists(schedules_directory):
                validate_user_owned_directory(schedules_directory)
        except RuntimeStateError as exc:
            raise ScheduleError("Launchd ownership record path cannot be trusted.") from exc
        if not os.path.lexists(path):
            return None
        try:
            value = load_private_json(spec.state_directory, relative_path)
        except RuntimeStateError as exc:
            raise ScheduleError("Launchd ownership record cannot be read safely.") from exc
        expected = {
            "schema_version": 1,
            "platform": "launchd",
            "job_id": spec.job_id,
            "label": self._label(spec),
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            raise ScheduleError("Launchd ownership record does not match this schedule.")
        if set(value) - {*expected, "disabled"} or not isinstance(
            value.get("disabled", False), bool
        ):
            raise ScheduleError("Launchd ownership record has an invalid shape.")
        return {**expected, "disabled": bool(value.get("disabled", False))}

    def _registration_owned(self, spec: ScheduleSpec) -> bool:
        return self._registration(spec) is not None

    def _manager_candidate_labels(self) -> set[str]:
        result = self._runner.run(
            [self._launchctl(), "print", f"gui/{self._user_id}"]
        )
        require_success(result, "launchd inventory")
        return set(
            re.findall(
                r"\bcom\.dependabot-watcher\.[A-Za-z0-9._-]+\b",
                result.stdout,
            )
        )

    def inventory(self) -> dict[str, Any]:
        labels = self._manager_candidate_labels()
        if os.path.lexists(self._agent_directory):
            try:
                validate_user_owned_directory(self._agent_directory)
            except RuntimeStateError as exc:
                raise ScheduleError("LaunchAgents directory cannot be trusted.") from exc
            labels.update(
                path.name.removesuffix(".plist")
                for path in self._agent_directory.glob("com.dependabot-watcher.*.plist")
            )
        registrations: list[dict[str, Any]] = []
        untrusted: list[str] = []
        for label in sorted(labels):
            path = self._agent_directory / f"{label}.plist"
            suffix = label.removeprefix("com.dependabot-watcher.")
            candidate = label
            try:
                job_id = validate_schedule_job_id(f"dependabot-watcher-{suffix}")
                if not os.path.lexists(path):
                    raise ScheduleError("loaded manager job has no trusted definition")
                stored = plistlib.loads(_read_plist(path))
                if not isinstance(stored, dict):
                    raise ScheduleError("definition has an invalid shape")
                environment = stored.get("EnvironmentVariables")
                marker = (
                    environment.get("DEPENDABOT_WATCHER_JOB_ID")
                    if isinstance(environment, dict)
                    else None
                )
                if stored.get("Label") != label or marker != job_id:
                    raise ScheduleError("definition has mismatched ownership markers")
                arguments = stored.get("ProgramArguments")
                if not isinstance(arguments, list) or not arguments or any(
                    not isinstance(item, str) for item in arguments
                ):
                    raise ScheduleError("definition has an invalid Watcher command")
                interval_seconds = stored.get("StartInterval")
                if (
                    isinstance(interval_seconds, bool)
                    or not isinstance(interval_seconds, int)
                    or interval_seconds <= 0
                    or interval_seconds % 60 != 0
                ):
                    raise ScheduleError("definition has an invalid native interval")
                registrations.append(
                    inventory_registration(
                        platform="launchd",
                        job_id=job_id,
                        run_argv=arguments,
                        native_identity=label,
                        authority="current GUI user",
                        native_interval_minutes=interval_seconds // 60,
                        enabled=None,
                        active=None,
                    )
                )
            except (OSError, plistlib.InvalidFileException, ScheduleError) as exc:
                untrusted.append(f"{candidate} ({exc})")
        return inventory_report(
            platform="launchd",
            registrations=registrations,
            untrusted_candidates=untrusted,
        )

    def _save_registration(self, spec: ScheduleSpec, *, disabled: bool) -> None:
        try:
            save_private_json(
                spec.state_directory,
                self._registration_path(spec),
                {
                    "schema_version": 1,
                    "platform": "launchd",
                    "job_id": spec.job_id,
                    "label": self._label(spec),
                    "disabled": disabled,
                },
            )
        except RuntimeStateError as exc:
            raise ScheduleError("Launchd ownership record could not be saved safely.") from exc

    def _remove_registration(self, spec: ScheduleSpec) -> bool:
        try:
            return delete_private_artifact(
                spec.state_directory,
                self._registration_path(spec),
            )
        except RuntimeStateError as exc:
            raise ScheduleError("Launchd ownership record could not be removed safely.") from exc

    def _definition(self, spec: ScheduleSpec) -> dict[str, Any]:
        log_directory = spec.state_directory / "logs"
        plist = {
            "Label": self._label(spec),
            "ProgramArguments": list(spec.run_argv),
            "StartInterval": spec.interval_minutes * 60,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "ThrottleInterval": 60,
            "StandardOutPath": str(log_directory / "scheduled.out.log"),
            "StandardErrorPath": str(log_directory / "scheduled.err.log"),
            "EnvironmentVariables": {
                "DEPENDABOT_WATCHER_JOB_ID": spec.job_id,
                "PATH": ":".join(
                    [
                        str(Path.home() / ".local/bin"),
                        str(Path.home() / ".npm-global/bin"),
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        "/usr/bin",
                        "/bin",
                    ]
                )
            },
        }
        return {
            "path": str(self._path(spec)),
            "label": self._label(spec),
            "plist": plist,
            "authority": "current GUI user",
            "output": str(log_directory),
        }

    def _stored_definition(self, spec: ScheduleSpec) -> dict[str, Any] | None:
        path = self._path(spec)
        if not os.path.lexists(path):
            return None
        try:
            stored = plistlib.loads(_read_plist(path))
        except (OSError, plistlib.InvalidFileException) as exc:
            raise ScheduleError("LaunchAgent definition cannot be parsed safely.") from exc
        if not isinstance(stored, dict):
            raise ScheduleError("LaunchAgent definition has an invalid shape.")
        environment = stored.get("EnvironmentVariables")
        marker = (
            environment.get("DEPENDABOT_WATCHER_JOB_ID")
            if isinstance(environment, dict)
            else None
        )
        if stored.get("Label") != self._label(spec) or marker != spec.job_id:
            raise ScheduleError(
                f"LaunchAgent path is occupied by a definition not owned by Dependabot Watcher: {path}."
            )
        return stored

    def preview(self, spec: ScheduleSpec) -> dict[str, Any]:
        return schedule_report(
            action="preview",
            spec=spec,
            status="preview",
            definition=self._definition(spec),
            next_step="Run `dependabot-watcher schedule install` to create this LaunchAgent.",
        )

    def _launchctl(self) -> str:
        command = shutil.which("launchctl")
        if command is None:
            raise ScheduleError("launchctl is unavailable; a LaunchAgent cannot be managed.")
        return command

    def _target(self, spec: ScheduleSpec) -> str:
        return f"gui/{self._user_id}/{self._label(spec)}"

    def _manager_status(self, spec: ScheduleSpec) -> dict[str, Any]:
        result = self._runner.run([self._launchctl(), "print", self._target(spec)])
        if result.returncode != 0:
            if result.returncode in {3, 113}:
                return {"loaded": False}
            raise ScheduleError("Launchd manager status could not be queried safely.")
        return {"loaded": True}

    def _bootout(self, spec: ScheduleSpec, command: list[str], operation: str) -> bool:
        result = self._runner.run(command)
        if result.returncode not in {0, 3}:
            require_success(result, operation)
        if self._manager_status(spec)["loaded"]:
            raise ScheduleError(
                "Launchd still reports the Watcher job loaded after bootout."
            )
        return result.returncode == 0

    def _restore_install_state(
        self,
        spec: ScheduleSpec,
        *,
        path: Path,
        launchctl: str,
        previous_payload: bytes | None,
        previous_registration: dict[str, Any] | None,
        previous_loaded: bool,
    ) -> None:
        """Restore the last trusted launchd state after an install failure."""

        try:
            if self._manager_status(spec)["loaded"]:
                command = (
                    [launchctl, "bootout", f"gui/{self._user_id}", str(path)]
                    if os.path.lexists(path)
                    else [launchctl, "bootout", self._target(spec)]
                )
                self._bootout(spec, command, "LaunchAgent install rollback")

            if previous_payload is not None:
                _atomic_bytes(path, previous_payload)
            else:
                path.unlink(missing_ok=True)

            if previous_registration is None:
                self._remove_registration(spec)
                was_disabled = False
            else:
                was_disabled = bool(previous_registration["disabled"])
                self._save_registration(spec, disabled=was_disabled)

            enablement = "disable" if was_disabled else "enable"
            require_success(
                self._runner.run([launchctl, enablement, self._target(spec)]),
                f"LaunchAgent install rollback {enablement}",
            )
            if previous_loaded:
                if previous_payload is None:
                    raise ScheduleError(
                        "LaunchAgent install rollback unloaded an owned orphan but could not restore the prior loaded job without a trusted definition; ownership evidence was preserved."
                    )
                require_success(
                    self._runner.run(
                        [launchctl, "bootstrap", f"gui/{self._user_id}", str(path)]
                    ),
                    "LaunchAgent install rollback bootstrap",
                )
                if not self._manager_status(spec)["loaded"]:
                    raise ScheduleError(
                        "LaunchAgent install rollback could not restore the prior loaded job."
                    )
        except (OSError, ScheduleError) as exc:
            if isinstance(exc, ScheduleError):
                raise
            raise ScheduleError(
                "LaunchAgent install rollback could not restore prior state safely."
            ) from exc

    def install(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        path = self._path(spec)
        payload = plistlib.dumps(definition["plist"], fmt=plistlib.FMT_XML, sort_keys=True)
        stored = self._stored_definition(spec)
        manager = self._manager_status(spec)
        registration = self._registration(spec)
        if manager["loaded"] and stored is None and registration is None:
            raise ScheduleError(
                "The Watcher launchd label is occupied by an unowned loaded job."
            )
        exact_active_registration = (
            stored == definition["plist"]
            and manager["loaded"]
            and registration is not None
            and registration["disabled"] is False
        )
        if exact_active_registration:
            report = self.status(spec)
            report.update(action="install", changed=False)
            report["capabilities"]["persistent_local_writes"] = True
            return report
        changed = (
            stored != definition["plist"]
            or not manager["loaded"]
            or registration is None
            or registration["disabled"] is True
        )
        previous_payload = _read_plist(path) if stored is not None else None
        previous_registration = registration
        previous_loaded = bool(manager["loaded"])
        try:
            ensure_private_directory(spec.state_directory)
            ensure_private_directory(spec.state_directory / "logs")
            ensure_private_directory(spec.state_directory / "schedules")
        except RuntimeStateError as exc:
            raise ScheduleError(str(exc)) from exc
        launchctl = self._launchctl()
        try:
            if manager["loaded"]:
                command = (
                    [launchctl, "bootout", f"gui/{self._user_id}", str(path)]
                    if stored is not None
                    else [launchctl, "bootout", self._target(spec)]
                )
                self._bootout(spec, command, "existing LaunchAgent disable")
            require_success(
                self._runner.run([launchctl, "enable", self._target(spec)]),
                "LaunchAgent enable",
            )
            _atomic_bytes(path, payload)
            require_success(
                self._runner.run(
                    [launchctl, "bootstrap", f"gui/{self._user_id}", str(path)]
                ),
                "LaunchAgent install",
            )
            self._save_registration(spec, disabled=False)
        except Exception as exc:
            try:
                self._restore_install_state(
                    spec,
                    path=path,
                    launchctl=launchctl,
                    previous_payload=previous_payload,
                    previous_registration=previous_registration,
                    previous_loaded=previous_loaded,
                )
            except ScheduleError as rollback_error:
                raise ScheduleError(
                    "LaunchAgent install failed and prior state could not be restored safely; ownership evidence was preserved."
                ) from rollback_error
            if isinstance(exc, ScheduleError):
                raise
            raise ScheduleError(
                "LaunchAgent install failed; prior state was restored."
            ) from exc
        report = self.status(spec)
        report.update(action="install", changed=changed)
        report["capabilities"]["persistent_local_writes"] = True
        return report

    def status(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        path = self._path(spec)
        stored = self._stored_definition(spec)
        manager = self._manager_status(spec)
        registration = self._registration(spec)
        if manager["loaded"] and stored is None and registration is None:
            raise ScheduleError(
                "The Watcher launchd label is occupied by an unowned loaded job."
            )
        if not os.path.lexists(path) and not manager["loaded"]:
            return schedule_report(
                action="status",
                spec=spec,
                status="missing",
                definition=definition,
                native_status={"installed": False, "active": False},
            )
        if not os.path.lexists(path) and manager["loaded"]:
            return schedule_report(
                action="status",
                spec=spec,
                status="drifted",
                definition=definition,
                native_status={
                    "installed": True,
                    "active": True,
                    "drifted": True,
                    "orphaned": True,
                },
            )
        if stored is None:
            raise ScheduleError("LaunchAgent definition disappeared during status readback.")
        persistent_disabled = (
            registration.get("disabled") if registration is not None else None
        )
        drifted = (
            stored != definition["plist"]
            or persistent_disabled is None
            or (manager["loaded"] and persistent_disabled is True)
            or (not manager["loaded"] and persistent_disabled is False)
        )
        active = manager["loaded"]
        status = (
            "drifted"
            if drifted
            else "active"
            if active
            else "disabled"
        )
        return schedule_report(
            action="status",
            spec=spec,
            status=status,
            definition=definition,
            native_status={
                "installed": True,
                "active": active,
                "persistent_disabled": persistent_disabled,
                "drifted": drifted,
            },
        )

    def disable(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        path = self._path(spec)
        stored = self._stored_definition(spec)
        manager = self._manager_status(spec)
        registration = self._registration(spec)
        if manager["loaded"] and stored is None and registration is None:
            raise ScheduleError(
                "The Watcher launchd label is occupied by an unowned loaded job."
            )
        if not os.path.lexists(path) and not manager["loaded"]:
            return schedule_report(
                action="disable",
                spec=spec,
                status="missing",
                definition=definition,
            )
        launchctl = self._launchctl()
        require_success(
            self._runner.run([launchctl, "disable", self._target(spec)]),
            "LaunchAgent persistent disable",
        )
        changed = False
        if manager["loaded"]:
            command = (
                [launchctl, "bootout", f"gui/{self._user_id}", str(path)]
                if os.path.lexists(path)
                else [launchctl, "bootout", self._target(spec)]
            )
            changed = self._bootout(spec, command, "LaunchAgent disable")
        self._save_registration(spec, disabled=True)
        return schedule_report(
            action="disable",
            spec=spec,
            status="disabled",
            definition=definition,
            changed=changed or manager["loaded"],
        )

    def remove(self, spec: ScheduleSpec) -> dict[str, Any]:
        definition = self._definition(spec)
        path = self._path(spec)
        stored = self._stored_definition(spec)
        manager = self._manager_status(spec)
        existed = os.path.lexists(path)
        registration = self._registration(spec)
        registered = registration is not None
        if manager["loaded"] and stored is None and not registered:
            raise ScheduleError(
                "The Watcher launchd label is occupied by an unowned loaded job."
            )
        if existed or manager["loaded"]:
            target = (
                [self._launchctl(), "bootout", f"gui/{self._user_id}", str(path)]
                if existed
                else [self._launchctl(), "bootout", self._target(spec)]
            )
            self._bootout(spec, target, "LaunchAgent disable before removal")
            path.unlink(missing_ok=True)
        marker_removed = self._remove_registration(spec) if registered else False
        if existed or manager["loaded"] or marker_removed:
            require_success(
                self._runner.run(
                    [self._launchctl(), "enable", self._target(spec)]
                ),
                "LaunchAgent disable-state cleanup",
            )
        return schedule_report(
            action="remove",
            spec=spec,
            status="removed" if existed or manager["loaded"] or marker_removed else "missing",
            definition=definition,
            changed=existed or manager["loaded"] or marker_removed,
            next_step="Watcher configuration, run history, plans, and execution audit were preserved.",
        )
