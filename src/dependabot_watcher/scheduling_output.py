from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Mapping

from .terminal import sanitize_human_value


def _plain(value: object) -> str:
    return sanitize_human_value(value)


def _escape(value: object) -> str:
    return _plain(value).replace("|", "\\|")


def _command(value: object) -> str:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return "unavailable"
    return subprocess.list2cmdline(value) if os.name == "nt" else shlex.join(value)


def _effective_command(definition: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    command = definition.get("command")
    arguments = definition.get("arguments")
    if isinstance(command, str) and isinstance(arguments, list) and all(
        isinstance(item, str) for item in arguments
    ):
        return subprocess.list2cmdline([command, *arguments])
    return _command(spec.get("run_argv"))


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
    if output_format == "markdown":
        return _markdown(report)
    if output_format == "text":
        return _text(report)
    raise ValueError(f"Unsupported output format: {output_format}")
