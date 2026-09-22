from __future__ import annotations

import json
from typing import Any


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("candidate_identity") or {}


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "Dependabot Watcher inspection",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        (
            f"Repositories: {summary['repository_count']} | "
            f"Open PRs: {summary['open_pull_request_count']} | "
            f"Open alerts: {summary['open_alert_count']} | "
            f"Attention required: {summary['attention_required_count']}"
        ),
    ]
    for repository in report["repositories"]:
        repo_summary = repository["summary"]
        lines.extend(
            [
                "",
                f"{repository['repository']} (expected base: {repository['expected_base']})",
                (
                    f"  PRs: {repo_summary['open_pull_request_count']} | "
                    f"Alerts: {repo_summary['open_alert_count']} | "
                    f"Unresolved alerts: {repo_summary['unresolved_alert_count']}"
                ),
            ]
        )
        if not repository["pull_requests"]:
            lines.append("  Pull requests: none")
        for pull_request in repository["pull_requests"]:
            identity = _identity(pull_request)
            lines.append(
                f"  PR #{identity.get('number', '?')}: {identity.get('title') or 'Untitled'} "
                f"[{pull_request.get('classification', 'unknown')}]"
            )
            for reason in pull_request.get("reasons", []):
                lines.append(f"    - {reason}")
        for alert in repository["alerts"]:
            lines.append(
                f"  Alert #{alert.get('number', '?')}: {alert.get('dependency_name') or 'unknown package'} "
                f"({alert.get('severity') or 'unknown severity'}, {alert.get('classification')})"
            )
        if repository["recent_sensitive_merges"]:
            lines.append("  Recent sensitive non-Dependabot merges:")
            for merge in repository["recent_sensitive_merges"]:
                matched_paths = ", ".join(
                    item["path"] for item in merge.get("matched_files", [])
                )
                lines.append(
                    f"    - PR #{merge.get('number', '?')}: {merge.get('title') or 'Untitled'} "
                    f"[{matched_paths or 'sensitive path'}]"
                )
        for truncation in repository["truncations"]:
            lines.append(f"  TRUNCATED: {truncation['surface']} at {truncation['limit']} items")
        for error in repository["errors"]:
            lines.append(f"  ERROR [{error['surface']}]: {error['message']}")
    return "\n".join(lines) + "\n"


