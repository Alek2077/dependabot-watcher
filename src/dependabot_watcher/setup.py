from __future__ import annotations

import json
import os
import shlex
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import (
    ConfigError,
    SupervisorConfig,
    default_config_path,
    normalize_branch_name,
    normalize_repository_name,
    parse_config,
)
from .diagnostics import (
    CommandRunner,
    _repository_branch_head,
    _repository_metadata,
    _run_command,
    _tool_status,
)
from .runtime_state import (
    RuntimeStateError,
    ensure_private_directory,
    ensure_user_owned_directory,
    secure_private_file,
)


SETUP_SCHEMA_VERSION = 1
Clock = Callable[[], datetime]


class SetupError(RuntimeError):
    """Raised when safe setup cannot produce a trustworthy user configuration."""


@dataclass(frozen=True)
class SetupRequest:
    repositories: tuple[str, ...]
    branches: Mapping[str, str]
    merge_methods: Mapping[str, str] = field(default_factory=dict)
    required_checks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    report_verbosity: str = "concise"
    advisory_mode: str = "off"
    triage_model: str | None = None
    triage_reasoning: str | None = None
    review_model: str | None = None
    review_reasoning: str | None = None
    acknowledge_model_use: bool = False
    require_advice_before_execution: bool = False
    autonomy: str = "read_only"
    acknowledge_mutation: bool = False
    offline: bool = False
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(clock: Clock) -> str:
    return clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _shell_quote(value: str) -> str:
    """Quote a path for the shell used by the current operator host."""

    if os.name == "nt":
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def _supervisor_data(policy: SupervisorConfig) -> dict[str, Any]:
    if policy.mode == "off":
        if policy != SupervisorConfig():
            raise SetupError("Disabled supervision cannot retain enabled supervisor choices.")
        return {"mode": "off"}
    data = asdict(policy)
    if policy.permission_profile is None:
        data.pop("permission_profile")
    data["workspaces"] = [
        {"repository": item.repository, "directory": item.directory,
         "instruction_sources": list(item.instruction_sources), "refresh_authorized": item.refresh_authorized}
        for item in policy.workspaces
    ]
    return data


def _config_data(
    request: SetupRequest,
    *,
    observed_branches: Mapping[str, str],
    generated_at: str,
    verified: bool,
) -> dict[str, Any]:
    source = (
        "setup_quick"
        if request.advisory_mode == "off"
        and request.report_verbosity == "concise"
        and request.autonomy == "read_only"
        and request.supervisor.mode == "off"
        else "setup_advanced"
    )
    data: dict[str, Any] = {
        "schema_version": 1,
        "operation": {
            "autonomy": request.autonomy,
            "mutation_enabled": request.autonomy == "approval_required",
            "require_advice_before_execution": request.require_advice_before_execution,
            "report_verbosity": request.report_verbosity,
            "machine_output": "json",
        },
        "profile": {
            "source": source,
            "created_at": generated_at,
            "confirmed_at": generated_at,
        },
        "advisory": {"mode": request.advisory_mode},
        "supervisor": _supervisor_data(request.supervisor),
        "repositories": [
            {
                "name": repository,
                "default_branch": request.branches.get(repository)
                or observed_branches.get(repository)
                or "main",
                **(
                    {
                        "workflow": {
                            "merge_method": request.merge_methods[repository],
                            "required_checks": list(request.required_checks[repository]),
                        }
                    }
                    if request.autonomy == "approval_required"
                    else {}
                ),
            }
            for repository in request.repositories
        ],
    }
    if verified:
        data["profile"]["last_verified_at"] = generated_at
    if request.advisory_mode != "off":
        data["advisory"].update(
            {
                "provider": "codex_cli",
                "codex_cli": {
                    "triage": {
                        "model": request.triage_model,
                        "reasoning_effort": request.triage_reasoning,
                    }
                },
            }
        )
        if request.advisory_mode == "reviewed":
            data["advisory"]["codex_cli"]["review"] = {
                "model": request.review_model,
                "reasoning_effort": request.review_reasoning,
            }
    return data


