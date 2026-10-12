from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlencode

import yaml

from .config import ConfigError, WatcherConfig, load_config
from .execution import ExecutionError, default_state_directory, validate_audit_history
from .native_review import NativeReviewError, verify_native_component, verify_native_host
from .runtime_state import RuntimeStateError
from .windows_helpers import WindowsHelperError


DIAGNOSTIC_SCHEMA_VERSION = 1
_DEPENDABOT_CONFIG_PATHS = (
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
)
_MAX_DEPENDABOT_CONFIG_BYTES = 131_072
_MAX_DEPENDABOT_YAML_DEPTH = 64
_MAX_DEPENDABOT_YAML_NODES = 10_000
_MAX_DEPENDABOT_UPDATES = 1_000


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], int], CommandResult]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_command(command: Sequence[str], timeout_seconds: int) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CommandResult(returncode=127)
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _timestamp(clock: Clock) -> str:
    return clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_version(output: str) -> str | None:
    line = output.splitlines()[0].strip() if output.splitlines() else ""
    if not line or any(ord(character) < 32 for character in line):
        return None
    return line[:160]


def _tool_status(
    executable: str,
    *,
    version_args: Sequence[str],
    auth_args: Sequence[str],
    offline: bool,
    runner: CommandRunner,
    timeout_seconds: int,
) -> dict[str, Any]:
    resolved = shutil.which(executable)
    if resolved is None:
        return {
            "executable": executable,
            "availability": "unavailable",
            "version": None,
            "authentication": "unavailable",
        }
    version_result = runner([executable, *version_args], timeout_seconds)
    version = _safe_version(version_result.stdout) if version_result.returncode == 0 else None
    authentication = "unknown"
    if not offline:
        auth_result = runner([executable, *auth_args], timeout_seconds)
        authentication = "available" if auth_result.returncode == 0 else "unavailable"
    return {
        "executable": executable,
        "availability": "available",
        "version": version,
        "authentication": authentication,
    }


def _repository_metadata(
    repository: str,
    *,
    runner: CommandRunner,
    timeout_seconds: int,
) -> tuple[str | None, str | None]:
    result = runner(
        [
            "gh",
            "repo",
            "view",
            repository,
            "--json",
            "nameWithOwner,defaultBranchRef",
        ],
        timeout_seconds,
    )
    if result.returncode != 0:
        return None, "repository_access_failed"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "repository_metadata_invalid"
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("nameWithOwner"), str)
        or payload["nameWithOwner"].casefold() != repository.casefold()
    ):
        return None, "repository_identity_mismatch"
    default_branch = payload.get("defaultBranchRef")
    if not isinstance(default_branch, dict):
        return None, "repository_metadata_invalid"
    branch = default_branch.get("name")
    if not isinstance(branch, str) or not branch.strip():
        return None, "default_branch_missing"
    return branch.strip(), None


def _repository_branch_head(
    repository: str,
    branch: str,
    *,
    runner: CommandRunner,
    timeout_seconds: int,
) -> tuple[str | None, str | None]:
    endpoint = f"/repos/{repository}/commits/{quote(branch, safe='')}"
    result = runner(["gh", "api", endpoint], timeout_seconds)
    if result.returncode != 0:
        return None, "configured_branch_unavailable"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "configured_branch_response_invalid"
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not sha.strip():
        return None, "configured_branch_response_invalid"
    return sha.strip(), None


def _dependabot_alert_access(
    repository: str,
    *,
    runner: CommandRunner,
    timeout_seconds: int,
) -> tuple[str, str | None]:
    max_response_bytes = 256 * 1024
    query = urlencode({"state": "open", "per_page": 1})
    endpoint = f"/repos/{repository}/dependabot/alerts?{query}"
    result = runner(
        ["gh", "api", endpoint, "--jq", ".[0:2] | map(.number)"],
        timeout_seconds,
    )
    if result.returncode != 0:
        return "unavailable", "dependabot_alert_access_failed"
    if len(result.stdout.encode("utf-8")) > max_response_bytes:
        return "unavailable", "dependabot_alert_response_invalid"
    try:
        payload = json.loads(result.stdout)
    except (ValueError, RecursionError):
        return "unavailable", "dependabot_alert_response_invalid"
    if (
        not isinstance(payload, list)
        or len(payload) > 1
        or any(
            isinstance(number, bool) or not isinstance(number, int) or number <= 0
            for number in payload
        )
    ):
        return "unavailable", "dependabot_alert_response_invalid"
    return "available", None


