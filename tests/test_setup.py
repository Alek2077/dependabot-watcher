from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest

import dependabot_watcher.setup as setup_implementation
from dependabot_watcher import diagnostics
from dependabot_watcher.config import LocalSyncCheckout, SupervisorConfig, SupervisorValidationConfig, SupervisorWorkspaceConfig, load_config
from dependabot_watcher.operational_output import render_operational
from dependabot_watcher.diagnostics import CommandResult
from dependabot_watcher.setup import (
    SetupError,
    SetupRequest,
    _shell_quote,
    setup as create_profile,
)


def _runner(commands: list[tuple[str, ...]]):
    def run(command: Sequence[str], timeout: int) -> CommandResult:
        commands.append(tuple(command))
        if command[1:] == ["--version"]:
            return CommandResult(0, f"{command[0]} version test\n")
        if command[:3] in (["gh", "auth", "status"], ["codex", "login", "status"]):
            return CommandResult(0, "authenticated")
        if command[:3] == ["gh", "repo", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "nameWithOwner": command[3],
                        "defaultBranchRef": {"name": "trunk"},
                    }
                ),
            )
        if command[:2] == ["gh", "api"] and "/commits/" in command[2]:
            return CommandResult(0, json.dumps({"sha": "d" * 40}))
        raise AssertionError(f"Unexpected command: {command}")

    return run


def test_quick_setup_verifies_then_creates_model_off_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "nested" / "config.toml"
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={},
        ),
        runner=_runner(commands),
        clock=lambda: datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )

    config = load_config(path)
    assert config.repositories[0].default_branch == "trunk"
    assert config.advisory.mode == "off"
    assert config.operation.autonomy == "read_only"
    assert config.operation.mutation_enabled is False
    assert config.operation.report_verbosity == "concise"
    assert config.profile.source == "setup_quick"
    assert config.profile.last_verified_at == "2026-09-22T12:00:00Z"
    assert report["capabilities"]["model_calls"] is False
    assert report["capabilities"]["github_mutations"] is False
    assert report["capabilities"]["persistent_local_writes"] is True
    assert not any(command[0] == "codex" for command in commands)


def test_selected_local_sync_requires_consent_before_auth_or_profile_write(tmp_path):
    commands: list[tuple[str, ...]] = []
    config_path = tmp_path / "config.toml"
    with pytest.raises(SetupError, match="acknowledge"):
        create_profile(config_path, SetupRequest(repositories=("example-org/example-app",), branches={},
            local_sync={"example-org/example-app": (LocalSyncCheckout(str(tmp_path / "checkout"), "integration"),)}),
            runner=_runner(commands))
    assert commands == []
    assert not config_path.exists()


@pytest.mark.parametrize("required_completion", [False, True])
def test_selected_local_sync_discovers_branch_without_importing_maintainer_conventions(tmp_path, monkeypatch, required_completion):
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    path = tmp_path / "config.toml"
    checkout = str(tmp_path / "checkout")
    report = create_profile(path, SetupRequest(repositories=("example-org/example-app",), branches={},
        local_sync={"example-org/example-app": (LocalSyncCheckout(checkout, ""),)},
        acknowledge_local_sync=True,
        require_local_completion=("example-org/example-app",) if required_completion else ()), runner=_runner(commands))
    config = load_config(path)
    assert config.repositories[0].local_sync == (LocalSyncCheckout(checkout, "trunk"),)
    assert config.repositories[0].completion_policy == ("selected_local_sync" if required_completion else "remote_only")
    assert config.operation.mutation_enabled is False
    assert report["local_sync"]["checkout_readiness_verified"] is False
    assert report["local_sync"]["integration_performed"] is False
    assert not (tmp_path / "checkout").exists()


@pytest.mark.parametrize("repositories", [("example-org/example-app",), ("outside/repository",)])
def test_required_completion_without_selected_scope_fails_before_auth_or_write(tmp_path, repositories):
    commands: list[tuple[str, ...]] = []
    path = tmp_path / "config.toml"
    with pytest.raises(SetupError, match="Required local completion"):
        create_profile(path, SetupRequest(repositories=("example-org/example-app",), branches={},
            require_local_completion=repositories), runner=_runner(commands))
    assert not path.exists()
    assert commands == []