def _render_config(data: Mapping[str, Any]) -> str:
    operation = data["operation"]
    profile = data["profile"]
    advisory = data["advisory"]
    lines = [
        "# Generated by `dependabot-watcher setup`.",
        "# This file contains non-secret operational choices. Do not add credentials.",
        "schema_version = 1",
        "",
        "[operation]",
        f"autonomy = {_toml_string(operation['autonomy'])}",
        f"mutation_enabled = {str(operation['mutation_enabled']).lower()}",
        "require_advice_before_execution = "
        f"{str(operation['require_advice_before_execution']).lower()}",
        f"report_verbosity = {_toml_string(operation['report_verbosity'])}",
        f"machine_output = {_toml_string(operation['machine_output'])}",
        "",
        "[profile]",
        f"source = {_toml_string(profile['source'])}",
        f"created_at = {_toml_string(profile['created_at'])}",
        f"confirmed_at = {_toml_string(profile['confirmed_at'])}",
    ]
    if profile.get("last_verified_at"):
        lines.append(f"last_verified_at = {_toml_string(profile['last_verified_at'])}")
    lines.extend(["", "[advisory]", f"mode = {_toml_string(advisory['mode'])}"])
    codex = advisory.get("codex_cli")
    if codex:
        lines.extend(
            [
                f"provider = {_toml_string(advisory['provider'])}",
                "",
                "[advisory.codex_cli.triage]",
                f"model = {_toml_string(codex['triage']['model'])}",
                f"reasoning_effort = {_toml_string(codex['triage']['reasoning_effort'])}",
            ]
        )
        if codex.get("review"):
            lines.extend(
                [
                    "",
                    "[advisory.codex_cli.review]",
                    f"model = {_toml_string(codex['review']['model'])}",
                    f"reasoning_effort = {_toml_string(codex['review']['reasoning_effort'])}",
                ]
            )
    for repository in data["repositories"]:
        lines.extend(
            [
                "",
                "[[repositories]]",
                f"name = {_toml_string(repository['name'])}",
                f"default_branch = {_toml_string(repository['default_branch'])}",
            ]
        )
        workflow = repository.get("workflow")
        if workflow:
            lines.extend(
                [
                    "",
                    "[repositories.workflow]",
                    f"merge_method = {_toml_string(workflow['merge_method'])}",
                    "required_checks = ["
                    + ", ".join(_toml_string(item) for item in workflow["required_checks"])
                    + "]",
                ]
            )
    supervisor = data["supervisor"]
    lines.extend(["", "[supervisor]", f"mode = {_toml_string(supervisor['mode'])}"])
    if supervisor["mode"] != "off":
        for key in ("provider", "model", "reasoning_effort", "expected_account_type", "access", "permission_profile"):
            if key in supervisor:
                lines.append(f"{key} = {_toml_string(supervisor[key])}")
        for key in ("model_use_acknowledged", "full_access_acknowledged", "validation_authorized"):
            lines.append(f"{key} = {str(supervisor[key]).lower()}")
        for key in ("timeout_seconds", "max_jobs_per_run"):
            lines.append(f"{key} = {supervisor[key]}")
        for workspace in supervisor["workspaces"]:
            lines.extend(["", "[[supervisor.workspaces]]",
                          f"repository = {_toml_string(workspace['repository'])}",
                          f"directory = {_toml_string(workspace['directory'])}",
                          f"refresh_authorized = {str(workspace['refresh_authorized']).lower()}",
                          "instruction_sources = [" + ", ".join(
                              _toml_string(source) for source in workspace["instruction_sources"]) + "]"])
    return "\n".join(lines) + "\n"


