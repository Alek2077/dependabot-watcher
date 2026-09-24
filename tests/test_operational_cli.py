from __future__ import annotations

import json
from pathlib import Path

from dependabot_watcher import cli
from dependabot_watcher.config import load_config
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