def test_offline_local_sync_requires_explicit_branch(tmp_path):
    with pytest.raises(SetupError, match="explicit selected branch"):
        create_profile(tmp_path / "config.toml", SetupRequest(repositories=("example-org/example-app",),
            branches={}, offline=True, acknowledge_local_sync=True,
            local_sync={"example-org/example-app": (LocalSyncCheckout(str(tmp_path / "checkout"), ""),)}))


def test_local_sync_setup_rejects_configuration_inside_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with pytest.raises(SetupError, match="separate"):
        create_profile(checkout / "watcher.toml", SetupRequest(repositories=("example-org/example-app",),
            branches={}, offline=True, acknowledge_local_sync=True,
            local_sync={"example-org/example-app": (LocalSyncCheckout(str(checkout), "integration"),)}))


def test_local_sync_setup_verifies_selected_nondefault_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    commands: list[tuple[str, ...]] = []
    base_runner = _runner(commands)
    def missing_branch(command, timeout):
        if command[:2] == ["gh", "api"] and "/commits/integration" in command[2]:
            commands.append(tuple(command))
            return CommandResult(1, "")
        return base_runner(command, timeout)
    with pytest.raises(SetupError, match="synchronization branch verification"):
        create_profile(tmp_path / "config.toml", SetupRequest(repositories=("example-org/example-app",), branches={},
            acknowledge_local_sync=True,
            local_sync={"example-org/example-app": (LocalSyncCheckout(str(tmp_path / "checkout"), "integration"),)}),
            runner=missing_branch)
    assert not (tmp_path / "config.toml").exists()


def test_default_configuration_is_reused_without_redundant_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("DEPENDABOT_WATCHER_CONFIG", str(path))

    report = create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={"example-org/example-app": "main"},
            offline=True,
        ),
    )

    assert report["configuration_auto_loaded"] is True
    assert report["next_commands"] == [
        "dependabot-watcher doctor",
        "dependabot-watcher run --notify off",
    ]
    assert report["advisory"]["usage_effect"] == "no_model_calls_or_provider_usage"
    assert report["advisory"]["budget"]["max_triage_calls"] == 0
    assert "Choose a schedule cadence" in report["next_decisions"][1]


def test_custom_configuration_command_uses_shell_safe_path(tmp_path: Path) -> None:
    path = tmp_path / "profile $(touch should-not-run)" / "config.toml"
    path.parent.mkdir()

    report = create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={"example-org/example-app": "main"},
            offline=True,
        ),
    )

    assert "--config '" in report["next_commands"][0]
    assert "$(touch should-not-run)" in report["next_commands"][0]


def test_shell_quote_uses_powershell_single_quote_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_implementation.os, "name", "nt")
    value = r"C:\profiles\founder's $(unsafe)\config.toml"

    assert _shell_quote(value) == "'C:\\profiles\\founder''s $(unsafe)\\config.toml'"


def test_setup_requires_separate_acknowledgement_for_approval_gated_execution(
    tmp_path: Path,
) -> None:
    request = SetupRequest(
        repositories=("example-org/example-app",),
        branches={"example-org/example-app": "main"},
        autonomy="approval_required",
        offline=True,
    )

    with pytest.raises(SetupError, match="acknowledge-mutation"):
        create_profile(tmp_path / "rejected.toml", request)

    report = create_profile(
        tmp_path / "accepted.toml",
        SetupRequest(
            repositories=request.repositories,
            branches=request.branches,
            autonomy="approval_required",
            acknowledge_mutation=True,
            merge_methods={"example-org/example-app": "squash"},
            required_checks={"example-org/example-app": ("CI",)},
            offline=True,
        ),
    )
    config = load_config(tmp_path / "accepted.toml")
    assert config.operation.autonomy == "approval_required"
    assert config.operation.mutation_enabled is True
    assert report["operation"]["mutation_enabled"] is True


def test_setup_refuses_to_overwrite_existing_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("keep me", encoding="utf-8")

    with pytest.raises(SetupError, match="will not overwrite"):
        create_profile(
            path,
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={},
                offline=True,
            ),
        )

    assert path.read_text(encoding="utf-8") == "keep me"


def test_enabled_model_setup_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="acknowledge-model-use"):
        create_profile(
            tmp_path / "config.toml",
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={},
                advisory_mode="economy",
                triage_model="test-model",
                triage_reasoning="high",
                offline=True,
            ),
        )


