from __future__ import annotations

import json
import math
from typing import Any, Callable

from .terminal import sanitize_human_value


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("candidate_identity") or {}


def _validate_verbosity(verbosity: str) -> None:
    if verbosity not in {"concise", "standard", "detailed"}:
        raise ValueError(f"Unsupported verbosity: {verbosity}.")


def _human_state(value: object) -> str:
    return sanitize_human_value(value or "unknown").replace("_", " ")


def _display(value: object, fallback: str = "") -> str:
    return sanitize_human_value(value or fallback)


def _count_phrase(count: object, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _concise_limit(count: int) -> int:
    if count <= 5:
        return count
    return min(10, max(5, math.ceil(count * 0.2)))


def _concise_highlights(
    items: list[dict[str, Any]],
    *,
    priority_key: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    limit = _concise_limit(len(items))
    prioritized = sorted(
        enumerate(items),
        key=lambda item: (not priority_key(item[1]), item[0]),
    )
    return [item for _, item in prioritized[:limit]]


def _selection_label(label: str, shown: int, total: int) -> str:
    if total == 0:
        return f"{label}: none"
    if shown == total:
        return f"{label}: all {total} shown"
    return f"{label} highlights: {shown} of {total} shown; attention items first"


def _inspection_next(report: dict[str, Any]) -> str:
    summary = report["summary"]
    if report["status"] == "partial":
        return "resolve the reported collection gaps, then inspect again."
    if summary["attention_required_count"]:
        return "review the attention-required items, or run dependabot-watcher plan."
    if summary["open_pull_request_count"] or summary["open_alert_count"]:
        return "run dependabot-watcher plan for an ordered action list."
    return "no open Dependabot work was found."


def render_text(report: dict[str, Any], verbosity: str = "standard") -> str:
    _validate_verbosity(verbosity)
    summary = report["summary"]
    lines = [
        "Dependabot Watcher inspection",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        "Evidence source: deterministic GitHub inspection (no model used)",
        (
            f"Repositories: {summary['repository_count']} | "
            f"Open PRs: {summary['open_pull_request_count']} | "
            f"Open alerts: {summary['open_alert_count']} | "
            f"Attention required: {summary['attention_required_count']} | "
            f"Errors: {summary['error_count']} | "
            f"Truncations: {summary['truncation_count']}"
        ),
    ]
    if verbosity == "concise":
        lines.append("More detail: dependabot-watcher inspect --verbosity standard")
    for repository in report["repositories"]:
        repo_summary = repository["summary"]
        lines.extend(
            [
                "",
                f"{sanitize_human_value(repository['repository'])} "
                f"(expected base: {sanitize_human_value(repository['expected_base'])})",
                (
                    f"  PRs: {repo_summary['open_pull_request_count']} | "
                    f"Alerts: {repo_summary['open_alert_count']} | "
                    "Alerts without direct open-PR coverage: "
                    f"{repo_summary['unresolved_alert_count']}"
                ),
            ]
        )
        if verbosity == "concise":
            pull_requests = _concise_highlights(
                repository["pull_requests"],
                priority_key=lambda item: item.get("classification") != "baseline_candidate",
            )
            lines.append(
                "  "
                + _selection_label(
                    "Pull requests", len(pull_requests), len(repository["pull_requests"])
                )
            )
            for pull_request in pull_requests:
                identity = _identity(pull_request)
                lines.append(
                    f"  PR #{identity.get('number', '?')}: "
                    f"{sanitize_human_value(identity.get('title') or 'Untitled')} "
                    f"[{_human_state(pull_request.get('classification'))}]"
                )
            if len(repository["pull_requests"]) > len(pull_requests):
                lines.append(
                    f"  + {len(repository['pull_requests']) - len(pull_requests)} more open PRs; "
                    "use --verbosity standard"
                )
            uncovered_alerts = [
                alert
                for alert in repository["alerts"]
                if alert.get("classification") != "likely_covered_by_open_pr"
            ]
            alert_highlights = _concise_highlights(
                uncovered_alerts,
                priority_key=lambda item: True,
            )
            lines.append(
                "  "
                + _selection_label(
                    "Alerts without direct open-PR coverage",
                    len(alert_highlights),
                    len(uncovered_alerts),
                )
            )
            for alert in alert_highlights:
                lines.append(
                    f"  Alert #{alert.get('number', '?')}: "
                    f"{sanitize_human_value(alert.get('dependency_name') or 'unknown package')} "
                    f"({sanitize_human_value(alert.get('severity') or 'unknown severity')}, "
                    f"{_human_state(alert.get('classification'))})"
                )
            if len(uncovered_alerts) > len(alert_highlights):
                lines.append(
                    f"  + {len(uncovered_alerts) - len(alert_highlights)} more alerts without direct "
                    "open-PR coverage; use --verbosity standard"
                )
            for truncation in repository["truncations"]:
                lines.append(
                    f"  TRUNCATED: {sanitize_human_value(truncation['surface'])} "
                    f"at {truncation['limit']} items"
                )
            for error in repository["errors"]:
                lines.append(
                    f"  ERROR [{sanitize_human_value(error['surface'])}]: "
                    f"{sanitize_human_value(error['message'])}"
                )
            continue
        if not repository["pull_requests"]:
            lines.append("  Pull requests: none")
        for pull_request in repository["pull_requests"]:
            identity = _identity(pull_request)
            lines.append(
                f"  PR #{identity.get('number', '?')}: "
                f"{sanitize_human_value(identity.get('title') or 'Untitled')} "
                f"[{_human_state(pull_request.get('classification'))}]"
            )
            for reason in pull_request.get("reasons", []):
                lines.append(f"    - {sanitize_human_value(reason)}")
            if verbosity == "detailed":
                lines.append(
                    f"    checks={pull_request.get('checks', 'unknown')} | "
                    f"paths={(pull_request.get('path_assessment') or {}).get('classification', 'unknown')} | "
                    f"url={sanitize_human_value(identity.get('url') or 'unavailable')}"
                )
        for alert in repository["alerts"]:
            lines.append(
                f"  Alert #{alert.get('number', '?')}: "
                f"{sanitize_human_value(alert.get('dependency_name') or 'unknown package')} "
                f"({sanitize_human_value(alert.get('severity') or 'unknown severity')}, "
                f"{_human_state(alert.get('classification'))})"
            )
            if verbosity == "detailed":
                lines.append(
                    "    url="
                    + sanitize_human_value(
                        alert.get("url") or alert.get("html_url") or "unavailable"
                    )
                )
        if repository["recent_sensitive_merges"]:
            lines.append("  Recent sensitive non-Dependabot merges:")
            for merge in repository["recent_sensitive_merges"]:
                matched_paths = ", ".join(
                    sanitize_human_value(item["path"])
                    for item in merge.get("matched_files", [])
                )
                lines.append(
                    f"    - PR #{merge.get('number', '?')}: "
                    f"{sanitize_human_value(merge.get('title') or 'Untitled')} "
                    f"[{matched_paths or 'sensitive path'}]"
                )
        for truncation in repository["truncations"]:
            lines.append(
                f"  TRUNCATED: {sanitize_human_value(truncation['surface'])} "
                f"at {truncation['limit']} items"
            )
        for error in repository["errors"]:
            lines.append(
                f"  ERROR [{sanitize_human_value(error['surface'])}]: "
                f"{sanitize_human_value(error['message'])}"
            )
    lines.append("")
    lines.append(f"Next: {_inspection_next(report)}")
    return "\n".join(lines) + "\n"


def _escape_markdown(value: object) -> str:
    return sanitize_human_value(value).replace("|", "\\|")


def render_markdown(report: dict[str, Any], verbosity: str = "standard") -> str:
    _validate_verbosity(verbosity)
    summary = report["summary"]
    lines = [
        "# Dependabot Watcher inspection",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Status: **{report['status']}**",
        "Evidence source: deterministic GitHub inspection (no model used).",
        "",
        "| Repositories | Open PRs | Open alerts | Attention required | Errors | Truncations |",
        "|---:|---:|---:|---:|---:|---:|",
        (
            f"| {summary['repository_count']} | {summary['open_pull_request_count']} | "
            f"{summary['open_alert_count']} | {summary['attention_required_count']} | "
            f"{summary['error_count']} | {summary['truncation_count']} |"
        ),
    ]
    if verbosity == "concise":
        lines.extend(
            [
                "",
                "Use `dependabot-watcher inspect --verbosity standard` for item-level details.",
            ]
        )
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
        if verbosity == "concise":
            repo_summary = repository["summary"]
            lines.extend(
                [
                    (
                        "Current state: "
                        f"**{_count_phrase(repo_summary['open_pull_request_count'], 'open PR')}**, "
                        f"**{_count_phrase(repo_summary['open_alert_count'], 'open alert')}**, "
                        f"**{repo_summary['unresolved_alert_count']}** without direct "
                        "open-PR coverage."
                    ),
                    "",
                ]
            )
            pull_requests = _concise_highlights(
                repository["pull_requests"],
                priority_key=lambda item: item.get("classification") != "baseline_candidate",
            )
            lines.append(
                _selection_label(
                    "Pull requests", len(pull_requests), len(repository["pull_requests"])
                )
            )
            lines.append("")
            if pull_requests:
                for pull_request in pull_requests:
                    identity = _identity(pull_request)
                    lines.append(
                        f"- PR #{_escape_markdown(identity.get('number', '?'))}: "
                        f"{_escape_markdown(identity.get('title') or 'Untitled')} "
                        f"(`{_escape_markdown(_human_state(pull_request.get('classification')))}`)"
                    )
                if len(repository["pull_requests"]) > len(pull_requests):
                    lines.append(
                        f"- {len(repository['pull_requests']) - len(pull_requests)} more open PRs; "
                        "use `--verbosity standard`."
                    )
            else:
                lines.append("None.")

            uncovered_alerts = [
                alert
                for alert in repository["alerts"]
                if alert.get("classification") != "likely_covered_by_open_pr"
            ]
            lines.extend(["", "### Alerts without direct open-PR coverage", ""])
            alert_highlights = _concise_highlights(
                uncovered_alerts,
                priority_key=lambda item: True,
            )
            lines.extend(
                [
                    _selection_label(
                        "Alerts without direct open-PR coverage",
                        len(alert_highlights),
                        len(uncovered_alerts),
                    ),
                    "",
                ]
            )
            if alert_highlights:
                for alert in alert_highlights:
                    lines.append(
                        f"- Alert #{_escape_markdown(alert.get('number', '?'))}: "
                        f"{_escape_markdown(alert.get('dependency_name') or 'Unknown')} "
                        f"({_escape_markdown(alert.get('severity') or 'Unknown')}, "
                        f"{_escape_markdown(_human_state(alert.get('classification')))})"
                    )
                if len(uncovered_alerts) > len(alert_highlights):
                    lines.append(
                        f"- {len(uncovered_alerts) - len(alert_highlights)} more uncovered alerts; "
                        "use `--verbosity standard`."
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
            continue
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
                if verbosity == "detailed":
                    reasons = "; ".join(pull_request.get("reasons") or []) or "None"
                    lines.append(
                        f"  - PR #{_escape_markdown(identity.get('number', '?'))} evidence: "
                        f"{_escape_markdown(reasons)}"
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
    lines.extend(["", f"**Next:** {_escape_markdown(_inspection_next(report))}"])
    return "\n".join(lines) + "\n"


def render(report: dict[str, Any], output_format: str, *, verbosity: str = "standard") -> str:
    if output_format == "json":
        return render_json(report)
    if output_format == "text":
        return render_text(report, verbosity)
    return render_markdown(report, verbosity)


def render_advisory_text(report: dict[str, Any], verbosity: str = "standard") -> str:
    _validate_verbosity(verbosity)
    advisory = report["advisory"]
    summary = advisory["summary"]
    inspection_summary = report["inspection"]["summary"]
    lines = [
        "Dependabot Watcher advice",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        "Mutation authorized: no (advice only)",
        f"Mode: {_display(advisory['mode'])} | "
        f"Provider: {_display(advisory['provider'], 'none')}",
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
            f"{_display(identity.get('repository'))} {label} #{number}: "
            f"{_display(effective['recommended_action'])} "
            f"[{_display(effective['risk_level'])} risk, "
            f"{_display(effective['confidence'])} confidence{reviewed}]"
        )
        if verbosity != "concise":
            lines.append(f"  {_display(effective['summary'])}")
        if verbosity == "detailed":
            lines.append(f"  Rationale: {_display(effective['rationale'])}")
            for evidence in effective.get("evidence") or []:
                lines.append(f"  Evidence: {_display(evidence)}")
            for question in effective.get("unresolved_questions") or []:
                lines.append(f"  Unresolved: {_display(question)}")
    if advisory["errors"]:
        lines.append("Advisory errors:")
        for error in advisory["errors"]:
            lines.append(
                f"  - {_display(error['stage'])} batch {error['batch']}: "
                f"{_display(error['message'])}"
            )
    if advisory["deferred"]:
        reasons: dict[str, int] = {}
        for item in advisory["deferred"]:
            reason = _display(item.get("reason"), "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        lines.append(
            "Deferred: " + ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))
        )
    return "\n".join(lines) + "\n"


def render_advisory_markdown(report: dict[str, Any], verbosity: str = "standard") -> str:
    _validate_verbosity(verbosity)
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
            if verbosity == "detailed":
                lines.append(
                    f"  - Rationale for `{_escape_markdown(item['candidate_id'])}`: "
                    f"{_escape_markdown(effective['rationale'])}"
                )
    if verbosity == "concise" and advisory["decisions"]:
        lines = [line for line in lines if not line.startswith("  - Rationale for `")]
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


def render_advisory(
    report: dict[str, Any], output_format: str, *, verbosity: str = "standard"
) -> str:
    if output_format == "json":
        return render_json(report)
    if output_format == "text":
        return render_advisory_text(report, verbosity)
    return render_advisory_markdown(report, verbosity)


def _plan_step_label(step: dict[str, Any]) -> str:
    identity = step.get("identity") or {}
    if step.get("kind") == "pull_request":
        return (
            f"PR #{identity.get('number', '?')}: "
            f"{_display(identity.get('title'), 'Untitled')}"
        )
    return (
        f"Alert #{identity.get('number', '?')}: "
        f"{_display(identity.get('dependency_name'), 'unknown dependency')}"
    )


def _plan_record_label(record: object) -> str:
    if not isinstance(record, dict):
        return _display(record)
    return _display(
        record.get("description")
        or record.get("name")
        or record.get("step_id")
        or record.get("conflicting_step_id")
        or record.get("type")
        or "structured requirement"
    )


def _plan_step_short_label(step: dict[str, Any] | None) -> str:
    if not step:
        return "another plan step"
    identity = step.get("identity") or {}
    if step.get("kind") == "pull_request":
        return f"PR #{identity.get('number', '?')}"
    return f"alert #{identity.get('number', '?')}"


def _plan_conflicts_label(
    records: list[dict[str, Any]],
    steps_by_id: dict[str, dict[str, Any]],
) -> str:
    targets: list[str] = []
    shared_paths: set[str] = set()
    shared_alerts: set[int] = set()
    relationship_types: set[str] = set()
    for record in records:
        other_id = record.get("step_id") or record.get("conflicting_step_id")
        targets.append(_plan_step_short_label(steps_by_id.get(str(other_id))))
        shared_paths.update(_display(path) for path in record.get("shared_paths") or [])
        shared_alerts.update(
            number
            for number in record.get("shared_alerts") or []
            if isinstance(number, int)
        )
        relationship_type = _human_state(record.get("type"))
        if relationship_type != "unknown":
            relationship_types.add(relationship_type)

    reasons: list[str] = []
    if shared_paths:
        sorted_paths = sorted(shared_paths)
        shown_paths = ", ".join(sorted_paths[:3])
        remainder = len(sorted_paths) - min(len(sorted_paths), 3)
        if remainder:
            shown_paths += f" (+{remainder} more)"
        reasons.append(f"shared dependency paths: {shown_paths}")
    if shared_alerts:
        reasons.append(
            "shared expected alerts: "
            + ", ".join(f"#{number}" for number in sorted(shared_alerts)[:5])
        )
    relationship = (
        "; ".join(reasons)
        or ", ".join(sorted(relationship_types))
        or "related plan evidence"
    )
    verb = "overlaps" if len(targets) == 1 else "overlap"
    return (
        f"{', '.join(targets)} {verb} on {relationship}. "
        "Handling any one requires a fresh plan."
    )


def _plan_source_label(report: dict[str, Any]) -> str:
    source = report.get("source") or {}
    advisory = source.get("advisory") or {}
    mode = _display(advisory.get("mode"), "off")
    provider_key = _display(advisory.get("provider"))
    provider = {"codex_cli": "Codex CLI"}.get(
        provider_key, provider_key.replace("_", " ")
    )
    model_calls = bool((source.get("capabilities") or {}).get("model_calls"))
    decisions = advisory.get("decisions") or []
    errors = advisory.get("errors") or []
    if mode == "off" or not provider:
        return "deterministic policy (no model used)"
    if decisions:
        label = (
            f"deterministic policy informed by accepted optional {provider} advice "
            f"({mode} mode)"
        )
        if errors:
            return f"{label}; some provider analysis failed"
        return label
    if errors:
        return (
            f"deterministic policy only; {provider} analysis failed and no model advice "
            "was accepted"
        )
    if model_calls:
        return f"deterministic policy only; no accepted {provider} advice was available"
    return f"deterministic policy; {provider} advice was configured but no model call was needed"


def render_plan_text(report: dict[str, Any], verbosity: str = "concise") -> str:
    _validate_verbosity(verbosity)
    summary = report["summary"]
    lines = [
        "Dependabot Watcher maintenance plan",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        f"Plan: {report['plan_id']}",
        f"Decision source: {_plan_source_label(report)}",
        (
            f"Repositories: {summary['repository_count']} | "
            f"Recommended steps: {summary['recommended_step_count']} | "
            f"Partial repositories: {summary['partial_repository_count']}"
        ),
        "Mutation authorized: no",
        (
            "Execution safety: a separately enabled execute call recollects live state and "
            "enforces the exact PR head, current alert evidence, and configured required checks."
        ),
    ]
    for repository in report["repositories"]:
        state = repository.get("state") or {}
        lines.extend(
            [
                "",
                f"{_display(repository['repository'])} "
                f"(base: {_display(repository['expected_base'])} @ "
                f"{_display((repository.get('evidence_snapshot') or {}).get('base_sha'), 'unknown')})",
                (
                    "  Current state: "
                    f"{_count_phrase(state.get('open_pull_request_count', 0), 'open PR')}, "
                    f"{_count_phrase(state.get('open_alert_count', 0), 'open alert')}, "
                    f"{state.get('unresolved_alert_count', 0)} without direct open-PR coverage"
                ),
                "  Recommended Order",
            ]
        )
        steps = repository.get("recommended_order") or []
        steps_by_id = {
            str(step.get("step_id")): step
            for step in steps
            if step.get("step_id")
        }
        if not steps:
            lines.append("    No actionable Dependabot items were found.")
        for step in steps:
            lines.append(
                f"    {step['order']}. {_plan_step_label(step)} "
                f"[{step['action'].replace('_', ' ')}]"
            )
            lines.append(f"       Why now: {_display(step['priority_reason'])}")
            impacts = step.get("expected_alert_impact") or []
            if impacts:
                impact_text = ", ".join(
                    f"alert #{item.get('alert_number')} "
                    f"({_display(item.get('coverage_evidence'))})"
                    for item in impacts
                )
                lines.append(f"       Expected alert impact: {impact_text}")
            for blocker in (step.get("blockers") or [])[:3]:
                lines.append(f"       Blocker: {_display(blocker)}")
            if verbosity in {"standard", "detailed"}:
                for prerequisite in step.get("prerequisites") or []:
                    lines.append(f"       Prerequisite: {_plan_record_label(prerequisite)}")
                conflicts = step.get("conflicts") or []
                if conflicts:
                    lines.append(
                        "       Replan dependencies: "
                        f"{_plan_conflicts_label(conflicts, steps_by_id)}"
                    )
            if verbosity == "detailed":
                freshness = step.get("freshness") or {}
                lines.append(
                    f"       Freshness: "
                    f"base={_display(freshness.get('base_sha'), 'unknown')} | "
                    f"head={_display(freshness.get('head_sha'), 'not applicable')}"
                )
            lines.append("       Then: re-inspect repository state before continuing.")
        unresolved = repository.get("unresolved_evidence") or {}
        if unresolved.get("errors") or unresolved.get("truncations"):
            lines.append("  Evidence is partial; resolve collection gaps before execution.")
    lines.append("")
    if report["status"] == "partial":
        lines.append("Next: resolve partial evidence and generate a fresh plan.")
    elif summary["recommended_step_count"]:
        lines.append(
            "Next: validate the first recommended step; this plan does not authorize mutation."
        )
    else:
        lines.append("Next: no actionable Dependabot items were found.")
    return "\n".join(lines) + "\n"


def render_plan_markdown(report: dict[str, Any], verbosity: str = "concise") -> str:
    _validate_verbosity(verbosity)
    summary = report["summary"]
    lines = [
        "# Dependabot Watcher maintenance plan",
        "",
        f"Generated: `{_escape_markdown(report['generated_at'])}`  ",
        f"Status: **{_escape_markdown(report['status'])}**  ",
        f"Plan: `{_escape_markdown(report['plan_id'])}`  ",
        f"Decision source: {_escape_markdown(_plan_source_label(report))}  ",
        "Mutation authorized: **no**",
        (
            "Execution safety: a separately enabled `execute` call recollects live state and "
            "enforces the exact PR head, current alert evidence, and configured required checks."
        ),
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
                    "Current state: "
                    f"**{_count_phrase(state.get('open_pull_request_count', 0), 'open PR')}**, "
                    f"**{_count_phrase(state.get('open_alert_count', 0), 'open alert')}**, "
                    f"**{state.get('unresolved_alert_count', 0)}** without direct open-PR coverage."
                ),
                "",
                "### Recommended Order",
                "",
            ]
        )
        steps = repository.get("recommended_order") or []
        steps_by_id = {
            str(step.get("step_id")): step
            for step in steps
            if step.get("step_id")
        }
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
            if verbosity in {"standard", "detailed"}:
                for prerequisite in step.get("prerequisites") or []:
                    lines.append(
                        f"   - Prerequisite: {_escape_markdown(_plan_record_label(prerequisite))}"
                    )
                conflicts = step.get("conflicts") or []
                if conflicts:
                    lines.append(
                        "   - Replan dependencies: "
                        f"{_escape_markdown(_plan_conflicts_label(conflicts, steps_by_id))}"
                    )
            if verbosity == "detailed":
                freshness = step.get("freshness") or {}
                lines.append(
                    f"   - Freshness: base `{_escape_markdown(freshness.get('base_sha') or 'unknown')}`, "
                    f"head `{_escape_markdown(freshness.get('head_sha') or 'not applicable')}`."
                )
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


