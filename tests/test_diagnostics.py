from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from dependabot_watcher import diagnostics
from dependabot_watcher.diagnostics import CommandResult, doctor


def _config(path: Path, *, advisory_mode: str = "off") -> None:
    advisory = f'[advisory]\nmode = "{advisory_mode}"\n'
    if advisory_mode != "off":
        advisory += (
            'provider = "codex_cli"\n'
            "[advisory.codex_cli.triage]\n"
            'model = "test-model"\n'
            'reasoning_effort = "high"\n'
        )
    path.write_text(
        "schema_version = 1\n"
        + advisory
        + "[[repositories]]\n"
        + 'name = "example-org/example-app"\n'
        + 'default_branch = "main"\n',
        encoding="utf-8",
    )


def test_doctor_reports_redacted_read_only_readiness_and_support_depth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)
    commands: list[tuple[str, ...]] = []
    dependabot_yaml = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: pip\n    directory: /\n    schedule: {interval: weekly}\n"
        "  - package-ecosystem: npm\n    directory: /web\n    schedule: {interval: weekly}\n"
    )

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        commands.append(tuple(command))
        if command[:2] == ["gh", "--version"]:
            return CommandResult(0, "gh version 2.92.0\n")
        if command[:2] == ["codex", "--version"]:
            return CommandResult(0, "codex-cli 0.155.0\n")
        if command[:3] in (["gh", "auth", "status"], ["codex", "login", "status"]):
            return CommandResult(0, "private account detail that must not be reported")
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
            if "/commits/" in command[2]:
                return CommandResult(0, json.dumps({"sha": "a" * 40}))
            encoded = base64.b64encode(dependabot_yaml.encode()).decode()
            return CommandResult(
                0,
                json.dumps(
                    {
                        "type": "file",
                        "size": len(dependabot_yaml.encode()),
                        "encoding": "base64",
                        "content": encoded,
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(
        config_path,
        runner=runner,
        clock=lambda: datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert report["status"] == "ready"
    assert report["repositories"][0]["ecosystems"] == ["pip", "npm"]
    support = report["repositories"][0]["support"]
    assert support["pip"]["deeper_deterministic_analysis"] == "partial_python_requirements"
    assert support["npm"]["deeper_deterministic_analysis"] == "generic_only"
    assert all(value is False for value in report["capabilities"].values())
    assert report["capabilities"]["github_mutations"] is False
    assert report["capabilities"]["persistent_local_writes"] is False
    assert report["state"]["persistent_runtime_state"] == "not_used"
    assert report["deployment_adapters"] == {"schedulers": [], "notifications": []}
    assert "private account detail" not in json.dumps(report)
    assert not any("exec" in command for command in commands)
    assert not any(
        mutation in command
        for command in commands
        for mutation in ("merge", "close", "comment", "edit")
    )
    assert ("codex", "login", "status") not in commands


def test_offline_doctor_skips_auth_and_repository_calls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0, f"{command[0]} version test\n")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, offline=True, runner=runner)

    assert report["status"] == "attention"
    assert report["repositories"][0]["access"] == "unknown"
    assert all(command[1:] == ("--version",) for command in commands)


def test_doctor_reports_approval_gated_audit_state_without_writing_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "state"
    config_path.write_text(
        "schema_version = 1\n"
        "[operation]\n"
        'autonomy = "approval_required"\n'
        "mutation_enabled = true\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n'
        'default_branch = "main"\n'
        "[repositories.workflow]\n"
        'merge_method = "squash"\n'
        'required_checks = ["CI"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DEPENDABOT_WATCHER_STATE_DIR", str(state_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = doctor(
        config_path,
        offline=True,
        runner=lambda command, timeout: CommandResult(0, f"{command[0]} version test\n"),
    )

    assert report["state"] == {
        "persistent_runtime_state": "approval_gated_execution_audit",
        "path": str(state_path),
    }
    assert not state_path.exists()


def test_doctor_reports_regular_file_execution_state_without_crashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "state"
    config_path.write_text(
        "schema_version = 1\n"
        "[operation]\n"
        'autonomy = "approval_required"\n'
        "mutation_enabled = true\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n'
        'default_branch = "main"\n'
        "[repositories.workflow]\n"
        'merge_method = "squash"\n'
        'required_checks = ["CI"]\n',
        encoding="utf-8",
    )
    state_path.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("DEPENDABOT_WATCHER_STATE_DIR", str(state_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = doctor(
        config_path,
        offline=True,
        runner=lambda command, timeout: CommandResult(0, f"{command[0]} version test\n"),
    )

    assert report["status"] == "blocked"
    assert any(
        finding["code"] == "execution_state_permissions"
        for finding in report["findings"]
    )


def test_doctor_reports_malformed_execution_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "state"
    config_path.write_text(
        "schema_version = 1\n"
        "[operation]\n"
        'autonomy = "approval_required"\n'
        "mutation_enabled = true\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n'
        'default_branch = "main"\n'
        "[repositories.workflow]\n"
        'merge_method = "squash"\n'
        'required_checks = ["CI"]\n',
        encoding="utf-8",
    )
    state_path.mkdir(mode=0o700)
    audit_path = state_path / "execution-audit.jsonl"
    audit_path.write_text("not-json\n", encoding="utf-8")
    audit_path.chmod(0o600)
    monkeypatch.setenv("DEPENDABOT_WATCHER_STATE_DIR", str(state_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = doctor(
        config_path,
        offline=True,
        runner=lambda command, timeout: CommandResult(0, f"{command[0]} version test\n"),
    )

    assert report["status"] == "blocked"
    assert any(
        finding["code"] == "execution_audit_invalid"
        for finding in report["findings"]
    )


def test_doctor_reports_non_utf8_execution_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "state"
    config_path.write_text(
        "schema_version = 1\n"
        "[operation]\n"
        'autonomy = "approval_required"\n'
        "mutation_enabled = true\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n'
        'default_branch = "main"\n'
        "[repositories.workflow]\n"
        'merge_method = "squash"\n'
        'required_checks = ["CI"]\n',
        encoding="utf-8",
    )
    state_path.mkdir(mode=0o700)
    audit_path = state_path / "execution-audit.jsonl"
    audit_path.write_bytes(b"\xff\xfe\x00")
    audit_path.chmod(0o600)
    monkeypatch.setenv("DEPENDABOT_WATCHER_STATE_DIR", str(state_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")

    report = doctor(
        config_path,
        offline=True,
        runner=lambda command, timeout: CommandResult(0, f"{command[0]} version test\n"),
    )

    assert report["status"] == "blocked"
    assert any(
        finding["code"] == "execution_audit_invalid"
        for finding in report["findings"]
    )


def test_doctor_suppresses_auth_failure_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        if command[1:] == ["--version"]:
            return CommandResult(0, f"{command[0]} version test\n")
        return CommandResult(1, stderr="token=must-not-escape")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, runner=runner)

    assert report["status"] == "blocked"
    assert "must-not-escape" not in json.dumps(report)


def test_doctor_rejects_repository_identity_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        if command[1:] == ["--version"]:
            return CommandResult(0, f"{command[0]} version test\n")
        if command[:3] in (["gh", "auth", "status"], ["codex", "login", "status"]):
            return CommandResult(0)
        if command[:3] == ["gh", "repo", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "nameWithOwner": "other-org/other-app",
                        "defaultBranchRef": {"name": "main"},
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, runner=runner)

    assert report["status"] == "blocked"
    assert report["repositories"][0]["error"] == "repository_identity_mismatch"


def test_doctor_reports_malformed_default_branch_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)

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
                        "defaultBranchRef": "malformed",
                    }
                ),
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, runner=runner)

    assert report["status"] == "blocked"
    assert report["repositories"][0]["error"] == "repository_metadata_invalid"


def test_missing_configuration_is_actionable_not_created(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    report = doctor(config_path, offline=True)

    assert report["configuration"]["status"] == "missing"
    assert report["status"] == "attention"
    assert not config_path.exists()


def test_doctor_falls_back_to_yaml_and_reads_configured_branch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'default_branch = "main"',
            'default_branch = "develop"',
        ),
        encoding="utf-8",
    )
    endpoints: list[str] = []
    dependabot_yaml = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: pip\n"
        "    directory: /\n"
        "    schedule: {interval: weekly}\n"
    )

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
            endpoints.append(command[2])
            if "/commits/develop" in command[2]:
                return CommandResult(0, json.dumps({"sha": "b" * 40}))
            if "dependabot.yml" in command[2]:
                return CommandResult(1)
            if "dependabot.yaml" in command[2]:
                encoded = base64.b64encode(dependabot_yaml.encode()).decode()
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "type": "file",
                            "size": len(dependabot_yaml.encode()),
                            "encoding": "base64",
                            "content": encoded,
                        }
                    ),
                )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, runner=runner)

    repository = report["repositories"][0]
    assert report["status"] == "attention"
    assert repository["configured_branch_access"] == "available"
    assert repository["dependabot_configuration_path"] == ".github/dependabot.yaml"
    assert repository["ecosystems"] == ["pip"]
    assert all("ref=develop" in endpoint for endpoint in endpoints if "/contents/" in endpoint)


