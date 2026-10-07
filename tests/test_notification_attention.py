from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from dependabot_watcher.github import GitHubClient
from dependabot_watcher.reconciliation import reconcile
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewQueue
from dependabot_watcher.runtime_state import load_private_json, save_private_json
from test_reconciliation import _config, _inspection, _no_work_inspection


@pytest.fixture
def attention_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ReviewQueue, dict[str, Any]]:
    # Only remote collection is controlled; real reconciliation writes the
    # durable attempt/report that the installed notification reader consumes.
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: _inspection())
    config = _config()
    report = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state", notification_mode="desktop",
        clock=lambda: datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    return ReviewQueue(config, tmp_path / "state"), report


def read(queue: ReviewQueue, run_id: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    request = {"schema_version": 1, "request_id": "read_notice_test",
               "operation": "notification_attention", "run_id": run_id, **extra}
    outgoing = io.BytesIO()
    code = exchange(queue, io.BytesIO(json.dumps(request).encode()), outgoing)
    return code, json.loads(outgoing.getvalue())


def test_current_bound_attention_reads_without_queue_writes_or_authority(attention_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = attention_case
    before = {path: path.read_bytes() for path in queue.directory.rglob("*.json")}
    code, response = read(queue, report["run_id"])
    assert code == 0 and response["needed"] is True
    assert response["run_id"] == report["run_id"]
    assert "Open your saved review queue" in response["body"]
    assert "Ask your agent" not in response["body"]
    assert response["capabilities"] == {"github_mutations": False, "model_calls": False}
    assert set(response) == {"schema_version", "request_id", "status", "run_id", "needed", "title", "body", "capabilities"}
    assert {path: path.read_bytes() for path in queue.directory.rglob("*.json")} == before


@pytest.mark.parametrize("extra", [{"path": "/other/report.json"}, {"title": "untrusted title"},
                                  {"approval_token": "not authority"}, {"schema_version": True}])
def test_sender_cannot_select_content_paths_or_authority(
    attention_case: tuple[ReviewQueue, dict[str, Any]], extra: dict[str, Any],
) -> None:
    queue, report = attention_case
    assert read(queue, report["run_id"], **extra)[0] == 2


def test_retired_run_cannot_publish_as_current(attention_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = attention_case
    code, response = read(queue, "run_" + "0" * 20)
    assert code == 2 and response["status"] == "error"


@pytest.mark.parametrize("field,value", [("summary", {}), ("mode", "plan"),
                                        ("schema_version", True), ("mutation_authorized", True), ("notification", {"needed": True})])
def test_report_must_match_durable_attempt(
    attention_case: tuple[ReviewQueue, dict[str, Any]], field: str, value: Any,
) -> None:
    queue, report = attention_case
    changed = load_private_json(queue.directory, Path("latest-run.json"))
    changed[field] = value
    save_private_json(queue.directory, Path("latest-run.json"), changed)
    code, response = read(queue, report["run_id"])
    assert code == 2 and response["status"] == "error"


def test_unchanged_and_no_work_remain_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    for case, inspection in (("unchanged", _inspection()), ("empty", _no_work_inspection())):
        state = tmp_path / case
        monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: inspection)
        report = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
            state_directory=state, notification_mode="off", clock=lambda: now)
        if case == "unchanged":
            report = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
                state_directory=state, notification_mode="off", clock=lambda: now)
        code, response = read(ReviewQueue(config, state), report["run_id"])
        assert code == 0 and response["needed"] is False
        assert response["title"] is response["body"] is None