def test_setup_requires_enabled_advice_for_execution_advice_policy(
    tmp_path: Path,
) -> None:
    with pytest.raises(SetupError, match="requires economy or reviewed"):
        create_profile(
            tmp_path / "rejected.toml",
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={},
                require_advice_before_execution=True,
                offline=True,
            ),
        )

    path = tmp_path / "accepted.toml"
    create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={},
            advisory_mode="economy",
            triage_model="triage-model",
            triage_reasoning="high",
            acknowledge_model_use=True,
            require_advice_before_execution=True,
            offline=True,
        ),
    )

    assert load_config(path).operation.require_advice_before_execution is True


def test_advanced_setup_checks_provider_auth_but_never_invokes_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.toml"
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={},
            report_verbosity="detailed",
            advisory_mode="reviewed",
            triage_model="triage-model",
            triage_reasoning="high",
            review_model="review-model",
            review_reasoning="max",
            acknowledge_model_use=True,
        ),
        runner=_runner(commands),
    )

    config = load_config(path)
    assert config.profile.source == "setup_advanced"
    assert config.operation.report_verbosity == "detailed"
    assert config.advisory.mode == "reviewed"
    assert report["advisory"]["model_use_acknowledged"] is True
    assert report["advisory"]["triage"] == {
        "model": "triage-model",
        "reasoning_effort": "high",
        "timeout_seconds": 900,
    }
    assert report["advisory"]["review"] == {
        "model": "review-model",
        "reasoning_effort": "max",
        "timeout_seconds": 900,
    }
    assert report["advisory"]["budget"]["max_triage_calls"] == 2
    assert report["advisory"]["budget"]["max_review_calls"] == 1
    assert report["advisory"]["usage_effect"] == "may_consume_provider_quota_or_incur_cost"
    assert ("codex", "login", "status") in commands
    assert not any("exec" in command for command in commands)


def test_offline_setup_makes_no_commands_and_records_unverified_profile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        raise AssertionError("Offline setup must make no command calls")

    create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={"example-org/example-app": "develop"},
            offline=True,
        ),
        runner=runner,
    )

    config = load_config(path)
    assert config.repositories[0].default_branch == "develop"
    assert config.profile.last_verified_at is None


def test_online_setup_verifies_configured_branch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.toml"
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = create_profile(
        path,
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={"example-org/example-app": "develop"},
        ),
        runner=_runner(commands),
    )

    assert any(
        "/commits/develop" in command[2]
        for command in commands
        if command[:2] == ("gh", "api")
    )
    verification = report["verification"]["repositories"]["example-org/example-app"]
    assert verification["configured_branch"] == "develop"
    assert verification["configured_branch_head_sha"] == "d" * 40


def test_setup_rejects_repository_flag_injection_before_subprocess(tmp_path: Path) -> None:
    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        raise AssertionError("Invalid repository identity must fail before subprocess execution")

    with pytest.raises(SetupError, match="owner/repository"):
        create_profile(
            tmp_path / "config.toml",
            SetupRequest(repositories=("--evil/repo",), branches={}),
            runner=runner,
        )


def test_online_setup_fails_when_configured_branch_cannot_be_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        if command[1:] == ["--version"]:
            return CommandResult(0, f"{command[0]} version test\n")
        if command[:3] == ["gh", "auth", "status"]:
            return CommandResult(0)
        if command[:3] == ["gh", "repo", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "nameWithOwner": "example-org/example-app",
                        "defaultBranchRef": {"name": "main"},
                    }
                ),
            )
        if command[:2] == ["gh", "api"]:
            return CommandResult(1)
        raise AssertionError(f"Unexpected command: {command}")

    path = tmp_path / "config.toml"
    with pytest.raises(SetupError, match="branch verification failed"):
        create_profile(
            path,
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={"example-org/example-app": "missing"},
            ),
            runner=runner,
        )
    assert not path.exists()


def test_offline_model_setup_marks_provider_verification_skipped(tmp_path: Path) -> None:
    report = create_profile(
        tmp_path / "config.toml",
        SetupRequest(
            repositories=("example-org/example-app",),
            branches={},
            advisory_mode="economy",
            triage_model="test-model",
            triage_reasoning="high",
            acknowledge_model_use=True,
            offline=True,
        ),
    )

    assert report["verification"]["github_authentication"] == "skipped"
    assert report["verification"]["provider_authentication"] == "skipped"


