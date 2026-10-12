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


def _command(notification: Mapping[str, Any]) -> str | None:
    next_action = notification.get("next")
    if not isinstance(next_action, Mapping):
        return None
    argv = next_action.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        return None
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _ai_summary(report: Mapping[str, Any]) -> str:
    supervisor = report.get("supervisor")
    if isinstance(supervisor, Mapping) and supervisor.get("mode") == "approval_first":
        dispatch = report.get("supervisor_dispatch")
        stage = ("background investigation requested; completion/usage not yet verified"
                 if isinstance(dispatch, Mapping) and dispatch.get("status") == "submitted"
                 else "configured; check saved jobs for actual execution and usage")
        return f"{_plain(supervisor.get('model'))} / {_plain(supervisor.get('reasoning_effort'))} | {stage}"
    coverage = report.get("advisory_coverage")
    data = coverage if isinstance(coverage, Mapping) else {}
    mode = data.get("mode") or "off"
    if mode == "off":
        return "off (no AI used)"
    usage = report.get("provider_usage")
    usage_data = usage if isinstance(usage, Mapping) else {}
    current_calls = int(usage_data.get("call_count") or 0)
    prior_calls = int(data.get("provider_call_count") or 0)
    summary = (
        f"{_plain(mode)} | advised {_plain(data.get('advised_candidate_count', 0))}/"
        f"{_plain(data.get('eligible_candidate_count', 0))} | "
        f"deferred {_plain(data.get('deferred_candidate_count', 0))} | "
        f"calls this run {_plain(current_calls)}"
    )
    if current_calls == 0 and prior_calls:
        summary += f" | retained plan calls {_plain(prior_calls)}"
    return summary


def _plan_summary(plan: Mapping[str, Any]) -> str:
    current = _plain(plan.get("plan_id") or "none")
    superseded = plan.get("supersedes_plan_id")
    if superseded:
        return f"{current} | supersedes {_plain(superseded)}"
    return current


def _delivery_summary(delivery: Mapping[str, Any]) -> str:
    mode = _plain(delivery.get("mode") or "off")
    status = str(delivery.get("status") or "unknown")
    if status == "delivered" and delivery.get("assurance") == "adapter_accepted_only":
        return f"{mode} / submitted to the desktop adapter (display not confirmed)"
    return f"{mode} / {_plain(status)}"


def _cadence_summary(schedule: Mapping[str, Any]) -> str:
    if schedule.get("expected_interval_minutes") is None:
        return "no cadence supplied for this run; schedule installation not checked"
    timing = "overdue catch-up" if schedule.get("overdue_catch_up") else "current"
    return f"{timing}; schedule installation not checked"


def _text(report: Mapping[str, Any]) -> str:
    summary = report.get("summary")
    queue = summary if isinstance(summary, Mapping) else {}
    lineage = report.get("plan_lineage")
    plan = lineage if isinstance(lineage, Mapping) else {}
    notification = report.get("notification")
    notice = notification if isinstance(notification, Mapping) else {}
    delivery_value = notice.get("delivery")
    delivery = delivery_value if isinstance(delivery_value, Mapping) else {}
    execution_value = report.get("execution_observation")
    execution = execution_value if isinstance(execution_value, Mapping) else {}
    schedule_value = report.get("schedule_observation")
    schedule = schedule_value if isinstance(schedule_value, Mapping) else {}
    lines = [
        "Dependabot Watcher reconciliation",
        f"Status: {_plain(report.get('status'))}",
        f"Outcome: {_plain(report.get('outcome'))}",
        f"Run: {_plain(report.get('run_id'))}",
        f"Evidence revision: {_plain(report.get('evidence_revision'))}",
        (
            "Queue: "
            f"{_plain(queue.get('repository_count', 0))} repositories | "
            f"{_plain(queue.get('open_pull_request_count', 0))} open PRs | "
            f"{_plain(queue.get('open_alert_count', 0))} open alerts | "
            f"{_plain(queue.get('attention_required_count', 0))} attention required"
        ),
        f"AI supervision: {_ai_summary(report)}",
        f"Plan: {_plan_summary(plan)}",
        (
            "Execution history: "
            f"{_plain(execution.get('action_count', 0))} actions | "
            f"{_plain(execution.get('recovery_required_count', 0))} need recovery"
        ),
        f"Cadence: {_cadence_summary(schedule)}",
        f"Persistent state: {_plain(report.get('state_path'))}",
        (
            f"Notification: required ({_plain(notice.get('reason'))})"
            if notice.get("needed")
            else f"Notification: silent ({_plain(notice.get('reason'))})"
        ),
        f"Delivery: {_delivery_summary(delivery)}",
        "Mutation authorized: no",
    ]
    if notice.get("message"):
        lines.append(f"Action: {_plain(notice['message'])}")
    command = _command(notice)
    if command:
        lines.append(f"Next: {_plain(command)}")
    if report.get("retry_at"):
        lines.append(f"Retry after: {_plain(report['retry_at'])}")
    return "\n".join(lines) + "\n"


