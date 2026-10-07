from __future__ import annotations

import json
from typing import Any

from .terminal import sanitize_human_value


def _json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _escape(value: object) -> str:
    return sanitize_human_value(value).replace("|", "\\|")


def _plain(value: object) -> str:
    return sanitize_human_value(value)


def _availability(value: object) -> str:
    return {
        "available": "available",
        "unavailable": "unavailable",
        "unknown": "unknown",
    }.get(str(value), _plain(value))


def _authentication(value: object) -> str:
    return {
        "available": "authenticated",
        "unavailable": "not authenticated",
        "unknown": "not confirmed",
        "not_required": "not checked (advice is off)",
        "skipped": "skipped",
    }.get(str(value), _plain(value))


def _setup_ai_lines(advisory: dict[str, Any], supervisor: dict[str, Any] | None = None) -> list[str]:
    if supervisor and supervisor.get("mode") != "off":
        lines = [
            f"Agent: {_plain(supervisor.get('model'))} ({_plain(supervisor.get('reasoning_effort'))} reasoning) via installed Codex CLI",
            f"Agent access: {_plain(supervisor.get('access'))}; account: {_plain(supervisor.get('expected_account_type'))}",
            "Private repository evidence and saved questions will be sent to the selected provider; runs consume subscription/API usage.",
            f"Workload: up to {_plain(supervisor.get('max_jobs_per_run'))} jobs per run, {_plain(supervisor.get('timeout_seconds'))} seconds per investigation; not a spending cap.",
            "Saved choices only: workspace/runtime checks and scheduled operation have not been verified. No agent or merge was launched.",
            "Metadata advice: off (no separate advice budget)",
        ]
        if supervisor.get("permission_profile"):
            lines.insert(2, f"Restricted permission profile: {_plain(supervisor['permission_profile'])}")
        if supervisor.get("access") == "full":
            lines.append("Full-access tools may bypass Watcher's guarded executor; instructions alone do not isolate mutation credentials.")
        lines.append("Workspace tests/builds: " + ("authorized for later investigation" if supervisor.get("validation_authorized") else "not authorized"))
        return lines
    mode = str(advisory.get("mode") or "off")
    if mode == "off":
        return ["AI: off (no model calls and no provider quota or cost)"]
    provider = _plain(advisory.get("provider") or "unknown provider")
    lines = [
        f"AI: {mode} via {provider}; recurring runs may consume provider quota or incur cost"
    ]
    lines.append(
        "Private evidence: selected repository, PR, alert, dependency, path, check, "
        "and deterministic assessment evidence may be sent to the provider"
    )
    for label in ("triage", "review"):
        stage = advisory.get(label)
        if isinstance(stage, dict):
            lines.append(
                f"{label.capitalize()} model: {_plain(stage.get('model'))} "
                f"({_plain(stage.get('reasoning_effort'))} reasoning)"
            )
    budget = advisory.get("budget")
    if isinstance(budget, dict):
        call_limit = (
            f"up to {_plain(budget.get('max_triage_calls', 0))} triage calls"
        )
        if mode == "reviewed":
            call_limit += (
                f" and {_plain(budget.get('max_review_calls', 0))} review calls"
            )
        lines.append(
            f"Model workload limit per changed run: {call_limit}; "
            "this is not a financial spending cap"
        )
    return lines