def test_setup_does_not_publish_partial_configuration_on_interruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.toml"

    def interrupt(_descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_implementation.os, "fsync", interrupt)

    with pytest.raises(KeyboardInterrupt):
        create_profile(
            path,
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={},
                offline=True,
            ),
        )

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def _supervisor_policy(tmp_path: Path, **changes) -> SupervisorConfig:
    return replace(SupervisorConfig(
        mode="approval_first", provider="codex_app_server", model="chosen-model",
        reasoning_effort="low", expected_account_type="chatgpt", access="inherit",
        model_use_acknowledged=True,
        workspaces=(SupervisorWorkspaceConfig("example-org/example-app", str(tmp_path / "work=tree"),
                    (str(tmp_path / "trusted instructions" / "AGENTS.md"),)),)), **changes)


@pytest.mark.parametrize("output_format", ["text", "markdown"])
@pytest.mark.parametrize("network", [False, True])
def test_setup_discloses_validation_preparation_and_resource_policy(tmp_path: Path, output_format: str, network: bool) -> None:
    policy = _supervisor_policy(tmp_path, validation_authorized=True,
        validation=SupervisorValidationConfig(preparation_authorized=True,
            network_authorized=network, maximum_bytes=12345678,
            minimum_free_bytes=87654321, command_timeout_seconds=83,
            storage_reserve_paths=(str(tmp_path),)))
    report = create_profile(tmp_path / 'config.toml', SetupRequest(
        repositories=('example-org/example-app',), branches={}, offline=True, supervisor=policy))
    rendered = render_operational(report, output_format)
    assert 'dependency installation' in rendered
    assert '12345678' in rendered and '87654321' in rendered and '83 seconds' in rendered
    assert str(tmp_path) in rendered and 'not a kernel quota' in rendered
    assert ('including reachable local services' in rendered) is network
    assert ('disabled inside the validation sandbox' in rendered) is not network
    assert 'caches and execution receipts are retained' in rendered


@pytest.mark.parametrize("access", ["inherit", "restricted", "full"])
def test_supervisor_setup_roundtrips_saved_policy_without_launch_or_workspace_creation(tmp_path: Path, access: str) -> None:
    policy = _supervisor_policy(tmp_path, access=access,
        permission_profile="chosen-restriction" if access == "restricted" else None,
        full_access_acknowledged=access == "full", validation_authorized=True,
        timeout_seconds=321, max_jobs_per_run=2)
    path = tmp_path / "config.toml"
    report = create_profile(path, SetupRequest(repositories=("example-org/example-app",), branches={},
        offline=True, supervisor=policy))
    saved = load_config(path)
    assert saved.supervisor == policy
    assert saved.profile.source == "setup_advanced"
    assert saved.advisory.mode == "off"
    assert saved.operation.mutation_enabled is False
    assert not Path(policy.workspaces[0].directory).exists()
    assert report["verification"]["provider_authentication"] == "skipped"
    assert report["supervisor"]["workspace_verified"] is False
    assert report["supervisor"]["runtime_verified"] is False
    assert report["supervisor"]["scheduled_initiation_verified"] is False
    assert report["supervisor"]["github_mutations_authorized"] is False
    assert report["capabilities"]["model_calls"] is False


@pytest.mark.parametrize("changes", [
    {"model_use_acknowledged": False}, {"model": None}, {"expected_account_type": None},
    {"access": "full", "full_access_acknowledged": False},
    {"access": "restricted", "permission_profile": None},
    {"access": "inherit", "permission_profile": "unwanted"},
    {"timeout_seconds": 0}, {"max_jobs_per_run": 0}, {"workspaces": ()},
    {"workspaces": (SupervisorWorkspaceConfig("other-org/other-app", "/isolated"),)},
])
def test_invalid_supervisor_setup_refuses_before_authentication_or_writes(tmp_path: Path, changes: dict) -> None:
    def unexpected(*_args):
        raise AssertionError("Invalid choices must not execute commands")
    with pytest.raises(SetupError, match="Supervisor choices"):
        create_profile(tmp_path / "config.toml", SetupRequest(
            repositories=("example-org/example-app",), branches={},
            supervisor=_supervisor_policy(tmp_path, **changes)), runner=unexpected)
    assert list(tmp_path.iterdir()) == []