def render_plan(
    report: dict[str, Any], output_format: str, *, verbosity: str = "concise"
) -> str:
    if output_format == "json":
        return render_json(report)
    if output_format == "text":
        return render_plan_text(report, verbosity)
    return render_plan_markdown(report, verbosity)


def render_execution(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return render_json(report)
    binding = report.get("binding") or {}
    decision = report.get("decision") or {}
    heading = (
        "Dependabot Watcher execution preflight"
        if report.get("mode") == "execute_preflight"
        else "Dependabot Watcher execution"
    )
    lines = [
        heading,
        f"Status: {report.get('status')}",
        (
            f"Target: {_display(binding.get('repository'))} "
            f"PR #{binding.get('pull_request')} @ {_display(binding.get('head_sha'))}"
        ),
        f"Update: {_display(decision.get('title'), 'unknown')}",
        f"Base: {_display(binding.get('expected_base'))}",
        f"Merge method: {_display(binding.get('merge_method'))}",
        f"Classification: {_display(decision.get('classification'), 'unknown')}",
        "Decision source: deterministic policy (no model used)",
        f"Action: {report.get('action_id')}",
        f"Mutation performed: {'yes' if report.get('mutation_performed') else 'no'}",
    ]
    required_checks = decision.get("required_checks") or []
    if required_checks:
        checks = ", ".join(
            f"{_display(item.get('name'))} ({_display(item.get('state'))})"
            for item in required_checks
        )
        lines.append(f"Required checks: {checks}")
    result = report.get("result") or {}
    if result:
        result_text = _display(result.get("message") or report.get("status"))
        if result.get("merge_commit_sha"):
            result_text += f" @ {result['merge_commit_sha']}"
        lines.append(f"Result: {result_text}")
    post_action = report.get("post_action_state") or {}
    if post_action:
        lines.append(f"Post-action reinspection: {post_action.get('status')}")
        if post_action.get("status") != "unavailable":
            lines.append(
                "Remaining: "
                f"{_count_phrase(post_action.get('open_pull_requests'), 'open PR')} | "
                f"{_count_phrase(post_action.get('open_alerts'), 'open alert')} | "
                f"{post_action.get('alerts_without_direct_open_pr_coverage')} "
                "without direct open-PR coverage | "
                f"{post_action.get('attention_required')} attention required | "
                f"{_count_phrase(post_action.get('recommended_steps'), 'recommended step')}"
            )
        if post_action.get("reason"):
            lines.append(f"Post-action note: {_display(post_action['reason'])}")
        if post_action.get("next"):
            lines.append(f"Next: {_display(post_action['next'])}")
    if report.get("approval_token"):
        lines.append(f"Approval confirmation token: {report['approval_token']}")
    if report.get("next"):
        lines.append(f"Next: {_display(report['next'])}")
    if report.get("audit_path"):
        lines.append(f"Audit: {_display(report['audit_path'])}")
    for item in report.get("recovery") or []:
        lines.append(f"Next: {_display(item)}")
    if output_format == "text":
        return "\n".join(lines) + "\n"
    markdown = [f"# {heading}", ""]
    markdown.extend(f"- {_escape_markdown(line)}" for line in lines[1:])
    return "\n".join(markdown) + "\n"