def test_doctor_reports_unavailable_dependabot_configuration_as_attention(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _config(config_path)

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
        if command[:2] == ["gh", "api"] and "/commits/" in command[2]:
            return CommandResult(0, json.dumps({"sha": "c" * 40}))
        if command[:2] == ["gh", "api"]:
            return CommandResult(1)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, runner=runner)

    assert report["status"] == "attention"
    assert report["repositories"][0]["dependabot_configuration"] == "unavailable"
    assert any(
        finding["code"] == "dependabot_configuration_unavailable"
        for finding in report["findings"]
    )


def test_dependabot_yaml_is_bounded_and_requires_operational_update_fields() -> None:
    nested = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: pip\n"
        "    directory: /\n"
        "    schedule:\n"
        "      interval: weekly\n"
        "      nested: "
        + "[" * 100
        + "0"
        + "]" * 100
        + "\n"
    )
    ecosystems, error = diagnostics._dependabot_ecosystems(nested)
    assert ecosystems == ()
    assert error == "invalid_yaml"

    ecosystems, error = diagnostics._dependabot_ecosystems(
        "version: 2\nupdates:\n  - package-ecosystem: pip\n"
    )
    assert ecosystems == ()
    assert error == "directory_configuration_missing_or_invalid"

    ecosystems, error = diagnostics._dependabot_ecosystems(
        "version: 2.0\nupdates: []\n"
    )
    assert ecosystems == ()
    assert error == "unsupported_or_missing_version"

    ecosystems, error = diagnostics._dependabot_ecosystems(
        "version: 2\n"
        "updates:\n"
        "  - package-ecosystem: pip\n"
        "    package-ecosystem: npm\n"
        "    directory: /\n"
        "    schedule: {interval: weekly}\n"
    )
    assert ecosystems == ()
    assert error == "invalid_yaml"


def test_invalid_configuration_blocks_doctor_without_executing_configured_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[advisory]\n"
        'mode = "off"\n'
        "[advisory.codex_cli]\n"
        'executable = "touch"\n'
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], timeout: int) -> CommandResult:
        commands.append(tuple(command))
        if command[1:] == ["--version"]:
            return CommandResult(0, f"{command[0]} version test\n")
        if command[:3] == ["gh", "auth", "status"]:
            return CommandResult(0)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(diagnostics.shutil, "which", lambda executable: f"/bin/{executable}")
    report = doctor(config_path, runner=runner)

    assert report["status"] == "blocked"
    assert report["configuration"]["status"] == "invalid"
    assert all(command[0] != "touch" for command in commands)
