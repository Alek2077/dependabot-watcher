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