def _escape_markdown(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Dependabot Watcher inspection",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Status: **{report['status']}**",
        "",
        "| Repositories | Open PRs | Open alerts | Attention required | Errors | Truncations |",
        "|---:|---:|---:|---:|---:|---:|",
        (
            f"| {summary['repository_count']} | {summary['open_pull_request_count']} | "
            f"{summary['open_alert_count']} | {summary['attention_required_count']} | "
            f"{summary['error_count']} | {summary['truncation_count']} |"
        ),
    ]
    for repository in report["repositories"]:
        lines.extend(
            [
                "",
                f"## {_escape_markdown(repository['repository'])}",
                "",
                f"Expected base: `{_escape_markdown(repository['expected_base'])}`",
                "",
                "### Pull requests",
                "",
            ]
        )
        if repository["pull_requests"]:
            lines.extend(
                [
                    "| PR | Title | Classification | Checks | Paths |",
                    "|---:|---|---|---|---|",
                ]
            )
            for pull_request in repository["pull_requests"]:
                identity = _identity(pull_request)
                path_classification = (pull_request.get("path_assessment") or {}).get(
                    "classification", "unknown"
                )
                lines.append(
                    f"| #{_escape_markdown(identity.get('number', '?'))} | "
                    f"{_escape_markdown(identity.get('title') or 'Untitled')} | "
                    f"{_escape_markdown(pull_request.get('classification', 'unknown'))} | "
                    f"{_escape_markdown(pull_request.get('checks', 'unknown'))} | "
                    f"{_escape_markdown(path_classification)} |"
                )
        else:
            lines.append("None.")
        lines.extend(["", "### Alerts", ""])
        if repository["alerts"]:
            lines.extend(
                [
                    "| Alert | Dependency | Severity | Classification |",
                    "|---:|---|---|---|",
                ]
            )
            for alert in repository["alerts"]:
                lines.append(
                    f"| #{_escape_markdown(alert.get('number', '?'))} | "
                    f"{_escape_markdown(alert.get('dependency_name') or 'Unknown')} | "
                    f"{_escape_markdown(alert.get('severity') or 'Unknown')} | "
                    f"{_escape_markdown(alert.get('classification'))} |"
                )
        else:
            lines.append("None.")
        lines.extend(["", "### Recent sensitive non-Dependabot merges", ""])
        if repository["recent_sensitive_merges"]:
            lines.extend(
                [
                    "| PR | Title | Merged | Sensitive paths |",
                    "|---:|---|---|---|",
                ]
            )
            for merge in repository["recent_sensitive_merges"]:
                matched_paths = ", ".join(
                    item["path"] for item in merge.get("matched_files", [])
                )
                lines.append(
                    f"| #{_escape_markdown(merge.get('number', '?'))} | "
                    f"{_escape_markdown(merge.get('title') or 'Untitled')} | "
                    f"{_escape_markdown(merge.get('merged_at') or 'Unknown')} | "
                    f"{_escape_markdown(matched_paths or 'Sensitive path')} |"
                )
        else:
            lines.append("None.")
        if repository["truncations"] or repository["errors"]:
            lines.extend(["", "### Collection notes", ""])
            for truncation in repository["truncations"]:
                lines.append(
                    f"- **Truncated:** `{_escape_markdown(truncation['surface'])}` at "
                    f"{truncation['limit']} items."
                )
            for error in repository["errors"]:
                lines.append(
                    f"- **Error ({_escape_markdown(error['surface'])}):** "
                    f"{_escape_markdown(error['message'])}"
                )
    return "\n".join(lines) + "\n"


def render(report: dict[str, Any], output_format: str) -> str:
    renderers = {
        "json": render_json,
        "text": render_text,
        "markdown": render_markdown,
    }
    return renderers[output_format](report)


def render_advisory_text(report: dict[str, Any]) -> str:
    advisory = report["advisory"]
    summary = advisory["summary"]
    inspection_summary = report["inspection"]["summary"]
    lines = [
        "Dependabot Watcher advice",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        "Mutation authorized: no (advice only)",
        f"Mode: {advisory['mode']} | Provider: {advisory['provider'] or 'none'}",
        (
            f"Repositories: {inspection_summary['repository_count']} | "
            f"Open PRs: {inspection_summary['open_pull_request_count']} | "
            f"Open alerts: {inspection_summary['open_alert_count']}"
        ),
        (
            f"Advised: {summary['advised_candidate_count']} | "
            f"Reviewed: {summary['reviewed_candidate_count']} | "
            f"Deferred: {summary['deferred_candidate_count']} | "
            f"Provider calls: {summary['provider_call_count']}"
        ),
    ]
    if advisory["status"] == "disabled":
        lines.append("Model advice is disabled; deterministic inspection remains available.")
    for item in advisory["decisions"]:
        identity = item["identity"]
        number = identity.get("number", "?")
        label = "PR" if item["kind"] == "pull_request" else "Alert"
        effective = item["effective_advice"]
        reviewed = " reviewed" if item.get("review") else ""
        lines.append(
            f"{identity.get('repository')} {label} #{number}: "
            f"{effective['recommended_action']} "
            f"[{effective['risk_level']} risk, {effective['confidence']} confidence{reviewed}]"
        )
        lines.append(f"  {effective['summary']}")
    if advisory["errors"]:
        lines.append("Advisory errors:")
        for error in advisory["errors"]:
            lines.append(f"  - {error['stage']} batch {error['batch']}: {error['message']}")
    if advisory["deferred"]:
        reasons: dict[str, int] = {}
        for item in advisory["deferred"]:
            reason = str(item.get("reason") or "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        lines.append(
            "Deferred: " + ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))
        )
    return "\n".join(lines) + "\n"