def _doctor_text(report: dict[str, Any]) -> str:
    configuration = report["configuration"]
    runtime = report["runtime"]
    tools = report["tools"]
    lines = [
        "Dependabot Watcher doctor",
        f"Status: {_plain(report['status'])}",
        f"Configuration: {_plain(configuration['status'])} "
        f"({_plain(configuration['path'])})",
        f"Runtime: {_plain(runtime['platform'])} | Python {_plain(runtime['python'])}",
        (
            "Execution: "
            + (
                "approval-gated capability enabled; every action still requires an exact confirmation token"
                if (configuration.get("operation") or {}).get("mutation_enabled")
                else "read-only"
            )
        ),
        (
            "GitHub CLI: "
            f"{_availability(tools['github_cli']['availability'])} / "
            f"{_authentication(tools['github_cli']['authentication'])}"
        ),
        (
            "Codex CLI (optional advice): "
            f"{_availability(tools['codex_cli']['availability'])} / "
            f"{_authentication(tools['codex_cli']['authentication'])}"
        ),
    ]
    if report.get("state", {}).get("path"):
        lines.append(f"Audit state: {_plain(report['state']['path'])}")
    if configuration.get("error"):
        lines.append(f"Configuration error: {_plain(configuration['error'])}")
    for repository in report["repositories"]:
        ecosystems = ", ".join(repository["ecosystems"]) or "not detected"
        lines.append(
            f"{_plain(repository['repository'])}: access {_plain(repository['access'])} | "
            f"alerts {_plain(repository['dependabot_alert_access'])} | "
            f"configured branch {_plain(repository['configured_branch'])} "
            f"({_plain(repository['configured_branch_access'])}) | "
            f"remote default {_plain(repository['observed_default_branch'] or 'unknown')} | "
            f"ecosystems {_plain(ecosystems)}"
        )
    if report["findings"]:
        lines.append("Findings:")
        for finding in report["findings"]:
            lines.append(
                f"  - {_plain(finding['severity']).upper()} "
                f"[{_plain(finding['code'])}]: {_plain(finding['message'])}"
            )
            if finding.get("next_step"):
                lines.append(f"    Next: {_plain(finding['next_step'])}")
    else:
        lines.append("Findings: none")
    lines.append("Mutation authorized: no")
    return "\n".join(lines) + "\n"


def _doctor_markdown(report: dict[str, Any]) -> str:
    configuration = report["configuration"]
    tools = report["tools"]
    lines = [
        "# Dependabot Watcher doctor",
        "",
        f"Status: **{_escape(report['status'])}**  ",
        f"Configuration: `{_escape(configuration['status'])}` at `{_escape(configuration['path'])}`  ",
        (
            "Execution: **approval-gated capability enabled**; every action still requires an exact confirmation token  "
            if (configuration.get("operation") or {}).get("mutation_enabled")
            else "Execution: **read-only**  "
        ),
        "Mutation authorized: **no**",
        "",
        "| Tool | Availability | Authentication | Version |",
        "|---|---|---|---|",
    ]
    if report.get("state", {}).get("path"):
        lines.insert(7, f"Audit state: `{_escape(report['state']['path'])}`  ")
    if configuration.get("error"):
        lines.extend(
            [
                "",
                f"Configuration error: `{_escape(configuration['error'])}`",
            ]
        )
    for label, key in (
        ("GitHub CLI", "github_cli"),
        ("Codex CLI (optional advice)", "codex_cli"),
    ):
        tool = tools[key]
        lines.append(
            f"| {label} | {_escape(_availability(tool['availability']))} | "
            f"{_escape(_authentication(tool['authentication']))} | "
            f"{_escape(tool.get('version') or 'unknown')} |"
        )
    lines.extend(["", "## Repositories", ""])
    if report["repositories"]:
        lines.extend(
            [
                "| Repository | Access | Alert access | Configured branch | Remote default | Ecosystems |",
                "|---|---|---|---|---|---|",
            ]
        )
        for repository in report["repositories"]:
            ecosystems = ", ".join(repository["ecosystems"]) or "not detected"
            lines.append(
                f"| {_escape(repository['repository'])} | {_escape(repository['access'])} | "
                f"{_escape(repository['dependabot_alert_access'])} | "
                f"{_escape(repository['configured_branch'])} "
                f"({_escape(repository['configured_branch_access'])}) | "
                f"{_escape(repository['observed_default_branch'] or 'unknown')} | "
                f"{_escape(ecosystems)} |"
            )
    else:
        lines.append("None configured.")
    lines.extend(["", "## Findings", ""])
    if report["findings"]:
        for finding in report["findings"]:
            lines.append(
                f"- **{_escape(finding['severity'])}** `{_escape(finding['code'])}`: "
                f"{_escape(finding['message'])}"
            )
            if finding.get("next_step"):
                lines.append(f"  - Next: {_escape(finding['next_step'])}")
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _setup_text(report: dict[str, Any]) -> str:
    configuration_suffix = (
        " (loaded automatically by later commands)"
        if report.get("configuration_auto_loaded")
        else ""
    )
    verification_mode = (
        "offline; GitHub access was not verified"
        if report["verification"]["mode"] == "offline"
        else "read-only repository access verified; run doctor for alert readiness"
    )
    lines = [
        "Dependabot Watcher setup",
        f"Status: {_plain(report['status'])}",
        f"Configuration: {_plain(report['configuration_path'])}{configuration_suffix}",
        f"Profile: {_plain(report['profile_source'])}",
        f"Repositories: {len(report['repositories'])}",
        f"Autonomy: {_plain(report['operation']['autonomy'])}",
        (
            "GitHub changes: approval-gated only"
            if report["operation"].get("mutation_enabled")
            else "GitHub changes: disabled"
        ),
        (
            f"Verification: {verification_mode} | GitHub "
            f"{_authentication(report['verification']['github_authentication'])} | provider "
            f"{_authentication(report['verification']['provider_authentication'])}"
        ),
        "Mutation authorized: no",
        "Configured repositories:",
    ]
    lines.extend(
        f"  - {_plain(repository['name'])} ({_plain(repository['default_branch'])})"
        for repository in report["repositories"]
    )
    lines.extend(_setup_ai_lines(report["advisory"], report.get("supervisor")))
    lines.append("Next commands:")
    lines.extend(f"  - {_plain(command)}" for command in report["next_commands"])
    lines.append("Before recurring operation:")
    lines.extend(f"  - {_plain(item)}" for item in report["next_decisions"])
    return "\n".join(lines) + "\n"