def _dependabot_config_text(
    repository: str,
    branch: str,
    *,
    runner: CommandRunner,
    timeout_seconds: int,
) -> tuple[str | None, str | None, str | None]:
    for config_path in _DEPENDABOT_CONFIG_PATHS:
        endpoint = (
            f"/repos/{repository}/contents/{quote(config_path, safe='/')}?"
            f"{urlencode({'ref': branch})}"
        )
        result = runner(["gh", "api", endpoint], timeout_seconds)
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None, config_path, "invalid_github_response"
        if not isinstance(payload, dict) or payload.get("type") != "file":
            return None, config_path, "unexpected_github_response"
        size = payload.get("size")
        content = payload.get("content")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_DEPENDABOT_CONFIG_BYTES
            or payload.get("encoding") != "base64"
            or not isinstance(content, str)
        ):
            return None, config_path, "invalid_or_oversized_config"
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
            if len(raw) > _MAX_DEPENDABOT_CONFIG_BYTES:
                return None, config_path, "invalid_or_oversized_config"
            return raw.decode("utf-8"), config_path, None
        except (UnicodeDecodeError, ValueError, TypeError):
            return None, config_path, "invalid_config_encoding"
    return None, None, "not_found_or_inaccessible"


class _BoundedSafeLoader(yaml.SafeLoader):
    """Safe YAML loader with explicit structural bounds for repository-controlled input."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._dependabot_depth = 0
        self._dependabot_nodes = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node | None:
        self._dependabot_depth += 1
        self._dependabot_nodes += 1
        try:
            if self._dependabot_depth > _MAX_DEPENDABOT_YAML_DEPTH:
                raise yaml.YAMLError("Dependabot YAML nesting exceeds the supported limit.")
            if self._dependabot_nodes > _MAX_DEPENDABOT_YAML_NODES:
                raise yaml.YAMLError("Dependabot YAML node count exceeds the supported limit.")
            return super().compose_node(parent, index)
        finally:
            self._dependabot_depth -= 1

    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.YAMLError("Dependabot YAML mapping key is not hashable.") from exc
            if duplicate:
                raise yaml.YAMLError("Dependabot YAML contains a duplicate mapping key.")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _bounded_yaml_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    return text


def _dependabot_ecosystems(text: str) -> tuple[tuple[str, ...], str | None]:
    try:
        payload = yaml.load(text, Loader=_BoundedSafeLoader)
    except (yaml.YAMLError, RecursionError):
        return (), "invalid_yaml"
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or type(version) is not int or version != 2:
        return (), "unsupported_or_missing_version"
    updates = payload.get("updates")
    if not isinstance(updates, list) or not updates or len(updates) > _MAX_DEPENDABOT_UPDATES:
        return (), "updates_missing_or_invalid"
    ecosystems: list[str] = []
    for update in updates:
        if not isinstance(update, dict):
            return (), "update_entry_invalid"
        ecosystem = _bounded_yaml_text(update.get("package-ecosystem"), maximum=100)
        if ecosystem is None:
            return (), "package_ecosystem_missing_or_invalid"
        directory = _bounded_yaml_text(update.get("directory"), maximum=1_024)
        directories = update.get("directories")
        valid_directories = (
            isinstance(directories, list)
            and 0 < len(directories) <= 100
            and all(
                _bounded_yaml_text(item, maximum=1_024) is not None
                for item in directories
            )
        )
        if (directory is None and not valid_directories) or (
            directory is not None and valid_directories
        ):
            return (), "directory_configuration_missing_or_invalid"
        schedule = update.get("schedule")
        if not isinstance(schedule, dict) or _bounded_yaml_text(
            schedule.get("interval"), maximum=50
        ) is None:
            return (), "schedule_missing_or_invalid"
        ecosystems.append(ecosystem)
    return tuple(dict.fromkeys(ecosystems)), None


def _support_depth(ecosystem: str) -> dict[str, str]:
    deeper = "partial_python_requirements" if ecosystem.casefold() == "pip" else "generic_only"
    return {
        "generic_inspection_and_planning": "supported",
        "deeper_deterministic_analysis": deeper,
        "automatic_execution": "not_available",
    }


def _finding(
    severity: str,
    code: str,
    message: str,
    next_step: str | None = None,
) -> dict[str, str]:
    finding = {"severity": severity, "code": code, "message": message}
    if next_step is not None:
        finding["next_step"] = next_step
    return finding


def _configuration_report(path: Path) -> tuple[WatcherConfig | None, dict[str, Any], list[dict[str, str]]]:
    expanded = path.expanduser()
    if not expanded.exists():
        return (
            None,
            {"path": str(expanded), "status": "missing", "schema_version": None},
            [
                _finding(
                    "warning",
                    "configuration_missing",
                    "No Dependabot Watcher configuration exists at the resolved path.",
                    "Run `dependabot-watcher setup --repo owner/repository`.",
                )
            ],
        )
    try:
        config = load_config(expanded)
    except ConfigError as exc:
        return (
            None,
            {
                "path": str(expanded),
                "status": "invalid",
                "schema_version": None,
                "error": str(exc),
            },
            [
                _finding(
                    "error",
                    "configuration_invalid",
                    "The resolved configuration is invalid.",
                    "Correct the reported configuration error before inspection.",
                )
            ],
        )
    return (
        config,
        {
            "path": str(expanded),
            "status": "valid",
            "schema_version": config.schema_version,
            "repository_count": len(config.repositories),
            "operation": asdict(config.operation),
            "profile": asdict(config.profile),
        },
        [],
    )


def doctor(
    config_path: Path,
    *,
    offline: bool = False,
    runner: CommandRunner = _run_command,
    clock: Clock = _utc_now,
    timeout_seconds: int = 30,
    state_directory: Path | None = None,
) -> dict[str, Any]:
    """Return a bounded, non-secret readiness report without changing local or remote state."""

    config, configuration, findings = _configuration_report(config_path)
    python_supported = sys.version_info >= (3, 11) and sys.version_info.releaselevel == "final"
    if not python_supported:
        findings.append(_finding("error", "python_runtime_unsupported",
            "Use a stable Python 3.11 or newer; prerelease interpreters are not supported."))
    gh = _tool_status(
        "gh",
        version_args=("--version",),
        auth_args=("auth", "status", "--active", "--hostname", "github.com"),
        offline=offline,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    advisory_mode = config.advisory.mode if config is not None else "off"
    supervisor_enabled = config is not None and config.supervisor.mode == "approval_first"
    provider_needed = advisory_mode != "off" or supervisor_enabled
    execution_enabled = bool(
        config is not None
        and config.operation.autonomy == "approval_required"
        and config.operation.mutation_enabled
    )
    state_path = (state_directory or default_state_directory()) if execution_enabled else None
    state_directory_valid = False
    if state_path is not None:
        try:
            state_exists = state_path.exists() or state_path.is_symlink()
            state = state_path.stat() if state_exists and not state_path.is_symlink() else None
        except OSError:
            state_exists = True
            state = None
        if state_exists and (
            state_path.is_symlink()
            or state is None
            or not stat.S_ISDIR(state.st_mode)
            or (hasattr(os, "getuid") and state.st_uid != os.getuid())
            or stat.S_IMODE(state.st_mode) & 0o077
        ):
            findings.append(
                _finding(
                    "error",
                    "execution_state_permissions",
                    "Execution state must be a user-private, non-symbolic-link directory.",
                    f"Correct permissions or configure a safe state directory at {state_path}.",
                )
            )
        elif state_exists:
            state_directory_valid = True
        if state_directory_valid and (state_path / "execution.lock").exists():
            findings.append(
                _finding(
                    "warning",
                    "execution_lock_present",
                    "An execution lock is present; another action may be active or interrupted.",
                    "Inspect the lock and audit history before attempting recovery.",
                )
            )
        if state_directory_valid:
            try:
                validate_audit_history(state_path)
            except ExecutionError:
                findings.append(
                    _finding(
                        "error",
                        "execution_audit_invalid",
                        "Execution audit history is malformed or unsafe.",
                        "Archive or repair the audit deliberately before execution.",
                    )
                )
    codex = _tool_status(
        "codex",
        version_args=("--version",),
        auth_args=("login", "status"),
        offline=offline or not provider_needed,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if not provider_needed:
        codex["authentication"] = "not_required"
    codex["adapter_capabilities"] = ["triage", "review"]
    if supervisor_enabled:
        codex["adapter_capabilities"].append("supervisor_app_server")
        codex["supervisor_runtime_verified"] = False
        try:
            verify_native_host()
        except NativeReviewError:
            codex["windows_component"] = "unsupported_host"
            findings.append(_finding("error", "supervisor_host_unavailable",
                "The current run-triggered supervisor requires Windows with a valid WSL distribution and user.",
                "Use the supported WSL route, or operate the CLI with your own agent; no SDK installation enables this route on another host."))
        else:
            try:
                verify_native_component(state_directory or default_state_directory())
            except (NativeReviewError, RuntimeStateError, WindowsHelperError, OSError):
                codex["windows_component"] = "unavailable_or_changed"
                findings.append(_finding("error", "supervisor_windows_component_unavailable",
                    "WSL supervision requires a selected, unchanged Windows component.",
                    "Obtain the packaged component and select it with review --native-component PATH using the same --config and --state-dir; do not install build tools."))
            else:
                codex["windows_component"] = "verified"

    if gh["availability"] != "available":
        findings.append(
            _finding("error", "github_cli_missing", "GitHub CLI is not available on PATH.")
        )
    elif not offline and gh["authentication"] != "available":
        findings.append(
            _finding(
                "error",
                "github_auth_unavailable",
                "GitHub CLI authentication is not ready for repository preflight.",
                "Authenticate GitHub CLI, then run doctor again.",
            )
        )

    if provider_needed:
        if codex["availability"] != "available":
            findings.append(
                _finding(
                    "error",
                    "provider_cli_missing",
                    "The configured Codex CLI provider is not available on PATH.",
                )
            )
        elif not offline and codex["authentication"] != "available":
            findings.append(
                _finding(
                    "error",
                    "provider_auth_unavailable",
                    "The configured Codex CLI provider is not authenticated.",
                )
            )

    repositories: list[dict[str, Any]] = []
    if config is not None:
        for repository_config in config.repositories:
            repository_report: dict[str, Any] = {
                "repository": repository_config.name,
                "configured_default_branch": repository_config.default_branch,
                "access": "unknown" if offline else "unchecked",
                "observed_default_branch": None,
                "default_branch_matches": None,
                "configured_branch": repository_config.default_branch,
                "configured_branch_access": "unknown" if offline else "unchecked",
                "configured_branch_head_sha": None,
                "dependabot_configuration": "unknown",
                "dependabot_configuration_path": None,
                "dependabot_alert_access": "unknown" if offline else "unchecked",
                "ecosystems": [],
                "support": {},
            }
            if not offline and gh["authentication"] == "available":
                branch, repository_error = _repository_metadata(
                    repository_config.name,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                )
                if repository_error is not None:
                    repository_report["access"] = "unavailable"
                    repository_report["error"] = repository_error
                    findings.append(
                        _finding(
                            "error",
                            "repository_access_failed",
                            f"Repository preflight failed for {repository_config.name}.",
                        )
                    )
                else:
                    repository_report["access"] = "available"
                    repository_report["observed_default_branch"] = branch
                    alert_access, alert_error = _dependabot_alert_access(
                        repository_config.name,
                        runner=runner,
                        timeout_seconds=timeout_seconds,
                    )
                    repository_report["dependabot_alert_access"] = alert_access
                    if alert_error is not None:
                        repository_report["dependabot_alert_access_detail"] = alert_error
                        findings.append(
                            _finding(
                                "warning",
                                "dependabot_alert_access_unavailable",
                                (
                                    "Dependabot security alerts could not be read for "
                                    f"{repository_config.name}."
                                ),
                                (
                                    "Verify that Dependabot alerts are enabled and that the "
                                    "GitHub credential can read them, then run doctor again."
                                ),
                            )
                        )
                    matches = branch == repository_config.default_branch
                    repository_report["default_branch_matches"] = matches
                    if not matches:
                        findings.append(
                            _finding(
                                "warning",
                                "default_branch_mismatch",
                                (
                                    f"{repository_config.name} is configured for "
                                    f"{repository_config.default_branch}, but GitHub reports {branch}."
                                ),
                            )
                        )
                    branch_sha, branch_error = _repository_branch_head(
                        repository_config.name,
                        repository_config.default_branch,
                        runner=runner,
                        timeout_seconds=timeout_seconds,
                    )
                    if branch_error is not None:
                        repository_report["configured_branch_access"] = "unavailable"
                        repository_report["configured_branch_detail"] = branch_error
                        findings.append(
                            _finding(
                                "error",
                                "configured_branch_unavailable",
                                (
                                    f"Configured branch {repository_config.default_branch} "
                                    f"could not be verified for {repository_config.name}."
                                ),
                                "Correct the configured branch before inspection.",
                            )
                        )
                    else:
                        repository_report["configured_branch_access"] = "available"
                        repository_report["configured_branch_head_sha"] = branch_sha
                        text, detected_config_path, dependabot_error = _dependabot_config_text(
                            repository_config.name,
                            repository_config.default_branch,
                            runner=runner,
                            timeout_seconds=timeout_seconds,
                        )
                        repository_report[
                            "dependabot_configuration_path"
                        ] = detected_config_path
                        if dependabot_error is not None:
                            repository_report["dependabot_configuration"] = "unavailable"
                            repository_report["dependabot_configuration_detail"] = dependabot_error
                            findings.append(
                                _finding(
                                    "warning",
                                    "dependabot_configuration_unavailable",
                                    (
                                        "Dependabot configuration could not be read from "
                                        f"{repository_config.name}:{repository_config.default_branch}."
                                    ),
                                    "Verify the configured branch, file path, and token permissions.",
                                )
                            )
                        elif text is not None:
                            ecosystems, parse_error = _dependabot_ecosystems(text)
                            if parse_error is not None:
                                repository_report["dependabot_configuration"] = "invalid"
                                repository_report["dependabot_configuration_detail"] = parse_error
                                findings.append(
                                    _finding(
                                        "warning",
                                        "dependabot_configuration_invalid",
                                        (
                                            "Dependabot configuration could not be interpreted "
                                            f"for {repository_config.name}."
                                        ),
                                    )
                                )
                            else:
                                repository_report["dependabot_configuration"] = "available"
                                repository_report["ecosystems"] = list(ecosystems)
                                repository_report["support"] = {
                                    ecosystem: _support_depth(ecosystem)
                                    for ecosystem in ecosystems
                                }
            repositories.append(repository_report)

    if offline:
        findings.append(
            _finding(
                "warning",
                "offline_mode",
                "External authentication and repository checks were skipped.",
            )
        )

    has_error = any(item["severity"] == "error" for item in findings)
    has_warning = any(item["severity"] == "warning" for item in findings)
    status = "blocked" if has_error else "attention" if has_warning else "ready"
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "mode": "doctor",
        "generated_at": _timestamp(clock),
        "status": status,
        "offline": offline,
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
            "scheduling": False,
        },
        "runtime": {
            "platform": platform.system().lower(),
            "platform_release": platform.release(),
            "python": platform.python_version(),
            "python_supported": python_supported,
        },
        "configuration": configuration,
        "state": {
            "persistent_runtime_state": (
                "approval_gated_execution_audit" if execution_enabled else "not_used"
            ),
            "path": str(state_path) if state_path is not None else None,
        },
        "tools": {"github_cli": gh, "codex_cli": codex},
        "deployment_adapters": {
            "schedulers": [],
            "notifications": [],
        },
        "repositories": repositories,
        "findings": findings,
    }