def render_advisory_markdown(report: dict[str, Any]) -> str:
    advisory = report["advisory"]
    summary = advisory["summary"]
    lines = [
        "# Dependabot Watcher advice",
        "",
        f"Generated: `{_escape_markdown(report['generated_at'])}`  ",
        f"Status: **{_escape_markdown(report['status'])}**  ",
        "Mutation authorized: **no** (advice only)  ",
        f"Mode: `{_escape_markdown(advisory['mode'])}`  ",
        f"Provider: `{_escape_markdown(advisory['provider'] or 'none')}`",
        "",
        "| Eligible | Advised | Reviewed | Deferred | Provider calls |",
        "|---:|---:|---:|---:|---:|",
        (
            f"| {summary['eligible_candidate_count']} | {summary['advised_candidate_count']} | "
            f"{summary['reviewed_candidate_count']} | {summary['deferred_candidate_count']} | "
            f"{summary['provider_call_count']} |"
        ),
        "",
        "## Decisions",
        "",
    ]
    if not advisory["decisions"]:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| Candidate | Recommendation | Risk | Confidence | Summary |",
                "|---|---|---|---|---|",
            ]
        )
        for item in advisory["decisions"]:
            identity = item["identity"]
            label = "PR" if item["kind"] == "pull_request" else "Alert"
            effective = item["effective_advice"]
            lines.append(
                f"| {_escape_markdown(identity.get('repository'))} {label} "
                f"#{_escape_markdown(identity.get('number', '?'))} | "
                f"{_escape_markdown(effective['recommended_action'])} | "
                f"{_escape_markdown(effective['risk_level'])} | "
                f"{_escape_markdown(effective['confidence'])} | "
                f"{_escape_markdown(effective['summary'])} |"
            )
    if advisory["errors"]:
        lines.extend(["", "## Advisory errors", ""])
        for error in advisory["errors"]:
            lines.append(
                f"- `{_escape_markdown(error['stage'])}` batch {error['batch']}: "
                f"{_escape_markdown(error['message'])}"
            )
    if advisory["deferred"]:
        lines.extend(["", "## Deferred candidates", ""])
        for item in advisory["deferred"]:
            identity = item.get("identity") or {}
            lines.append(
                f"- `{_escape_markdown(item.get('candidate_id'))}` "
                f"({_escape_markdown(identity.get('repository'))} "
                f"#{_escape_markdown(identity.get('number', '?'))}): "
                f"`{_escape_markdown(item.get('reason'))}`"
            )
    return "\n".join(lines) + "\n"


def render_advisory(report: dict[str, Any], output_format: str) -> str:
    renderers = {
        "json": render_json,
        "text": render_advisory_text,
        "markdown": render_advisory_markdown,
    }
    return renderers[output_format](report)


def _plan_step_label(step: dict[str, Any]) -> str:
    identity = step.get("identity") or {}
    if step.get("kind") == "pull_request":
        return f"PR #{identity.get('number', '?')}: {identity.get('title') or 'Untitled'}"
    return (
        f"Alert #{identity.get('number', '?')}: "
        f"{identity.get('dependency_name') or 'unknown dependency'}"
    )