def _markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary")
    queue = summary if isinstance(summary, Mapping) else {}
    lineage = report.get("plan_lineage")
    plan = lineage if isinstance(lineage, Mapping) else {}
    notification = report.get("notification")
    notice = notification if isinstance(notification, Mapping) else {}
    delivery_value = notice.get("delivery")
    delivery = delivery_value if isinstance(delivery_value, Mapping) else {}
    execution_value = report.get("execution_observation")
    execution = execution_value if isinstance(execution_value, Mapping) else {}
    schedule_value = report.get("schedule_observation")
    schedule = schedule_value if isinstance(schedule_value, Mapping) else {}
    lines = [
        "# Dependabot Watcher reconciliation",
        "",
        f"Status: **{_escape(report.get('status'))}**  ",
        f"Outcome: **{_escape(report.get('outcome'))}**  ",
        f"Run: `{_escape(report.get('run_id'))}`  ",
        f"Evidence revision: `{_escape(report.get('evidence_revision'))}`  ",
        f"AI supervision: {_escape(_ai_summary(report))}  ",
        f"Plan: `{_escape(_plan_summary(plan))}`  ",
        (
            "Execution history: "
            f"{_escape(execution.get('action_count', 0))} actions | "
            f"{_escape(execution.get('recovery_required_count', 0))} need recovery  "
        ),
        f"Cadence: {_escape(_cadence_summary(schedule))}  ",
        "Mutation authorized: **no**",
        "",
        "| Repositories | Open PRs | Open alerts | Attention required |",
        "|---:|---:|---:|---:|",
        (
            f"| {_escape(queue.get('repository_count', 0))} | "
            f"{_escape(queue.get('open_pull_request_count', 0))} | "
            f"{_escape(queue.get('open_alert_count', 0))} | "
            f"{_escape(queue.get('attention_required_count', 0))} |"
        ),
        "",
        (
            f"Notification: **required** (`{_escape(notice.get('reason'))}`)"
            if notice.get("needed")
            else f"Notification: **silent** (`{_escape(notice.get('reason'))}`)"
        ),
        f"Delivery: {_escape(_delivery_summary(delivery))}",
    ]
    if notice.get("message"):
        lines.extend(["", f"Action: {_escape(notice['message'])}"])
    command = _command(notice)
    if command:
        lines.extend(["", f"Next: `{_escape(command)}`"])
    if report.get("retry_at"):
        lines.extend(["", f"Retry after: `{_escape(report['retry_at'])}`"])
    lines.extend(["", f"State: `{_escape(report.get('state_path'))}`"])
    return "\n".join(lines) + "\n"


def render_reconciliation(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output_format == "markdown":
        return _markdown(report)
    if output_format == "text":
        return _text(report)
    raise ValueError(f"Unsupported output format: {output_format}")
