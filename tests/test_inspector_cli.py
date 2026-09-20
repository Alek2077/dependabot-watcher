from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import cli
from dependabot_watcher.config import parse_config
from dependabot_watcher.github import CollectionResult
from dependabot_watcher.inspector import inspect
from dependabot_watcher.output import render_json, render_markdown, render_text


class FakeClient:
    def __init__(
        self,
        *,
        truncate_prs: bool = False,
        truncate_history: bool = False,
        sensitive_history: bool = False,
    ) -> None:
        self.auth_checked = False
        self.truncate_prs = truncate_prs
        self.truncate_history = truncate_history
        self.sensitive_history = sensitive_history

    def validate_auth(self) -> None:
        self.auth_checked = True

    def list_open_dependabot_prs(self, repository: str, limit: int):
        return CollectionResult(
            items=(
                {
                    "number": 42,
                    "title": "Bump example-lib from 1.0.0 to 1.1.0",
                    "url": "https://github.com/example-org/example-app/pull/42",
                    "baseRefName": "main",
                    "headRefName": "dependabot/pip/example-lib-1.1.0",
                    "headRefOid": "abc123",
                },
            ),
            truncated=self.truncate_prs,
            limit=limit,
        )

    def list_open_dependabot_alerts(self, repository: str, limit: int):
        return CollectionResult(
            items=(
                {
                    "number": 7,
                    "state": "open",
                    "dependency": {
                        "package": {"name": "example-lib", "ecosystem": "pip"},
                        "manifest_path": "pyproject.toml",
                    },
                    "security_vulnerability": {
                        "severity": "high",
                        "first_patched_version": {"identifier": "1.1.0"},
                    },
                },
            ),
            limit=limit,
        )

    def fetch_pull_request(
        self,
        repository: str,
        number: int,
        file_limit: int,
    ) -> dict[str, Any]:
        assert file_limit == 500
        return {
            "number": number,
            "title": "Bump example-lib from 1.0.0 to 1.1.0",
            "url": "https://github.com/example-org/example-app/pull/42",
            "updatedAt": "2026-09-19T10:00:00Z",
            "headRefName": "dependabot/pip/example-lib-1.1.0",
            "headRefOid": "abc123",
            "baseRefName": "main",
            "author": {"login": "app/dependabot"},
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "files": [{"path": "pyproject.toml"}, {"path": "uv.lock"}],
        }

    def list_recent_merged_prs(
        self,
        repository: str,
        *,
        lookback_days: int,
        limit: int,
        file_limit: int,
    ):
        assert file_limit == 500
        items: tuple[dict[str, Any], ...] = ()
        if self.sensitive_history:
            items = (
                {
                    "number": 9,
                    "title": "Revise release workflow",
                    "url": "https://github.com/example-org/example-app/pull/9",
                    "mergedAt": "2026-09-18T12:00:00Z",
                    "author": {"login": "maintainer"},
                    "files": [{"path": ".github/workflows/release.yml"}],
                },
            )
        return CollectionResult(items=items, truncated=self.truncate_history, limit=limit)


def config():
    return parse_config(
        {
            "schema_version": 1,
            "repositories": [{"name": "example-org/example-app", "default_branch": "main"}],
        }
    )


def test_inspection_is_complete_and_explicitly_non_mutating() -> None:
    client = FakeClient()

    report = inspect(
        config(),
        client,
        clock=lambda: datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert client.auth_checked is True
    assert report["generated_at"] == "2026-09-19T12:00:00Z"
    assert report["status"] == "complete"
    assert report["summary"]["open_pull_request_count"] == 1
    assert report["repositories"][0]["alerts"][0]["classification"] == "likely_covered_by_open_pr"
    assert all(value is False for value in report["capabilities"].values())
    assert report["repositories"][0]["pull_requests"][0]["mutation_authorized"] is False


def test_truncation_makes_inspection_partial() -> None:
    report = inspect(config(), FakeClient(truncate_prs=True))

    assert report["status"] == "partial"
    assert report["summary"]["truncation_count"] == 1


def test_incomplete_sensitive_history_blocks_baseline_candidate() -> None:
    report = inspect(config(), FakeClient(truncate_history=True))

    pull_request = report["repositories"][0]["pull_requests"][0]
    assert report["status"] == "partial"
    assert pull_request["classification"] == "attention_required"
    assert "history is incomplete" in " ".join(pull_request["reasons"])


def test_all_output_formats_include_core_evidence() -> None:
    report = inspect(config(), FakeClient(sensitive_history=True))

    assert json.loads(render_json(report))["schema_version"] == 1
    assert "Dependabot Watcher inspection" in render_text(report)
    assert "example-org/example-app" in render_text(report)
    assert "Recent sensitive non-Dependabot merges" in render_text(report)
    assert ".github/workflows/release.yml" in render_text(report)
    assert "# Dependabot Watcher inspection" in render_markdown(report)
    assert "| #42 |" in render_markdown(report)
    assert "### Recent sensitive non-Dependabot merges" in render_markdown(report)
    assert ".github/workflows/release.yml" in render_markdown(report)


def test_cli_json_success_uses_stdout_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    fake = FakeClient()
    monkeypatch.setattr(cli, "GhCliClient", lambda **kwargs: fake)

    exit_code = cli.main(["inspect", "--config", str(config_path), "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert json.loads(captured.out)["status"] == "complete"
    assert captured.err == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def test_cli_partial_exit_code_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[collection]\n"
        "max_open_prs = 1\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "GhCliClient", lambda **kwargs: FakeClient(truncate_prs=True))

    exit_code = cli.main(["inspect", "--config", str(config_path)])

    assert exit_code == cli.ExitCode.PARTIAL_INSPECTION
    assert "TRUNCATED" in capsys.readouterr().out


def test_cli_rejects_unconfigured_repo_before_github_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "GhCliClient",
        lambda **kwargs: pytest.fail("GitHub client should not be created"),
    )

    exit_code = cli.main(
        ["inspect", "--config", str(config_path), "--repo", "other-org/other-app"]
    )

    assert exit_code == cli.ExitCode.CONFIGURATION_ERROR
    assert "not configured" in capsys.readouterr().err
