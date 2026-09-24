from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import cli
from dependabot_watcher.config import parse_config
from dependabot_watcher.github import CollectionResult
from dependabot_watcher.inspector import inspect
from dependabot_watcher.inspector import _manifest_relationship_path
from dependabot_watcher.output import render, render_json, render_markdown, render_text


class _TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


class _RedirectedBuffer(StringIO):
    def isatty(self) -> bool:
        return False


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

    def fetch_branch_head(self, repository: str, branch: str) -> str:
        assert repository == "example-org/example-app"
        assert branch == "main"
        return "base123"

    def fetch_text_file(
        self,
        repository: str,
        path: str,
        ref: str,
        max_bytes: int,
    ) -> str:
        raise AssertionError("No requirements-file evidence is expected in this fixture.")

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


class ManifestRelationshipClient(FakeClient):
    def list_open_dependabot_alerts(self, repository: str, limit: int):
        result = super().list_open_dependabot_alerts(repository, limit)
        item = dict(result.items[0])
        item["dependency"] = {
            "package": {"name": "example-lib", "ecosystem": "pip"},
            "manifest_path": "requirements-dev.txt",
        }
        return CollectionResult(items=(item,), limit=limit)

    def fetch_pull_request(
        self,
        repository: str,
        number: int,
        file_limit: int,
    ) -> dict[str, Any]:
        details = super().fetch_pull_request(repository, number, file_limit)
        details["files"] = [{"path": "requirements.txt"}]
        return details

    def fetch_text_file(
        self,
        repository: str,
        path: str,
        ref: str,
        max_bytes: int,
    ) -> str:
        assert ref == "base123"
        files = {
            "requirements-dev.txt": "-r requirements.txt\n",
            "requirements.txt": "example-lib==1.1.0\n",
        }
        return files[path]


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
    assert report["repositories"][0]["evidence_snapshot"] == {
        "base_sha": "base123",
        "observed_at": "2026-09-19T12:00:00Z",
    }
    related_alert = report["repositories"][0]["pull_requests"][0]["related_alerts"][0]
    assert related_alert["number"] == 7
    assert related_alert["severity"] == "high"
    assert related_alert["manifest_path_matches"] is True
    assert related_alert["meets_first_patched_version"] is True
    assert related_alert["coverage_evidence"] == "direct"
    related_pull_request = report["repositories"][0]["alerts"][0][
        "package_related_open_pull_requests"
    ][0]["pull_request_evidence"]
    assert related_pull_request["number"] == 42
    assert related_pull_request["classification"] == "baseline_candidate"
    assert related_pull_request["checks"] == "passing"
    assert related_pull_request["path_assessment"]["classification"] == "dependency_only"
    assert related_pull_request["mutation_authorized"] is False
    assert all(value is False for value in report["capabilities"].values())
    assert report["repositories"][0]["pull_requests"][0]["mutation_authorized"] is False


def test_truncation_makes_inspection_partial() -> None:
    report = inspect(config(), FakeClient(truncate_prs=True))

    assert report["status"] == "partial"
    assert report["summary"]["truncation_count"] == 1


def test_manifest_inheritance_is_likely_evidence_without_changing_alert_classification() -> None:
    report = inspect(config(), ManifestRelationshipClient())

    repository = report["repositories"][0]
    alert = repository["alerts"][0]
    relationship = alert["package_related_open_pull_requests"][0]
    related_alert = repository["pull_requests"][0]["related_alerts"][0]

    assert alert["classification"] == "unresolved_without_pr"
    assert relationship["coverage_evidence"] == "likely"
    assert relationship["manifest_relationship_path"] == [
        "requirements-dev.txt",
        "requirements.txt",
    ]
    assert related_alert["coverage_evidence"] == "likely"


def test_same_manifest_relationship_preserves_provenance() -> None:
    assert _manifest_relationship_path({}, "requirements.txt", "requirements.txt") == [
        "requirements.txt"
    ]


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


def test_human_verbosity_changes_presentation_but_not_json_contract() -> None:
    report = inspect(config(), FakeClient(sensitive_history=True))

    concise = render(report, "text", verbosity="concise")
    standard = render(report, "text", verbosity="standard")
    detailed = render(report, "text", verbosity="detailed")

    assert "PR #42" not in concise
    assert "deterministic GitHub inspection (no model used)" in concise
    assert "inspect --verbosity standard" in concise
    assert "Alerts without direct open-PR coverage" in concise
    assert "PR #42" in standard
    assert "checks=passing" in detailed
    assert len(concise) < len(standard) < len(detailed)
    assert json.loads(render(report, "json", verbosity="concise")) == json.loads(
        render(report, "json", verbosity="detailed")
    )


def test_interactive_progress_is_transient_and_machine_output_stays_clean() -> None:
    interactive = _TtyBuffer()
    with cli._live_progress("Collecting evidence...", enabled=True, stream=interactive):
        assert interactive.getvalue() == "Collecting evidence..."
    assert interactive.getvalue().endswith("\r" + (" " * 22) + "\r")

    redirected = StringIO()
    with cli._live_progress("Collecting evidence...", enabled=False, stream=redirected):
        pass
    assert redirected.getvalue() == ""

    assert cli._progress_enabled("text", stdout=_TtyBuffer(), stderr=_TtyBuffer())
    assert not cli._progress_enabled(
        "text", stdout=_RedirectedBuffer(), stderr=_TtyBuffer()
    )
    assert not cli._progress_enabled(
        "text", stdout=_TtyBuffer(), stderr=_RedirectedBuffer()
    )
    assert not cli._progress_enabled("json", stdout=_TtyBuffer(), stderr=_TtyBuffer())


def test_cli_uses_profile_verbosity_and_allows_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[operation]\n"
        'report_verbosity = "concise"\n'
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "GhCliClient", lambda **kwargs: FakeClient())

    assert cli.main(["inspect", "--config", str(config_path)]) == cli.ExitCode.SUCCESS
    assert "PR #42" not in capsys.readouterr().out

    assert (
        cli.main(
            [
                "inspect",
                "--config",
                str(config_path),
                "--verbosity",
                "detailed",
            ]
        )
        == cli.ExitCode.SUCCESS
    )
    assert "PR #42" in capsys.readouterr().out


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


def test_cli_advise_defaults_to_model_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[advisory]\n"
        'mode = "off"\n'
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "GhCliClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["advise", "--config", str(config_path), "--format", "json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == cli.ExitCode.SUCCESS
    assert payload["mode"] == "advise"
    assert payload["advisory"]["status"] == "disabled"
    assert payload["capabilities"]["model_calls"] is False


def test_cli_plan_defaults_to_model_off_and_prints_recommended_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[advisory]\n"
        'mode = "off"\n'
        "[[repositories]]\n"
        'name = "example-org/example-app"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "GhCliClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["plan", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == cli.ExitCode.SUCCESS
    assert "Dependabot Watcher maintenance plan" in captured.out
    assert "Recommended Order" in captured.out
    assert "Mutation authorized: no" in captured.out
    assert captured.err == ""