def test_supervisor_setup_online_checks_existing_authentication_without_model(tmp_path: Path, monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = create_profile(tmp_path / "config.toml", SetupRequest(
        repositories=("example-org/example-app",), branches={}, supervisor=_supervisor_policy(tmp_path)),
        runner=_runner(commands))
    assert ("codex", "login", "status") in commands
    assert not any("exec" in command or "app-server" in command for command in commands)
    assert report["verification"]["provider_authentication"] == "available"
    assert report["supervisor"]["runtime_verified"] is False


def test_setup_refuses_metadata_advice_alongside_supervisor(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="not both"):
        create_profile(tmp_path / "config.toml", SetupRequest(
            repositories=("example-org/example-app",), branches={}, offline=True,
            advisory_mode="economy", triage_model="advice-model", triage_reasoning="low",
            acknowledge_model_use=True, supervisor=_supervisor_policy(tmp_path)))
    assert not (tmp_path / "config.toml").exists()


def test_disabled_supervisor_cannot_silently_retain_full_access(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="Disabled supervision"):
        create_profile(tmp_path / "config.toml", SetupRequest(
            repositories=("example-org/example-app",), branches={}, offline=True,
            supervisor=SupervisorConfig(access="full", full_access_acknowledged=True)))
    assert not (tmp_path / "config.toml").exists()


def test_setup_case_aliases_bind_branch_and_workflow_to_selected_spelling(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    report = create_profile(path, SetupRequest(
        repositories=("EXAMPLE-ORG/Example-App",),
        branches={"example-org/example-app": "develop"},
        merge_methods={"Example-Org/Example-App": "squash"},
        required_checks={"EXAMPLE-ORG/EXAMPLE-APP": ("CI", "Build")},
        autonomy="approval_required", acknowledge_mutation=True, offline=True))
    repository = load_config(path).repositories[0]
    assert repository.name == "EXAMPLE-ORG/Example-App"
    assert repository.default_branch == "develop"
    assert repository.workflow.merge_method == "squash"
    assert repository.workflow.required_checks == ("CI", "Build")
    assert report["repositories"][0]["name"] == repository.name


@pytest.mark.parametrize("field,values", [
    ("branches", {"example-org/example-app": "main", "EXAMPLE-ORG/EXAMPLE-APP": "develop"}),
    ("merge_methods", {"example-org/example-app": "squash", "EXAMPLE-ORG/EXAMPLE-APP": "merge"}),
    ("required_checks", {"example-org/example-app": ("CI",), "EXAMPLE-ORG/EXAMPLE-APP": ("Build",)}),
])
def test_setup_refuses_duplicate_override_aliases_before_commands(tmp_path: Path, field: str, values: dict) -> None:
    def unexpected(*_args):
        raise AssertionError("Ambiguous overrides must not execute commands")
    request = replace(SetupRequest(repositories=("example-org/example-app",), branches={}), **{field: values})
    with pytest.raises(SetupError, match="case-insensitive repository alias"):
        create_profile(tmp_path / "config.toml", request, runner=unexpected)
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("changes", [
    {"triage_model": "ignored-model"}, {"triage_reasoning": "low"},
    {"review_model": "ignored-model"}, {"review_reasoning": "low"},
    {"acknowledge_model_use": True},
    {"advisory_mode": "economy", "triage_model": "triage", "triage_reasoning": "low",
     "acknowledge_model_use": True, "review_model": "ignored-review"},
])
def test_setup_refuses_inactive_advice_choices_without_writing(tmp_path: Path, changes: dict) -> None:
    request = replace(SetupRequest(repositories=("example-org/example-app",), branches={}, offline=True), **changes)
    with pytest.raises(SetupError, match="require"):
        create_profile(tmp_path / "config.toml", request)
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("changes", [
    {"acknowledge_mutation": True},
    {"merge_methods": {"example-org/example-app": "squash"}},
    {"merge_methods": {"example-org/example-app": "invalid"}},
    {"required_checks": {"example-org/example-app": ("CI",)}},
])
def test_read_only_setup_refuses_discarded_workflow_choices(tmp_path: Path, changes: dict) -> None:
    request = replace(SetupRequest(repositories=("example-org/example-app",), branches={}), **changes)

    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid setup must fail before authentication or collection")

    with pytest.raises(SetupError, match="require autonomy approval_required"):
        create_profile(tmp_path / "config.toml", request, runner=unexpected)
    assert not (tmp_path / "config.toml").exists()
