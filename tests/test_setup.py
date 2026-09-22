from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest

import dependabot_watcher.setup as setup_implementation
from dependabot_watcher import diagnostics
from dependabot_watcher.config import load_config
from dependabot_watcher.diagnostics import CommandResult
from dependabot_watcher.setup import SetupError, SetupRequest, setup


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

    report = setup(
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


def test_setup_refuses_to_overwrite_existing_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("keep me", encoding="utf-8")

    with pytest.raises(SetupError, match="will not overwrite"):
        setup(
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
        setup(
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


def test_advanced_setup_checks_provider_auth_but_never_invokes_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.toml"
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = setup(
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
    assert ("codex", "login", "status") in commands
    assert not any("exec" in command for command in commands)


def test_offline_setup_makes_no_commands_and_records_unverified_profile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        raise AssertionError("Offline setup must make no command calls")

    setup(
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

    report = setup(
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
        setup(
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
        setup(
            path,
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={"example-org/example-app": "missing"},
            ),
            runner=runner,
        )
    assert not path.exists()


def test_offline_model_setup_marks_provider_verification_skipped(tmp_path: Path) -> None:
    report = setup(
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
        setup(
            path,
            SetupRequest(
                repositories=("example-org/example-app",),
                branches={},
                offline=True,
            ),
        )

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