def _write_exclusive(path: Path, content: str) -> None:
    expanded = path.expanduser()
    if os.name == "nt":
        try:
            ensure_private_directory(expanded.parent)
        except RuntimeStateError as exc:
            raise SetupError(str(exc)) from exc
    else:
        try:
            ensure_user_owned_directory(expanded.parent)
        except RuntimeStateError as exc:
            raise SetupError(str(exc)) from exc
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{expanded.name}.",
            suffix=".tmp",
            dir=expanded.parent,
        )
        temporary_path = Path(temporary_name)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = -1
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, expanded)
        except FileExistsError as exc:
            raise SetupError(
                f"Configuration already exists at {expanded}; setup will not overwrite it."
            ) from exc
        except OSError as exc:
            raise SetupError(f"Could not atomically create configuration at {expanded}.") from exc
        if os.name == "nt":
            try:
                secure_private_file(expanded)
            except RuntimeStateError as exc:
                expanded.unlink(missing_ok=True)
                raise SetupError(str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_request(request: SetupRequest) -> None:
    if not request.repositories:
        raise SetupError("Setup requires at least one --repo owner/repository value.")
    if len({name.casefold() for name in request.repositories}) != len(request.repositories):
        raise SetupError("Setup repositories must be unique.")
    try:
        for repository in request.repositories:
            if normalize_repository_name(repository) != repository:
                raise ConfigError("repository names must not contain surrounding whitespace.")
        for repository, branch in request.branches.items():
            if normalize_repository_name(repository) != repository:
                raise ConfigError("branch repository names must not contain surrounding whitespace.")
            if normalize_branch_name(branch) != branch:
                raise ConfigError("branch names must not contain surrounding whitespace.")
    except ConfigError as exc:
        raise SetupError(f"Invalid setup identity: {exc}") from exc
    unknown_branches = sorted(set(request.branches) - set(request.repositories))
    if unknown_branches:
        raise SetupError(
            "Branch overrides reference repositories not selected for setup: "
            + ", ".join(unknown_branches)
            + "."
        )
    if request.autonomy not in {"read_only", "approval_required"}:
        raise SetupError("Setup autonomy must be read_only or approval_required.")
    if request.autonomy == "approval_required" and not request.acknowledge_mutation:
        raise SetupError(
            "Approval-gated execution requires --acknowledge-mutation; "
            "setup itself performs no GitHub mutation."
        )
    merge_methods = request.merge_methods or {}
    required_checks = request.required_checks or {}
    if request.autonomy == "read_only" and (request.acknowledge_mutation or merge_methods or required_checks):
        raise SetupError("Mutation acknowledgement and merge/check choices require autonomy approval_required.")
    unknown_workflows = sorted(
        (set(merge_methods) | set(required_checks)) - set(request.repositories)
    )
    if unknown_workflows:
        raise SetupError(
            "Workflow settings reference repositories not selected for setup: "
            + ", ".join(unknown_workflows)
            + "."
        )
    if request.autonomy == "approval_required":
        missing_methods = [name for name in request.repositories if name not in merge_methods]
        missing_checks = [name for name in request.repositories if not required_checks.get(name)]
        if missing_methods:
            raise SetupError(
                "Approval-gated setup requires --merge-method for: "
                + ", ".join(missing_methods)
                + "."
            )
        if missing_checks:
            raise SetupError(
                "Approval-gated setup requires at least one --required-check for: "
                + ", ".join(missing_checks)
                + "."
            )
        if any(method not in {"merge", "squash", "rebase"} for method in merge_methods.values()):
            raise SetupError("Approval-gated merge methods must be merge, squash, or rebase.")
    if request.advisory_mode != "off" and not request.acknowledge_model_use:
        raise SetupError(
            "Enabling model advice requires --acknowledge-model-use; setup itself makes no model call."
        )
    if request.advisory_mode == "off" and any((request.triage_model is not None,
            request.triage_reasoning is not None, request.review_model is not None,
            request.review_reasoning is not None, request.acknowledge_model_use)):
        raise SetupError("Advice model/reasoning/acknowledgement choices require an enabled advisory mode.")
    if request.advisory_mode != "reviewed" and (request.review_model is not None or request.review_reasoning is not None):
        raise SetupError("Review model/reasoning choices require advisory mode reviewed.")
    if request.require_advice_before_execution and request.advisory_mode == "off":
        raise SetupError(
            "--require-advice-before-execution requires economy or reviewed advisory mode."
        )
    if request.advisory_mode != "off" and (
        not request.triage_model or not request.triage_reasoning
    ):
        raise SetupError(
            "Enabled model advice requires explicit --triage-model and --triage-reasoning."
        )
    if request.advisory_mode == "reviewed" and (
        not request.review_model or not request.review_reasoning
    ):
        raise SetupError(
            "Reviewed mode requires explicit --review-model and --review-reasoning."
        )
    if request.supervisor.mode != "off" and request.advisory_mode != "off":
        raise SetupError("Choose tool-capable supervision or metadata advice, not both for the same profile.")
    # Validate saved supervisor scope/consent before any authentication or
    # repository call. This is parsing, not workspace or runtime attestation.
    try:
        parse_config({"schema_version": 1, "repositories": [{"name": name} for name in request.repositories],
                      "supervisor": _supervisor_data(request.supervisor)})
    except ConfigError as exc:
        raise SetupError(f"Supervisor choices are invalid: {exc}") from exc


def setup(
    output_path: Path,
    request: SetupRequest,
    *,
    runner: CommandRunner = _run_command,
    clock: Clock = _utc_now,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Create one explicit, non-secret operational profile without external mutation."""

    # GitHub repository lookup is case-insensitive; retain selected spelling in
    # the profile, and bind every override to it before validation or lookup.
    names = {name.casefold(): name for name in request.repositories}
    def canonical(values: Mapping[str, Any], label: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in values.items():
            selected = names.get(name.casefold(), name)
            if selected in result:
                raise SetupError(f"{label} overrides repeat a case-insensitive repository alias.")
            result[selected] = value
        return result
    request = replace(request, branches=canonical(request.branches, "Branch"),
                      merge_methods=canonical(request.merge_methods, "Merge method"),
                      required_checks=canonical(request.required_checks, "Required check"))
    _validate_request(request)
    expanded = output_path.expanduser()
    if expanded.exists():
        raise SetupError(
            f"Configuration already exists at {expanded}; setup will not overwrite it."
        )

    observed_branches: dict[str, str] = {}
    verification: dict[str, Any] = {
        "mode": "offline" if request.offline else "read_only_online",
        "github_authentication": "skipped" if request.offline else "unknown",
        "provider_authentication": (
            "not_required"
            if request.advisory_mode == "off" and request.supervisor.mode == "off"
            else "skipped"
            if request.offline
            else "unknown"
        ),
        "repositories": {},
    }
    verified = False
    if not request.offline:
        gh = _tool_status(
            "gh",
            version_args=("--version",),
            auth_args=("auth", "status", "--active", "--hostname", "github.com"),
            offline=False,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        verification["github_authentication"] = gh["authentication"]
        if gh["availability"] != "available" or gh["authentication"] != "available":
            raise SetupError("GitHub CLI must be installed and authenticated for verified setup.")
        for repository in request.repositories:
            branch, error = _repository_metadata(
                repository,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )
            if error is not None or branch is None:
                raise SetupError(f"Read-only repository verification failed for {repository}.")
            observed_branches[repository] = branch
            verification["repositories"][repository] = {
                "access": "available",
                "observed_default_branch": branch,
            }
            configured_branch = request.branches.get(repository)
            if configured_branch is not None and configured_branch != branch:
                verification["repositories"][repository]["branch_override"] = configured_branch
            selected_branch = configured_branch or branch
            branch_sha, branch_error = _repository_branch_head(
                repository,
                selected_branch,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )
            if branch_error is not None or branch_sha is None:
                raise SetupError(
                    f"Configured branch verification failed for {repository}:{selected_branch}."
                )
            verification["repositories"][repository].update(
                {
                    "configured_branch": selected_branch,
                    "configured_branch_head_sha": branch_sha,
                }
            )
        if request.advisory_mode != "off" or request.supervisor.mode != "off":
            codex = _tool_status(
                "codex",
                version_args=("--version",),
                auth_args=("login", "status"),
                offline=False,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )
            verification["provider_authentication"] = codex["authentication"]
            if codex["availability"] != "available" or codex["authentication"] != "available":
                raise SetupError(
                    "Codex CLI must be installed and authenticated for the selected AI mode."
                )
        verified = True

    generated_at = _timestamp(clock)
    data = _config_data(
        request,
        observed_branches=observed_branches,
        generated_at=generated_at,
        verified=verified,
    )
    try:
        parsed_config = parse_config(data)
    except ConfigError as exc:
        raise SetupError(f"Setup choices are invalid: {exc}") from exc
    _write_exclusive(expanded, _render_config(data))
    automatically_loaded = expanded.resolve() == default_config_path().expanduser().resolve()
    config_argument = (
        "" if automatically_loaded else f" --config {_shell_quote(str(expanded))}"
    )
    return {
        "schema_version": SETUP_SCHEMA_VERSION,
        "mode": "setup",
        "generated_at": generated_at,
        "status": "created",
        "configuration_path": str(expanded),
        "configuration_auto_loaded": automatically_loaded,
        "profile_source": data["profile"]["source"],
        "repositories": data["repositories"],
        "operation": data["operation"],
        "advisory": {
            "mode": request.advisory_mode,
            "provider": "codex_cli" if request.advisory_mode != "off" else None,
            "model_use_acknowledged": request.acknowledge_model_use,
            "triage": (
                asdict(parsed_config.advisory.codex_cli.triage)
                if parsed_config.advisory.codex_cli is not None
                and parsed_config.advisory.codex_cli.triage is not None
                else None
            ),
            "review": (
                asdict(parsed_config.advisory.codex_cli.review)
                if parsed_config.advisory.codex_cli is not None
                and parsed_config.advisory.codex_cli.review is not None
                else None
            ),
            "budget": asdict(parsed_config.advisory.budget),
            "usage_effect": (
                "no_model_calls_or_provider_usage"
                if request.advisory_mode == "off"
                else "may_consume_provider_quota_or_incur_cost"
            ),
            "evidence_privacy": (
                "not_applicable_ai_off"
                if request.advisory_mode == "off"
                else "selected_repository_pr_alert_dependency_path_check_and_deterministic_assessment_evidence_may_be_sent_to_provider"
            ),
        },
        "supervisor": {
            **asdict(parsed_config.supervisor),
            "workspace_verified": False,
            "runtime_verified": False,
            "scheduled_initiation_verified": False,
            "github_mutations_authorized": False,
            "usage_effect": ("no_model_calls_or_provider_usage" if request.supervisor.mode == "off"
                             else "subscription_or_api_usage_if_launched"),
            "evidence_privacy": ("not_applicable_ai_off" if request.supervisor.mode == "off"
                                 else "repository_evidence_and_saved_questions_sent_to_selected_provider_if_launched"),
        },
        "verification": verification,
        "capabilities": {
            "model_calls": False,
            "github_mutations": False,
            "notifications": False,
            "scheduling": False,
            "persistent_local_writes": True,
        },
        "next_commands": [
            f"dependabot-watcher doctor{config_argument}",
            f"dependabot-watcher run{config_argument} --notify off",
        ],
        "next_decisions": [
            "Run one read-only reconciliation before scheduling recurring operation.",
            "Choose a schedule cadence and notification mode before installing a native schedule.",
            (
                "Supervisor choices are saved, not launched. Verify the isolated workspaces, instructions and runtime before investigation; scheduled initiation is a separate check."
                if request.supervisor.mode != "off" else
                "AI advice is off; decide whether deterministic operation is sufficient before enabling model use."
                if request.advisory_mode == "off"
                else "Review the configured models, reasoning levels, and call limits before recurring operation."
            ),
        ],
    }
