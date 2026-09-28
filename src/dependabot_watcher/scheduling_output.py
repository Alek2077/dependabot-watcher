from __future__ import annotations

import json
import os
import shlex
from typing import Any, Mapping

from .terminal import sanitize_human_value


def _plain(value: object) -> str:
    return sanitize_human_value(value)


def _escape(value: object) -> str:
    return _plain(value).replace("|", "\\|")


def _powershell_join(value: list[str]) -> str:
    return "& " + " ".join("'" + item.replace("'", "''") + "'" for item in value)


def _command(value: object) -> str:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return "unavailable"
    return _powershell_join(value) if os.name == "nt" else shlex.join(value)


def _effective_command(definition: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    command = definition.get("command")
    arguments = definition.get("arguments")
    if isinstance(command, str) and isinstance(arguments, list) and all(
        isinstance(item, str) for item in arguments
    ):
        return _powershell_join([command, *arguments])
    return _command(spec.get("run_argv"))


def _inventory_registrations(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = report.get("registrations")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _inventory_text(report: Mapping[str, Any]) -> str:
    registrations = _inventory_registrations(report)
    lines = [
        "Dependabot Watcher schedule inventory",
        f"Status: {_plain(report.get('status'))}",
        f"Platform: {_plain(report.get('platform'))}",
        f"Verified registrations: {len(registrations)}",
        "Changed local scheduler: no",
        "GitHub mutation authorized: no",
    ]
    for index, registration in enumerate(registrations, start=1):
        lines.extend(
            [
                "",
                f"Registration {index}: {_plain(registration.get('job_id'))}",
                f"Native identity: {_plain(registration.get('native_identity'))}",
                f"Configuration: {_plain(registration.get('config_path'))}",
                f"State: {_plain(registration.get('state_directory'))}",
                f"Cadence: every {_plain(registration.get('interval_minutes'))} minutes",
                f"Notifications: {_plain(registration.get('notification_mode') or 'off')}",
                f"Enabled: {_plain(registration.get('enabled'))}",
                f"Active: {_plain(registration.get('active'))}",
                f"Drift detected: {'yes' if registration.get('drifted') else 'no'}",
                f"Recovery: {_plain(_command(registration.get('recovery_command')))}",
            ]
        )
    candidates = report.get("untrusted_candidates")
    if isinstance(candidates, list) and candidates:
        lines.extend(["", "Untrusted candidates (never removal authority):"])
        lines.extend(f"- {_plain(item)}" for item in candidates)
    if report.get("next"):
        lines.extend(["", f"Next: {_plain(report['next'])}"])
    return "\n".join(lines) + "\n"


def _inventory_markdown(report: Mapping[str, Any]) -> str:
    registrations = _inventory_registrations(report)
    lines = [
        "# Dependabot Watcher schedule inventory",
        "",
        f"Status: **{_escape(report.get('status'))}**  ",
        f"Platform: `{_escape(report.get('platform'))}`  ",
        f"Verified registrations: **{len(registrations)}**  ",
        "Changed local scheduler: **no**  ",
        "GitHub mutation authorized: **no**",
    ]
    for index, registration in enumerate(registrations, start=1):
        lines.extend(
            [
                "",
                f"## Registration {index}: `{_escape(registration.get('job_id'))}`",
                "",
                f"- Native identity: `{_escape(registration.get('native_identity'))}`",
                f"- Configuration: `{_escape(registration.get('config_path'))}`",
                f"- State: `{_escape(registration.get('state_directory'))}`",
                f"- Cadence: every {_escape(registration.get('interval_minutes'))} minutes",
                f"- Notifications: `{_escape(registration.get('notification_mode') or 'off')}`",
                f"- Enabled: `{_escape(registration.get('enabled'))}`",
                f"- Active: `{_escape(registration.get('active'))}`",
                f"- Drift detected: **{'yes' if registration.get('drifted') else 'no'}**",
                f"- Recovery: `{_escape(_command(registration.get('recovery_command')))}`",
            ]
        )
    candidates = report.get("untrusted_candidates")
    if isinstance(candidates, list) and candidates:
        lines.extend(["", "## Untrusted candidates", ""])
        lines.append("These entries are never removal authority.")
        lines.append("")
        lines.extend(f"- {_escape(item)}" for item in candidates)
    if report.get("next"):
        lines.extend(["", f"Next: {_escape(report['next'])}"])
    return "\n".join(lines) + "\n"


def _text(report: Mapping[str, Any]) -> str:
    schedule = report.get("schedule")
    spec = schedule if isinstance(schedule, Mapping) else {}
    native = report.get("native_status")
    native_status = native if isinstance(native, Mapping) else {}
    raw_definition = report.get("definition")
    definition = raw_definition if isinstance(raw_definition, Mapping) else {}
    lines = [
        "Dependabot Watcher schedule",
        f"Action: {_plain(report.get('action'))}",
        f"Status: {_plain(report.get('status'))}",
        f"Platform: {_plain(spec.get('platform'))}",
        f"Job: {_plain(spec.get('job_id'))}",
        f"Cadence: every {_plain(spec.get('interval_minutes'))} minutes",
        f"Notifications: {_plain(spec.get('notification_mode') or 'off')}",
        f"Command: {_plain(_effective_command(definition, spec))}",
        f"Configuration: {_plain(spec.get('config_path'))}",
        f"State: {_plain(spec.get('state_directory'))}",
        f"Authority: {_plain(definition.get('authority') or 'current user')}",
        f"Output: {_plain(definition.get('output') or 'Watcher runtime state')}",
        f"Missed runs: {_plain(report.get('missed_run_policy'))}",
        f"Overlap: {_plain(report.get('overlap_policy'))}",
        f"Changed local scheduler: {'yes' if report.get('changed') else 'no'}",
        "GitHub mutation authorized: no",
    ]
    if native_status:
        lines.append(
            "Native state: "
            + ", ".join(
                f"{_plain(key)}={_plain(value)}"
                for key, value in sorted(native_status.items())
            )
        )
    if report.get("next"):
        lines.append(f"Next: {_plain(report['next'])}")
    return "\n".join(lines) + "\n"


def _markdown(report: Mapping[str, Any]) -> str:
    schedule = report.get("schedule")
    spec = schedule if isinstance(schedule, Mapping) else {}
    native = report.get("native_status")
    native_status = native if isinstance(native, Mapping) else {}
    raw_definition = report.get("definition")
    definition = raw_definition if isinstance(raw_definition, Mapping) else {}
    lines = [
        "# Dependabot Watcher schedule",
        "",
        f"Action: **{_escape(report.get('action'))}**  ",
        f"Status: **{_escape(report.get('status'))}**  ",
        f"Platform: `{_escape(spec.get('platform'))}`  ",
        f"Job: `{_escape(spec.get('job_id'))}`  ",
        f"Cadence: every {_escape(spec.get('interval_minutes'))} minutes  ",
        f"Notifications: `{_escape(spec.get('notification_mode') or 'off')}`  ",
        f"Command: `{_escape(_effective_command(definition, spec))}`  ",
        f"Configuration: `{_escape(spec.get('config_path'))}`  ",
        f"State: `{_escape(spec.get('state_directory'))}`  ",
        f"Authority: `{_escape(definition.get('authority') or 'current user')}`  ",
        f"Output: `{_escape(definition.get('output') or 'Watcher runtime state')}`  ",
        f"Missed runs: {_escape(report.get('missed_run_policy'))}  ",
        f"Overlap: {_escape(report.get('overlap_policy'))}  ",
        f"Changed local scheduler: **{'yes' if report.get('changed') else 'no'}**  ",
        "GitHub mutation authorized: **no**",
    ]
    if native_status:
        lines.extend(["", "## Native state", ""])
        lines.extend(
            f"- `{_escape(key)}`: {_escape(value)}"
            for key, value in sorted(native_status.items())
        )
    if report.get("next"):
        lines.extend(["", f"Next: {_escape(report['next'])}"])
    return "\n".join(lines) + "\n"


def render_schedule(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=True) + "\n"
    if report.get("action") == "inventory":
        if output_format == "markdown":
            return _inventory_markdown(report)
        if output_format == "text":
            return _inventory_text(report)
    if output_format == "markdown":
        return _markdown(report)
    if output_format == "text":
        return _text(report)
    raise ValueError(f"Unsupported output format: {output_format}")
