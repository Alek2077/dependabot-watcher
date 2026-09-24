from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dependabot_watcher import cli
from dependabot_watcher.config import ConfigError, load_config
from dependabot_watcher.operational_output import render_operational


def test_cli_offline_setup_creates_safe_profile_without_external_calls(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "config.toml"

    def unexpected_doctor(*args, **kwargs):
        raise AssertionError("Setup must not route through doctor")

    monkeypatch.setattr(cli, "doctor", unexpected_doctor)

    exit_code = cli.main(
        [
            "setup",
            "--config",
            str(path),
            "--repo",
            "example-org/example-app",
            "--branch",
            "example-org/example-app=develop",
            "--offline",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    config = load_config(path)
    assert exit_code == cli.ExitCode.SUCCESS
    assert captured.err == ""
    assert report["mode"] == "setup"
    assert report["capabilities"]["model_calls"] is False
    assert report["capabilities"]["github_mutations"] is False
    assert report["capabilities"]["persistent_local_writes"] is True
    assert config.repositories[0].default_branch == "develop"
    assert config.operation.mutation_enabled is False
    assert "Verification: offline; GitHub access was not verified" in render_operational(
        report, "text"
    )
    assert "Verification: offline; GitHub access was not verified" in render_operational(
        report, "markdown"
    )


def test_cli_execute_without_token_runs_read_only_preflight(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    saved_plan = {"saved": True}
    observed: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: saved_plan)
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())

    def preflight(config_arg, client_arg, plan_arg, step_id):
        observed.update(
            config=config_arg,
            plan=plan_arg,
            step_id=step_id,
        )
        return {
            "mode": "execute_preflight",
            "status": "ready",
            "binding": {
                "repository": "example-org/example-app",
                "pull_request": 42,
                "head_sha": "a" * 40,
                "merge_method": "squash",
            },
            "action_id": "action_test",
            "mutation_performed": False,
            "approval_token": "approve_test",
            "next": "Approve exactly this action.",
        }

    monkeypatch.setattr(cli, "preflight_execution", preflight)

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert observed == {"config": config, "plan": saved_plan, "step_id": "step_exact"}
    assert "Mutation performed: no" in captured.out
    assert "approve_test" in captured.out


def test_cli_execute_already_applied_shows_next_step(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: {"saved": True})
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    monkeypatch.setattr(
        cli,
        "preflight_execution",
        lambda *args: {
            "mode": "execute_preflight",
            "status": "already_applied",
            "binding": {
                "repository": "example-org/example-app",
                "pull_request": 42,
                "head_sha": "a" * 40,
                "merge_method": "squash",
            },
            "action_id": "action_test",
            "mutation_performed": False,
            "approval_token": None,
            "next": "No action is needed; GitHub reports this exact head as already merged.",
        },
    )

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert "Status: already_applied" in captured.out
    assert "Next: No action is needed" in captured.out


def test_cli_execute_with_token_routes_approved_execution(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: {"saved": True})
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())

    def execute(config_arg, client_arg, plan_arg, step_id, token):
        observed.update(step_id=step_id, token=token)
        return {
            "schema_version": 1,
            "mode": "execute",
            "status": "merged",
            "mutation_performed": True,
            "mutation_authorized": True,
            "binding": {
                "repository": "example-org/example-app",
                "pull_request": 42,
                "head_sha": "a" * 40,
                "merge_method": "squash",
            },
            "action_id": "action_test",
        }

    monkeypatch.setattr(cli, "execute_approved", execute)

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
            "--approval-token",
            "approve_exact",
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert observed == {"step_id": "step_exact", "token": "approve_exact"}
    assert json.loads(captured.out)["status"] == "merged"


def test_cli_execute_reports_execution_error_with_specific_exit_code(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    plan_path = tmp_path / "plan.json"
    config_path.write_text("placeholder", encoding="utf-8")
    plan_path.write_text("{}", encoding="utf-8")
    config = SimpleNamespace(collection=SimpleNamespace(command_timeout_seconds=120))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "load_plan", lambda path: {"saved": True})
    monkeypatch.setattr(cli, "GhCliClient", lambda timeout_seconds: object())
    monkeypatch.setattr(
        cli,
        "preflight_execution",
        lambda *args: (_ for _ in ()).throw(cli.ExecutionError("stale exact action")),
    )

    exit_code = cli.main(
        [
            "execute",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
            "--step-id",
            "step_exact",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.EXECUTION_ERROR
    assert captured.out == ""
    assert "Execution error: stale exact action" in captured.err


def test_cli_setup_can_create_explicit_approval_gated_profile(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "config.toml"

    exit_code = cli.main(
        [
            "setup",
            "--config",
            str(path),
            "--repo",
            "example-org/example-app",
            "--branch",
            "example-org/example-app=main",
            "--autonomy",
            "approval_required",
            "--merge-method",
            "example-org/example-app=squash",
            "--required-check",
            "example-org/example-app=CI",
            "--acknowledge-mutation",
            "--offline",
            "--format",
            "json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    config = load_config(path)
    assert exit_code == cli.ExitCode.SUCCESS
    assert report["operation"]["autonomy"] == "approval_required"
    assert config.operation.mutation_enabled is True
    assert config.repositories[0].workflow.merge_method == "squash"
    assert config.repositories[0].workflow.required_checks == ("CI",)


def test_cli_rejects_required_check_boundary_whitespace() -> None:
    with pytest.raises(ConfigError, match="OWNER/REPO=CHECK"):
        cli._required_check_overrides(["example-org/example-app= CI"])


def test_cli_doctor_uses_partial_exit_for_attention_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "missing.toml"
    monkeypatch.setattr(
        cli,
        "doctor",
        lambda config_path, offline: {
            "schema_version": 1,
            "mode": "doctor",
            "generated_at": "2026-09-22T12:00:00Z",
            "status": "attention",
            "offline": offline,
            "capabilities": {
                "model_calls": False,
                "persistent_local_writes": False,
                "github_mutations": False,
                "notifications": False,
                "scheduling": False,
            },
            "runtime": {
                "platform": "linux",
                "platform_release": "test",
                "python": "3.13.0",
                "python_supported": True,
            },
            "configuration": {
                "path": str(config_path),
                "status": "missing",
                "schema_version": None,
            },
            "state": {"persistent_runtime_state": "not_used", "path": None},
            "tools": {
                "github_cli": {
                    "availability": "available",
                    "authentication": "unknown",
                    "version": "test",
                },
                "codex_cli": {
                    "availability": "available",
                    "authentication": "unknown",
                    "version": "test",
                },
            },
            "deployment_adapters": {"schedulers": [], "notifications": []},
            "repositories": [],
            "findings": [
                {
                    "severity": "warning",
                    "code": "configuration_missing",
                    "message": "Configuration is missing.",
                }
            ],
        },
    )

    exit_code = cli.main(
        ["doctor", "--config", str(path), "--offline", "--format", "json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == cli.ExitCode.PARTIAL_INSPECTION
    assert report["status"] == "attention"


def test_doctor_human_outputs_include_actionable_error_detail() -> None:
    report = {
        "mode": "doctor",
        "status": "blocked",
        "configuration": {
            "status": "invalid",
            "path": "/tmp/config.toml",
            "error": "Unknown operation key: unsafe.",
        },
        "runtime": {"platform": "linux", "python": "3.13.0"},
        "tools": {
            "github_cli": {
                "availability": "available",
                "authentication": "available",
                "version": "test",
            },
            "codex_cli": {
                "availability": "available",
                "authentication": "not_required",
                "version": "test",
            },
        },
        "repositories": [],
        "findings": [
            {
                "severity": "error",
                "code": "configuration_invalid",
                "message": "The resolved configuration is invalid.",
                "next_step": "Correct the configuration.",
            }
        ],
    }

    text = render_operational(report, "text")
    markdown = render_operational(report, "markdown")

    assert "Unknown operation key: unsafe." in text
    assert "Next: Correct the configuration." in text
    assert "Unknown operation key: unsafe." in markdown
    assert "Next: Correct the configuration." in markdown
    assert "not checked (advice is off)" in text


def test_cli_setup_defaults_to_human_text_and_existing_path_fails_cleanly(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "config.toml"
    arguments = [
        "setup",
        "--config",
        str(path),
        "--repo",
        "example-org/example-app",
        "--offline",
    ]

    assert cli.main(arguments) == cli.ExitCode.SUCCESS
    first = capsys.readouterr()
    assert "Dependabot Watcher setup" in first.out
    assert "Verification: offline" in first.out

    assert cli.main(arguments) == cli.ExitCode.CONFIGURATION_ERROR
    second = capsys.readouterr()
    assert second.out == ""
    assert "will not overwrite" in second.err
