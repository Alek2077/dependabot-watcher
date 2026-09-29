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


def _cadence(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return _plain(value)
    if value == 1_440:
        return "daily (every 24 hours)"
    if value == 10_080:
        return "weekly (every 7 days)"
    if value % 60 == 0:
        hours = value // 60
        unit = "hour" if hours == 1 else "hours"
        return f"every {hours} {unit}"
    return f"every {value} minutes"


def _profile_text(profile: Mapping[str, Any]) -> list[str]:
    advisory_value = profile.get("advisory")
    advisory = advisory_value if isinstance(advisory_value, Mapping) else {}
    operation_value = profile.get("operation")
    operation = operation_value if isinstance(operation_value, Mapping) else {}
    mode = str(advisory.get("mode") or "off")
    if mode == "off":
        ai = "off (scheduled runs make no model calls and use no provider quota or cost)"
    else:
        triage_value = advisory.get("triage")
        triage = triage_value if isinstance(triage_value, Mapping) else {}
        review_value = advisory.get("review")
        review = review_value if isinstance(review_value, Mapping) else {}
        budget_value = advisory.get("budget")
        budget = budget_value if isinstance(budget_value, Mapping) else {}
        call_limit = f"up to {_plain(budget.get('max_triage_calls', 0))} triage calls"
        review_summary = ""
        if mode == "reviewed":
            call_limit += f" and {_plain(budget.get('max_review_calls', 0))} review calls"
            review_summary = (
                f"review {_plain(review.get('model') or 'unknown model')} "
                f"({_plain(review.get('reasoning_effort') or 'unknown')} reasoning); "
            )
        ai = (
            f"{mode} via {_plain(advisory.get('provider') or 'unknown provider')}; "
            f"triage {_plain(triage.get('model') or 'unknown model')} "
            f"({_plain(triage.get('reasoning_effort') or 'unknown')} reasoning); "
            f"{review_summary}{call_limit} per changed run (not a financial cap); "
            "scheduled changes may consume provider quota or incur cost"
        )
        ai += "; selected private repository evidence may be sent to the provider"
    mutation = (
        "approval-gated only"
        if operation.get("mutation_enabled") is True
        else "disabled"
    )
    repositories = profile.get("repositories")
    if isinstance(repositories, list) and repositories:
        repository_summary = ", ".join(_plain(item) for item in repositories)
    else:
        repository_summary = _plain(profile.get("repository_count", 0))
    return [
        f"Repositories watched: {repository_summary}",
        f"AI during scheduled runs: {ai}",
        f"GitHub changes: {mutation}",
    ]


def _native_text(native_status: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if "next_run_at" in native_status:
        lines.append(
            f"Next scheduled run: {_plain(native_status.get('next_run_at') or 'not scheduled')}"
        )
    if native_status.get("has_run") is False:
        lines.append("Last scheduled run: never")
    elif native_status.get("has_run") is True:
        lines.append(
            f"Last scheduled run: {_plain(native_status.get('last_run_at') or 'unknown')}"
        )
        last_result = native_status.get("last_result")
        lines.append(
            f"Last native result: {_plain('unknown' if last_result is None else last_result)}"
        )
    missed_runs = native_status.get("missed_runs")
    if isinstance(missed_runs, int) and not isinstance(missed_runs, bool) and missed_runs:
        lines.append(f"Missed native starts: {_plain(missed_runs)}")
    return lines


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
        f"Cadence: {_cadence(spec.get('interval_minutes'))}",
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
    profile_value = report.get("profile")
    if isinstance(profile_value, Mapping):
        lines.extend(_profile_text(profile_value))
    verification_value = report.get("verification")
    if isinstance(verification_value, Mapping):
        lines.append(
            f"Manual verification: {_plain(_command(verification_value.get('argv')))}"
        )
    lines.extend(_native_text(native_status))
    if native_status:
        dedicated = {
            "has_run",
            "next_run_at",
            "last_run_at",
            "last_result",
            "missed_runs",
        }
        lines.append(
            "Native state: "
            + ", ".join(
                f"{_plain(key)}={_plain(value)}"
                for key, value in sorted(native_status.items())
                if key not in dedicated
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
        f"Cadence: {_escape(_cadence(spec.get('interval_minutes')))}  ",
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
    profile_value = report.get("profile")
    if isinstance(profile_value, Mapping):
        lines.extend(["", "## Recurring operation", ""])
        lines.extend(f"- {_escape(item)}" for item in _profile_text(profile_value))
    verification_value = report.get("verification")
    if isinstance(verification_value, Mapping):
        lines.extend(
            [
                "",
                f"Manual verification: `{_escape(_command(verification_value.get('argv')))}`  ",
            ]
        )
    native_lines = _native_text(native_status)
    if native_lines:
        lines.extend(["", "## Scheduled timing", ""])
        lines.extend(f"- {_escape(item)}" for item in native_lines)
    if native_status:
        lines.extend(["", "## Native state", ""])
        dedicated = {
            "has_run",
            "next_run_at",
            "last_run_at",
            "last_result",
            "missed_runs",
        }
        lines.extend(
            f"- `{_escape(key)}`: {_escape(value)}"
            for key, value in sorted(native_status.items())
            if key not in dedicated
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
