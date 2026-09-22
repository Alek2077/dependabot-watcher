from __future__ import annotations

import json
from typing import Any


def _json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _escape(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _plain(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")


def _doctor_text(report: dict[str, Any]) -> str:
    configuration = report["configuration"]
    runtime = report["runtime"]
    tools = report["tools"]
    lines = [
        "Dependabot Watcher doctor",
        f"Status: {report['status']}",
        f"Configuration: {configuration['status']} ({configuration['path']})",
        f"Runtime: {runtime['platform']} | Python {runtime['python']}",
        (
            "GitHub CLI: "
            f"{tools['github_cli']['availability']} / auth "
            f"{tools['github_cli']['authentication']}"
        ),
        (
            "Codex CLI: "
            f"{tools['codex_cli']['availability']} / auth "
            f"{tools['codex_cli']['authentication']}"
        ),
    ]
    if configuration.get("error"):
        lines.append(f"Configuration error: {_plain(configuration['error'])}")
    for repository in report["repositories"]:
        ecosystems = ", ".join(repository["ecosystems"]) or "not detected"
        lines.append(
            f"{repository['repository']}: access {repository['access']} | "
            f"configured branch {repository['configured_branch']} "
            f"({repository['configured_branch_access']}) | "
            f"remote default {repository['observed_default_branch'] or 'unknown'} | "
            f"ecosystems {ecosystems}"
        )
    if report["findings"]:
        lines.append("Findings:")
        for finding in report["findings"]:
            lines.append(
                f"  - {finding['severity'].upper()} [{finding['code']}]: "
                f"{finding['message']}"
            )
            if finding.get("next_step"):
                lines.append(f"    Next: {finding['next_step']}")
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
        "Mutation authorized: **no**",
        "",
        "| Tool | Availability | Authentication | Version |",
        "|---|---|---|---|",
    ]
    if configuration.get("error"):
        lines.extend(
            [
                "",
                f"Configuration error: `{_escape(configuration['error'])}`",
            ]
        )
    for label, key in (("GitHub CLI", "github_cli"), ("Codex CLI", "codex_cli")):
        tool = tools[key]
        lines.append(
            f"| {label} | {_escape(tool['availability'])} | "
            f"{_escape(tool['authentication'])} | {_escape(tool.get('version') or 'unknown')} |"
        )
    lines.extend(["", "## Repositories", ""])
    if report["repositories"]:
        lines.extend(
            [
                "| Repository | Access | Configured branch | Remote default | Ecosystems |",
                "|---|---|---|---|---|",
            ]
        )
        for repository in report["repositories"]:
            ecosystems = ", ".join(repository["ecosystems"]) or "not detected"
            lines.append(
                f"| {_escape(repository['repository'])} | {_escape(repository['access'])} | "
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
    lines = [
        "Dependabot Watcher setup",
        f"Status: {report['status']}",
        f"Configuration: {report['configuration_path']}",
        f"Profile: {report['profile_source']}",
        f"Repositories: {len(report['repositories'])}",
        f"Advisory mode: {report['advisory']['mode']}",
        (
            f"Verification: {report['verification']['mode']} | GitHub auth "
            f"{report['verification']['github_authentication']} | provider auth "
            f"{report['verification']['provider_authentication']}"
        ),
        "Mutation authorized: no",
        "Next:",
    ]
    lines.extend(f"  - {command}" for command in report["next_commands"])
    return "\n".join(lines) + "\n"


def _setup_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Dependabot Watcher setup",
        "",
        f"Status: **{_escape(report['status'])}**  ",
        f"Configuration: `{_escape(report['configuration_path'])}`  ",
        f"Profile: `{_escape(report['profile_source'])}`  ",
        f"Advisory mode: `{_escape(report['advisory']['mode'])}`  ",
        (
            f"Verification: `{_escape(report['verification']['mode'])}`; GitHub auth "
            f"`{_escape(report['verification']['github_authentication'])}`; provider auth "
            f"`{_escape(report['verification']['provider_authentication'])}`  "
        ),
        "Mutation authorized: **no**",
        "",
        "## Next commands",
        "",
    ]
    lines.extend(f"- `{_escape(command)}`" for command in report["next_commands"])
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