def _setup_markdown(report: dict[str, Any]) -> str:
    configuration_suffix = (
        " (loaded automatically by later commands)"
        if report.get("configuration_auto_loaded")
        else ""
    )
    verification_mode = (
        "offline; GitHub access was not verified"
        if report["verification"]["mode"] == "offline"
        else "read-only repository access verified; run doctor for alert readiness"
    )
    lines = [
        "# Dependabot Watcher setup",
        "",
        f"Status: **{_escape(report['status'])}**  ",
        f"Configuration: `{_escape(report['configuration_path'])}`{configuration_suffix}  ",
        f"Profile: `{_escape(report['profile_source'])}`  ",
        f"Autonomy: `{_escape(report['operation']['autonomy'])}`  ",
        (
            "GitHub changes: **approval-gated only**  "
            if report["operation"].get("mutation_enabled")
            else "GitHub changes: **disabled**  "
        ),
        (
            f"Verification: {_escape(verification_mode)}; GitHub "
            f"{_escape(_authentication(report['verification']['github_authentication']))}; provider "
            f"{_escape(_authentication(report['verification']['provider_authentication']))}  "
        ),
        "Mutation authorized: **no**",
        "",
        "## Configured repositories",
        "",
    ]
    lines.extend(
        f"- `{_escape(repository['name'])}` (`{_escape(repository['default_branch'])}`)"
        for repository in report["repositories"]
    )
    lines.extend(["", "## AI", ""])
    lines.extend(f"- {_escape(item)}" for item in _setup_ai_lines(report["advisory"], report.get("supervisor")))
    lines.extend(
        [
            "",
            "## Next commands",
            "",
        ]
    )
    lines.extend(f"- `{_escape(command)}`" for command in report["next_commands"])
    lines.extend(["", "## Before recurring operation", ""])
    lines.extend(f"- {_escape(item)}" for item in report["next_decisions"])
    return "\n".join(lines) + "\n"


def render_operational(report: dict[str, Any], output_format: str) -> str:
    mode = report.get("mode")
    if mode == "doctor":
        renderers = {"json": _json, "text": _doctor_text, "markdown": _doctor_markdown}
    elif mode == "setup":
        renderers = {"json": _json, "text": _setup_text, "markdown": _setup_markdown}
    else:
        raise ValueError(f"Unsupported operational report mode: {mode}.")
    return renderers[output_format](report)
