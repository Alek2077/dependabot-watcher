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
    LocalSyncCheckout,
    SupervisorConfig,
    SupervisorWorkspaceConfig,
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
from .local_sync import LocalSyncError, validate_checkout_control_locations
from .runtime_state import (
    RuntimeStateError,
    default_state_directory,
    ensure_private_directory,
    ensure_user_owned_directory,
    secure_private_file,
)
from .supervisor_workspace import verify_supervisor_workspace
from .supervisor_workspace_provisioning import (
    DEFAULT_WORKSPACE_MAX_BYTES,
    WorkspaceProvisionRequest,
    WorkspaceProvisioningError,
    provision_supervisor_workspaces,
    source_checkout_git_directory,
    workspace_directory_for,
    workspace_root_for_state,
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
    supervisor_provision_sources: Mapping[str, Path] = field(default_factory=dict)
    supervisor_provision_instructions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    supervisor_provision_refresh: tuple[str, ...] = ()
    supervisor_workspace_root: Path | None = None
    supervisor_workspace_max_bytes: int = DEFAULT_WORKSPACE_MAX_BYTES
    local_sync: Mapping[str, tuple[LocalSyncCheckout, ...]] = field(default_factory=dict)
    acknowledge_local_sync: bool = False
    require_local_completion: tuple[str, ...] = ()


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
    data['validation']['storage_reserve_paths'] = list(policy.validation.storage_reserve_paths)
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
        and not request.local_sync
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
                **({"completion_policy": "selected_local_sync"}
                   if repository in request.require_local_completion else {}),
                **({"local_sync": [{"directory": target.directory,
                                    "branch": target.branch or request.branches.get(repository)
                                    or observed_branches.get(repository) or "main"}
                                   for target in request.local_sync[repository]]}
                   if request.local_sync.get(repository) else {}),
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
        if "completion_policy" in repository:
            lines.append(f"completion_policy = {_toml_string(repository['completion_policy'])}")
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
        for checkout in repository.get("local_sync", []):
            lines.extend(["", "[[repositories.local_sync]]",
                          f"directory = {_toml_string(checkout['directory'])}",
                          f"branch = {_toml_string(checkout['branch'])}"])
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
        validation = supervisor["validation"]
        lines.extend(["", "[supervisor.validation]"])
        for key in ("preparation_authorized", "network_authorized"):
            lines.append(f"{key} = {str(validation[key]).lower()}")
        for key in ("maximum_bytes", "minimum_free_bytes", "command_timeout_seconds"):
            lines.append(f"{key} = {validation[key]}")
        lines.append('storage_reserve_paths = [' + ', '.join(_toml_string(path)
                     for path in validation['storage_reserve_paths']) + ']')
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


def _provision_root(request: SetupRequest) -> Path:
    return request.supervisor_workspace_root or workspace_root_for_state(default_state_directory())


def _resolved_setup_path(path: Path, *, strict: bool = False) -> Path:
    try:
        return path.expanduser().resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise SetupError("Setup paths could not be resolved safely before provisioning.") from exc


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _validate_output_separation(output_path: Path, request: SetupRequest) -> None:
    """Keep the eventual configuration write outside every provisioning input/output tree."""

    output = _resolved_setup_path(output_path)
    for targets in request.local_sync.values():
        for target in targets:
            try:
                validate_checkout_control_locations(Path(target.directory), output, None)
            except LocalSyncError as exc:
                raise SetupError(str(exc)) from exc
    if not request.supervisor_provision_sources:
        return
    protected: list[tuple[Path, str]] = []
    root = _resolved_setup_path(_provision_root(request))
    protected.append((root, "private workspace root"))
    for repository, source_path in request.supervisor_provision_sources.items():
        source = _resolved_setup_path(source_path, strict=True)
        try:
            source_common = _resolved_setup_path(
                source_checkout_git_directory(source_path, repository),
                strict=True,
            )
        except WorkspaceProvisioningError as exc:
            raise SetupError(str(exc)) from exc
        if _paths_overlap(root, source_common):
            raise SetupError(
                "Private workspace root overlaps the source Git metadata directory."
            )
        protected.append((source, "source checkout"))
        protected.append((source_common, "source Git metadata directory"))
        workspace = _resolved_setup_path(workspace_directory_for(root, repository))
        protected.extend((
            (workspace.parent, "generated repository store"),
            (workspace.parent / "source-clone", "generated private clone"),
            (workspace, "maintenance worktree"),
        ))
    for directory, label in protected:
        if _paths_overlap(output, directory):
            raise SetupError(
                f"Configuration output path overlaps the {label}; choose a separate configuration location."
            )


def _canonicalize_provisioning(request: SetupRequest) -> SetupRequest:
    selected = {name.casefold(): name for name in request.repositories}

    def canonical_paths(values: Mapping[str, Path], label: str) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for name, raw_path in values.items():
            repository = selected.get(name.casefold())
            if repository is None:
                raise SetupError(f"{label} references a repository not selected for setup: {name}.")
            if repository in result:
                raise SetupError(f"{label} repeats a case-insensitive repository alias.")
            if not isinstance(raw_path, Path):
                raw_path = Path(raw_path)
            expanded = raw_path.expanduser()
            if not expanded.is_absolute() or ".." in expanded.parts:
                raise SetupError(f"{label} paths must be absolute and traversal-free.")
            result[repository] = expanded
        return result

    sources = canonical_paths(request.supervisor_provision_sources, "Supervisor provisioning source")
    selected_instructions: dict[str, tuple[str, ...]] = {}
    for name, raw_sources in request.supervisor_provision_instructions.items():
        repository = selected.get(name.casefold())
        if repository is None or repository not in sources:
            raise SetupError("Provisioning instruction choices must name a provisioned repository.")
        if repository in selected_instructions:
            raise SetupError("Provisioning instructions repeat a case-insensitive repository alias.")
        selected_instructions[repository] = tuple(raw_sources)
    refresh: list[str] = []
    for name in request.supervisor_provision_refresh:
        repository = selected.get(name.casefold())
        if repository is None or repository not in sources or repository in refresh:
            raise SetupError("--supervisor-refresh-workspace must name one provisioned repository exactly once.")
        refresh.append(repository)
    root = request.supervisor_workspace_root
    if root is not None:
        root = root.expanduser()
        if not root.is_absolute() or ".." in root.parts:
            raise SetupError("--supervisor-workspace-root must be absolute and traversal-free.")
        if not sources:
            raise SetupError("--supervisor-workspace-root requires at least one --supervisor-provision source.")
    if not sources and request.supervisor_workspace_max_bytes != DEFAULT_WORKSPACE_MAX_BYTES:
        raise SetupError("--supervisor-workspace-max-bytes requires at least one --supervisor-provision source.")
    return replace(
        request,
        supervisor_provision_sources=sources,
        supervisor_provision_instructions=selected_instructions,
        supervisor_provision_refresh=tuple(refresh),
        supervisor_workspace_root=root,
    )


def _validation_supervisor(request: SetupRequest) -> SupervisorConfig:
    policy = request.supervisor
    if not request.supervisor_provision_sources:
        return policy
    manual = {item.repository.casefold() for item in policy.workspaces}
    overlap = manual & {name.casefold() for name in request.supervisor_provision_sources}
    if overlap:
        raise SetupError("A repository cannot use both --supervisor-workspace and --supervisor-provision.")
    root = _provision_root(request)
    generated = tuple(
        SupervisorWorkspaceConfig(
            repository=name,
            directory=str(workspace_directory_for(root, name)),
            instruction_sources=request.supervisor_provision_instructions.get(name, ()),
            refresh_authorized=name in request.supervisor_provision_refresh,
        )
        for name in request.supervisor_provision_sources
    )
    return replace(policy, workspaces=policy.workspaces + generated)


def _validate_request(request: SetupRequest) -> None:
    if not request.repositories:
        raise SetupError("Setup requires at least one --repo owner/repository value.")
    if (len(set(request.require_local_completion)) != len(request.require_local_completion)
            or any(repository not in request.repositories or not request.local_sync.get(repository)
                   for repository in request.require_local_completion)):
        raise SetupError("Required local completion must name each configured repository with selected checkouts at most once.")
    if request.local_sync and not request.acknowledge_local_sync:
        raise SetupError("Selected local synchronization requires --acknowledge-local-sync; each update still needs exact approval.")
    if request.acknowledge_local_sync and not request.local_sync:
        raise SetupError("--acknowledge-local-sync requires selected checkouts.")
    if set(request.local_sync) - set(request.repositories):
        raise SetupError("Local synchronization must reference configured repositories only.")
    if request.offline and any(not target.branch and not request.branches.get(repository)
                               for repository, targets in request.local_sync.items() for target in targets):
        raise SetupError("Offline local synchronization setup requires an explicit selected branch.")
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
    if request.supervisor_provision_sources and request.supervisor.mode == "off":
        raise SetupError("Supervisor provisioning requires --supervisor-mode approval_first.")
    if request.supervisor_provision_instructions and not request.supervisor_provision_sources:
        raise SetupError("Provisioning instruction choices require --supervisor-provision.")
    if request.supervisor_provision_refresh and not request.supervisor_provision_sources:
        raise SetupError("Workspace refresh authority requires --supervisor-provision.")
    if type(request.supervisor_workspace_max_bytes) is not int or not (
        1 <= request.supervisor_workspace_max_bytes <= 16 * 1024**3
    ):
        raise SetupError("--supervisor-workspace-max-bytes must be between one byte and 16 GiB.")
    # Validate saved supervisor scope/consent before any authentication or
    # repository call. This is parsing, not workspace or runtime attestation.
    try:
        parse_config({"schema_version": 1, "repositories": [{"name": name} for name in request.repositories],
                      "supervisor": _supervisor_data(_validation_supervisor(request))})
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
    request = _canonicalize_provisioning(request)
    _validate_request(request)
    expanded = output_path.expanduser()
    if expanded.exists():
        raise SetupError(
            f"Configuration already exists at {expanded}; setup will not overwrite it."
        )
    _validate_output_separation(expanded, request)

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
            sync_branches = {target.branch or selected_branch for target in request.local_sync.get(repository, ())}
            for sync_branch in sync_branches - {selected_branch}:
                sync_sha, sync_error = _repository_branch_head(repository, sync_branch, runner=runner,
                                                              timeout_seconds=timeout_seconds)
                if sync_error is not None or sync_sha is None:
                    raise SetupError(f"Selected synchronization branch verification failed for {repository}:{sync_branch}.")
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

    provisioned_workspaces = ()
    if request.supervisor_provision_sources:
        specs: list[WorkspaceProvisionRequest] = []
        root = _provision_root(request)
        for repository, source_path in request.supervisor_provision_sources.items():
            branch = request.branches.get(repository) or observed_branches.get(repository) or "main"
            verified_repository = verification["repositories"].get(repository, {})
            base_sha = verified_repository.get("configured_branch_head_sha")
            specs.append(WorkspaceProvisionRequest(
                repository=repository,
                source_checkout=source_path,
                workspace_directory=workspace_directory_for(root, repository),
                branch=branch,
                collected_base_sha=base_sha,
                explicit_instruction_sources=request.supervisor_provision_instructions.get(repository, ()),
                refresh_authorized=repository in request.supervisor_provision_refresh,
            ))
        try:
            provisioned_workspaces = provision_supervisor_workspaces(
                specs,
                root=root,
                offline=request.offline,
                maximum_bytes=request.supervisor_workspace_max_bytes,
                minimum_free_bytes=request.supervisor.validation.minimum_free_bytes,
                reserve_paths=tuple(Path(path) for path in request.supervisor.validation.storage_reserve_paths),
            )
        except WorkspaceProvisioningError as exc:
            raise SetupError(str(exc)) from exc
        combined = list(request.supervisor.workspaces)
        combined.extend(result.registration for result in provisioned_workspaces)
        request = replace(request, supervisor=replace(request.supervisor, workspaces=tuple(combined)))

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
    if provisioned_workspaces:
        try:
            for result in provisioned_workspaces:
                verify_supervisor_workspace(
                    parsed_config,
                    result.registration.repository,
                    {
                        "repository": result.registration.repository,
                        "evidence_snapshot": {"base_sha": result.base_sha},
                    },
                )
                verification.setdefault("provisioned_workspace_bases", {})[result.registration.repository] = {
                    "status": result.base_provenance,
                    "base_sha": result.base_sha,
                    "workspace_structure": "verified_by_existing_read_only_verifier",
                    "runtime_instruction_discovery_verified": False,
                }
        except Exception as exc:
            raise SetupError(
                "Provisioned workspace failed the existing supervisor verifier; owned files were preserved."
            ) from exc
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
            "workspace_provisioning": [
                {
                    "status": result.status,
                    "source_checkout": result.source_checkout,
                    "directory": result.registration.directory,
                    "base_sha": result.base_sha,
                    "base_provenance": result.base_provenance,
                    "estimated_bytes": result.estimated_bytes,
                    "setup_wide_maximum_bytes": result.maximum_bytes,
                    "minimum_free_bytes": result.minimum_free_bytes,
                    "reserve_paths": list(result.reserve_paths),
                    "discovered_instruction_sources": list(result.discovered_instruction_sources),
                    "expected_instruction_sources": list(result.instruction_sources),
                    "source_checkout_ancestor_instruction_sources": list(
                        result.source_ancestor_instruction_sources
                    ),
                    "maintenance_path_ancestor_instruction_sources": list(
                        result.workspace_ancestor_instruction_sources
                    ),
                    "runtime_instruction_discovery_verified": False,
                    "retention": result.retention,
                }
                for result in provisioned_workspaces
            ],
        },
        "verification": verification,
        **({"local_sync": {"selected_checkouts": [
            {"repository": repository.name, **asdict(target)}
            for repository in parsed_config.repositories for target in repository.local_sync],
            "checkout_readiness_verified": False, "integration_performed": False,
            "required_completion_repositories": list(request.require_local_completion),
            "policy": "separate_exact_confirmation_clean_fast_forward_only"}}
           if request.local_sync else {}),
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
            *(["Selected developer checkouts are registered, not updated. Review each exact clean fast-forward separately; no stash, branch switch or push is authorized."]
              if request.local_sync else []),
            "Run one read-only reconciliation before scheduling recurring operation.",
            "Choose a schedule cadence and notification mode before installing a native schedule.",
            (
                "Supervisor policy is saved, not launched. Persistent provisioned copies passed the local workspace verifier; confirm the runtime instruction chain and account/model before investigation."
                if provisioned_workspaces else
                "Supervisor choices are saved, not launched. Verify the isolated workspaces, instructions and runtime before investigation; scheduled initiation is a separate check."
                if request.supervisor.mode != "off" else
                "AI advice is off; decide whether deterministic operation is sufficient before enabling model use."
                if request.advisory_mode == "off"
                else "Review the configured models, reasoning levels, and call limits before recurring operation."
            ),
        ],
    }