def render_plan_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "Dependabot Watcher maintenance plan",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        f"Plan: {report['plan_id']}",
        (
            f"Repositories: {summary['repository_count']} | "
            f"Recommended steps: {summary['recommended_step_count']} | "
            f"Partial repositories: {summary['partial_repository_count']}"
        ),
        "Mutation authorized: no",
    ]
    for repository in report["repositories"]:
        state = repository.get("state") or {}
        lines.extend(
            [
                "",
                f"{repository['repository']} (base: {repository['expected_base']} "
                f"@ {(repository.get('evidence_snapshot') or {}).get('base_sha') or 'unknown'})",
                (
                    f"  Current state: {state.get('open_pull_request_count', 0)} open PRs, "
                    f"{state.get('open_alert_count', 0)} open alerts, "
                    f"{state.get('unresolved_alert_count', 0)} unresolved alerts"
                ),
                "  Recommended Order",
            ]
        )
        steps = repository.get("recommended_order") or []
        if not steps:
            lines.append("    No actionable Dependabot items were found.")
        for step in steps:
            lines.append(
                f"    {step['order']}. {_plan_step_label(step)} "
                f"[{step['action'].replace('_', ' ')}]"
            )
            lines.append(f"       Why now: {step['priority_reason']}")
            impacts = step.get("expected_alert_impact") or []
            if impacts:
                impact_text = ", ".join(
                    f"alert #{item.get('alert_number')} ({item.get('coverage_evidence')})"
                    for item in impacts
                )
                lines.append(f"       Expected alert impact: {impact_text}")
            for blocker in (step.get("blockers") or [])[:3]:
                lines.append(f"       Blocker: {blocker}")
            lines.append("       Then: re-inspect repository state before continuing.")
        unresolved = repository.get("unresolved_evidence") or {}
        if unresolved.get("errors") or unresolved.get("truncations"):
            lines.append("  Evidence is partial; resolve collection gaps before execution.")
    return "\n".join(lines) + "\n"


def render_plan_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Dependabot Watcher maintenance plan",
        "",
        f"Generated: `{_escape_markdown(report['generated_at'])}`  ",
        f"Status: **{_escape_markdown(report['status'])}**  ",
        f"Plan: `{_escape_markdown(report['plan_id'])}`  ",
        "Mutation authorized: **no**",
        "",
        (
            f"Repositories: **{summary['repository_count']}** · "
            f"Recommended steps: **{summary['recommended_step_count']}** · "
            f"Partial repositories: **{summary['partial_repository_count']}**"
        ),
    ]
    for repository in report["repositories"]:
        state = repository.get("state") or {}
        snapshot = repository.get("evidence_snapshot") or {}
        lines.extend(
            [
                "",
                f"## {_escape_markdown(repository['repository'])}",
                "",
                (
                    f"Base: `{_escape_markdown(repository['expected_base'])}` at "
                    f"`{_escape_markdown(snapshot.get('base_sha') or 'unknown')}`"
                ),
                "",
                (
                    f"Current state: **{state.get('open_pull_request_count', 0)}** open PRs, "
                    f"**{state.get('open_alert_count', 0)}** open alerts, "
                    f"**{state.get('unresolved_alert_count', 0)}** unresolved alerts."
                ),
                "",
                "### Recommended Order",
                "",
            ]
        )
        steps = repository.get("recommended_order") or []
        if not steps:
            lines.append("No actionable Dependabot items were found.")
        for step in steps:
            lines.append(
                f"{step['order']}. **{_escape_markdown(_plan_step_label(step))}** — "
                f"`{_escape_markdown(step['action'])}`"
            )
            lines.append(f"   - Why now: {_escape_markdown(step['priority_reason'])}")
            impacts = step.get("expected_alert_impact") or []
            if impacts:
                impact_text = ", ".join(
                    f"alert #{item.get('alert_number')} ({item.get('coverage_evidence')})"
                    for item in impacts
                )
                lines.append(f"   - Expected alert impact: {_escape_markdown(impact_text)}")
            for blocker in (step.get("blockers") or [])[:3]:
                lines.append(f"   - Blocker: {_escape_markdown(blocker)}")
            lines.append("   - Then: re-inspect repository state before continuing.")
        unresolved = repository.get("unresolved_evidence") or {}
        if unresolved.get("errors") or unresolved.get("truncations"):
            lines.extend(
                [
                    "",
                    "**Evidence is partial; resolve collection gaps before execution.**",
                ]
            )
    return "\n".join(lines) + "\n"


def render_plan(report: dict[str, Any], output_format: str) -> str:
    renderers = {
        "json": render_json,
        "text": render_plan_text,
        "markdown": render_plan_markdown,
    }
    return renderers[output_format](report)
