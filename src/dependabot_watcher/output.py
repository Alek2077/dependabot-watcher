from __future__ import annotations

import json
from typing import Any


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("candidate_identity") or {}


def _validate_verbosity(verbosity: str) -> None:
    if verbosity not in {"concise", "standard", "detailed"}:
        raise ValueError(f"Unsupported verbosity: {verbosity}.")


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
            f"Attention required: {summary['attention_required_count']}"
        ),
    ]
    if verbosity == "concise":
        lines.append("More detail: dependabot-watcher inspect --verbosity standard")
    for repository in report["repositories"]:
        repo_summary = repository["summary"]
        lines.extend(
            [
                "",
                f"{repository['repository']} (expected base: {repository['expected_base']})",
                (
                    f"  PRs: {repo_summary['open_pull_request_count']} | "
                    f"Alerts: {repo_summary['open_alert_count']} | "
                    "Alerts without direct open-PR coverage: "
                    f"{repo_summary['unresolved_alert_count']}"
                ),
            ]
        )
        if verbosity == "concise":
            for truncation in repository["truncations"]:
                lines.append(
                    f"  TRUNCATED: {truncation['surface']} at {truncation['limit']} items"
                )
            for error in repository["errors"]:
                lines.append(f"  ERROR [{error['surface']}]: {error['message']}")
            continue
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
            if verbosity == "detailed":
                lines.append(
                    f"    checks={pull_request.get('checks', 'unknown')} | "
                    f"paths={(pull_request.get('path_assessment') or {}).get('classification', 'unknown')} | "
                    f"url={identity.get('url') or 'unavailable'}"
                )
        for alert in repository["alerts"]:
            lines.append(
                f"  Alert #{alert.get('number', '?')}: {alert.get('dependency_name') or 'unknown package'} "
                f"({alert.get('severity') or 'unknown severity'}, {alert.get('classification')})"
            )
            if verbosity == "detailed":
                lines.append(f"    url={alert.get('url') or alert.get('html_url') or 'unavailable'}")
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
        if verbosity != "concise":
            lines.append(f"  {effective['summary']}")
        if verbosity == "detailed":
            lines.append(f"  Rationale: {effective['rationale']}")
            for evidence in effective.get("evidence") or []:
                lines.append(f"  Evidence: {evidence}")
            for question in effective.get("unresolved_questions") or []:
                lines.append(f"  Unresolved: {question}")
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
        return f"PR #{identity.get('number', '?')}: {identity.get('title') or 'Untitled'}"
    return (
        f"Alert #{identity.get('number', '?')}: "
        f"{identity.get('dependency_name') or 'unknown dependency'}"
    )


def _plan_record_label(record: object) -> str:
    if not isinstance(record, dict):
        return str(record)
    return str(
        record.get("description")
        or record.get("name")
        or record.get("step_id")
        or record.get("conflicting_step_id")
        or record.get("type")
        or "structured requirement"
    )


def _plan_source_label(report: dict[str, Any]) -> str:
    source = report.get("source") or {}
    advisory = source.get("advisory") or {}
    mode = str(advisory.get("mode") or "off")
    provider_key = str(advisory.get("provider") or "")
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
                    f"{state.get('unresolved_alert_count', 0)} without direct open-PR coverage"
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
            if verbosity in {"standard", "detailed"}:
                for prerequisite in step.get("prerequisites") or []:
                    lines.append(f"       Prerequisite: {_plan_record_label(prerequisite)}")
                for conflict in step.get("conflicts") or []:
                    lines.append(f"       Conflict: {_plan_record_label(conflict)}")
                for gate in step.get("validation_gates") or []:
                    lines.append(f"       Validation: {_plan_record_label(gate)}")
            if verbosity == "detailed":
                freshness = step.get("freshness") or {}
                lines.append(
                    f"       Freshness: base={freshness.get('base_sha') or 'unknown'} | "
                    f"head={freshness.get('head_sha') or 'not applicable'}"
                )
            lines.append("       Then: re-inspect repository state before continuing.")
        unresolved = repository.get("unresolved_evidence") or {}
        if unresolved.get("errors") or unresolved.get("truncations"):
            lines.append("  Evidence is partial; resolve collection gaps before execution.")
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
                    f"**{state.get('unresolved_alert_count', 0)}** without direct open-PR coverage."
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
            if verbosity in {"standard", "detailed"}:
                for prerequisite in step.get("prerequisites") or []:
                    lines.append(
                        f"   - Prerequisite: {_escape_markdown(_plan_record_label(prerequisite))}"
                    )
                for conflict in step.get("conflicts") or []:
                    lines.append(
                        f"   - Conflict: {_escape_markdown(_plan_record_label(conflict))}"
                    )
                for gate in step.get("validation_gates") or []:
                    lines.append(
                        f"   - Validation: {_escape_markdown(_plan_record_label(gate))}"
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
            f"Target: {binding.get('repository')} PR #{binding.get('pull_request')} "
            f"@ {binding.get('head_sha')}"
        ),
        f"Update: {decision.get('title') or 'unknown'}",
        f"Base: {binding.get('expected_base')}",
        f"Merge method: {binding.get('merge_method')}",
        f"Classification: {decision.get('classification') or 'unknown'}",
        "Decision source: deterministic policy (no model used)",
        f"Action: {report.get('action_id')}",
        f"Mutation performed: {'yes' if report.get('mutation_performed') else 'no'}",
    ]
    required_checks = decision.get("required_checks") or []
    if required_checks:
        checks = ", ".join(
            f"{item.get('name')} ({item.get('state')})" for item in required_checks
        )
        lines.append(f"Required checks: {checks}")
    result = report.get("result") or {}
    if result:
        result_text = str(result.get("message") or report.get("status"))
        if result.get("merge_commit_sha"):
            result_text += f" @ {result['merge_commit_sha']}"
        lines.append(f"Result: {result_text}")
    post_action = report.get("post_action_state") or {}
    if post_action:
        lines.append(f"Post-action reinspection: {post_action.get('status')}")
        if post_action.get("status") != "unavailable":
            lines.append(
                "Remaining: "
                f"{post_action.get('open_pull_requests')} open PRs | "
                f"{post_action.get('open_alerts')} open alerts | "
                f"{post_action.get('alerts_without_direct_open_pr_coverage')} "
                "without direct open-PR coverage | "
                f"{post_action.get('attention_required')} attention required | "
                f"{post_action.get('recommended_steps')} recommended steps"
            )
        if post_action.get("reason"):
            lines.append(f"Post-action note: {post_action['reason']}")
        if post_action.get("next"):
            lines.append(f"Next: {post_action['next']}")
    if report.get("approval_token"):
        lines.append(f"Approval token: {report['approval_token']}")
    if report.get("next"):
        lines.append(f"Next: {report['next']}")
    if report.get("audit_path"):
        lines.append(f"Audit: {report['audit_path']}")
    for item in report.get("recovery") or []:
        lines.append(f"Next: {item}")
    if output_format == "text":
        return "\n".join(lines) + "\n"
    markdown = [f"# {heading}", ""]
    markdown.extend(f"- {_escape_markdown(line)}" for line in lines[1:])
    return "\n".join(markdown) + "\n"
