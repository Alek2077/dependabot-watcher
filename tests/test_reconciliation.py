from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from dependabot_watcher.advisory import AdvisoryRequest, ProviderResponse
from dependabot_watcher.config import parse_config
from dependabot_watcher.github import GitHubClient, GitHubError
from dependabot_watcher.reconciliation import (
    ReconciliationError,
    _suppressed_notification,
    evidence_revision,
    reconcile,
    record_notification_delivery,
)
from dependabot_watcher.runtime_state import (
    RuntimeStateError,
    load_private_json,
    load_run_state,
    run_lock,
    save_private_json,
    save_run_state,
)
from dependabot_watcher.reconciliation_output import render_reconciliation
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_permissions import SupervisorPermissions
from dependabot_watcher.supervisor_status import supervision_status
from test_review_queue import decision, queue_case


def _config(
    mode: str = "off",
    *,
    triage_model: str = "test-model",
    require_advice: bool = False,
):
    advisory: dict[str, Any] = {"mode": mode}
    if mode != "off":
        advisory.update(
            {
                "provider": "codex_cli",
                "codex_cli": {
                    "triage": {"model": triage_model, "reasoning_effort": "high"},
                    "review": {"model": "test-model", "reasoning_effort": "high"},
                },
            }
        )
    return parse_config(
        {
            "schema_version": 1,
            "advisory": advisory,
            "operation": {
                "require_advice_before_execution": require_advice,
            },
            "repositories": [
                {
                    "name": "example-org/example-app",
                    "default_branch": "main",
                    "workflow": {
                        "merge_method": "squash",
                        "required_checks": ["CI"],
                    },
                }
            ],
        }
    )


def _inspection(*, generated_at: str = "2026-09-27T10:00:00Z") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "inspect",
        "generated_at": generated_at,
        "status": "complete",
        "capabilities": {
            "model_calls": False,
            "persistent_local_writes": False,
            "github_mutations": False,
            "notifications": False,
        },
        "summary": {
            "repository_count": 1,
            "open_pull_request_count": 1,
            "open_alert_count": 0,
            "attention_required_count": 0,
            "unresolved_alert_count": 0,
            "error_count": 0,
            "truncation_count": 0,
        },
        "repositories": [
            {
                "repository": "example-org/example-app",
                "expected_base": "main",
                "evidence_snapshot": {
                    "base_sha": "b" * 40,
                    "observed_at": generated_at,
                },
                "workflow_profile": {
                    "merge_method": "squash",
                    "promotion_branch": None,
                    "required_checks": ["CI"],
                    "deployment_environments": [],
                    "validation_notes": [],
                },
                "pull_requests": [
                    {
                        "candidate_identity": {
                            "repository": "example-org/example-app",
                            "number": 42,
                            "title": "Bump example-lib from 1.0 to 1.1",
                            "url": "https://github.com/example-org/example-app/pull/42",
                            "expected_base": "main",
                            "observed_base": "main",
                            "head_ref": "dependabot/pip/example-lib-1.1",
                            "head_sha": "a" * 40,
                        },
                        "updated_at": "2026-09-27T09:00:00Z",
                        "evidence_observed_at": generated_at,
                        "author": "app/dependabot",
                        "mergeable": "MERGEABLE",
                        "merge_state_status": "CLEAN",
                        "is_draft": False,
                        "checks": "passing",
                        "status_checks": [
                            {
                                "name": "CI",
                                "kind": "check_run",
                                "state": "passing",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                            }
                        ],
                        "classification": "baseline_candidate",
                        "reasons": [],
                        "path_assessment": {
                            "classification": "dependency_only",
                            "files": [
                                {
                                    "path": "requirements.txt",
                                    "categories": ["dependency_manifest"],
                                    "dependency_related": True,
                                    "sensitive": False,
                                    "classified": True,
                                }
                            ],
                            "unclassified_paths": [],
                            "sensitive_paths": [],
                        },
                        "recent_sensitive_overlap": [],
                        "related_alerts": [],
                        "mutation_authorized": False,
                    }
                ],
                "alerts": [],
                "manifest_evidence": {
                    "status": "complete",
                    "base_sha": "b" * 40,
                    "files": [
                        {
                            "path": "requirements.txt",
                            "depth": 0,
                            "includes": [],
                        }
                    ],
                    "relationships": [],
                    "errors": [],
                    "truncations": [],
                },
                "recent_sensitive_merges": [],
                "errors": [],
                "truncations": [],
                "summary": {
                    "open_pull_request_count": 1,
                    "open_alert_count": 0,
                    "attention_required_count": 0,
                    "unresolved_alert_count": 0,
                    "error_count": 0,
                    "truncation_count": 0,
                },
            }
        ],
    }


def _no_work_inspection() -> dict[str, Any]:
    report = _inspection()
    report["summary"].update(
        open_pull_request_count=0,
        open_alert_count=0,
    )
    repository = report["repositories"][0]
    repository["pull_requests"] = []
    repository["summary"].update(
        open_pull_request_count=0,
        open_alert_count=0,
    )
    return report


class _Provider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.summary = "Synthetic safe candidate."
        self.requests: list[AdvisoryRequest] = []

    def analyze(self, request: AdvisoryRequest, *, stage: str) -> ProviderResponse:
        self.requests.append(request)
        if self.fail:
            from dependabot_watcher.advisory import AdvisoryError

            raise AdvisoryError("synthetic provider failure")
        return ProviderResponse(
            payload={
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "recommended_action": "merge_candidate",
                        "risk_level": "low",
                        "confidence": "high",
                        "requires_review": False,
                        "summary": self.summary,
                        "rationale": "Synthetic evidence is complete.",
                        "evidence": ["Synthetic evidence."],
                        "unresolved_questions": [],
                    }
                    for candidate in request.candidates
                ]
            },
            provider="fake",
            model="test-model",
            reasoning_effort="high",
            duration_seconds=0.1,
            usage={"input_tokens": 20, "output_tokens": 5},
        )


def test_evidence_revision_ignores_observation_time_but_tracks_material_state() -> None:
    first = _inspection(generated_at="2026-09-27T10:00:00Z")
    later = _inspection(generated_at="2026-09-27T11:00:00Z")

    assert evidence_revision(first) == evidence_revision(later)

    later["repositories"][0]["pull_requests"][0]["candidate_identity"][
        "head_sha"
    ] = "c" * 40
    assert evidence_revision(first) != evidence_revision(later)


def test_reconcile_persists_plan_and_suppresses_unchanged_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    current = _inspection()
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: json.loads(json.dumps(current)),
    )
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    first = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now,
    )
    second = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert first["outcome"] == "action_required"
    assert first["capabilities"] == {
        "model_calls": True,
        "persistent_local_writes": True,
        "github_mutations": False,
        "notifications": False,
    }
    assert first["queue_synopsis"]["item_count"] == 1
    assert provider.requests[0].queue_synopsis is not None
    assert provider.requests[0].queue_synopsis["item_count"] == 1
    assert Path(first["plan_lineage"]["plan_path"]).is_file()
    assert first["notification"]["reason"] == "approval_ready"
    assert first["notification"]["next"]["argv"][-6:-4] == [
        "--state-dir",
        str(tmp_path / "state"),
    ]
    assert second["outcome"] == "unchanged"
    assert second["notification"]["needed"] is False
    assert second["plan"]["plan_id"] == first["plan"]["plan_id"]
    assert second["plan_lineage"] == first["plan_lineage"]
    assert first["run_id"] != second["run_id"]
    assert len(provider.requests) == 1
    state = load_run_state(tmp_path / "state")
    assert len(state["history"]) == 2
    assert state["latest_plan"]["plan_id"] == first["plan_lineage"]["plan_id"]
    first_text = render_reconciliation(first, "text")
    second_text = render_reconciliation(second, "text")
    assert "calls this run 1" in first_text
    assert "calls this run 0" in second_text
    assert "retained plan calls 1" in second_text
    first["notification"]["delivery"] = {
        "mode": "desktop",
        "status": "delivered",
        "assurance": "adapter_accepted_only",
    }
    assert "submitted to the desktop adapter (display not confirmed)" in (
        render_reconciliation(first, "text")
    )


def test_reconciliation_human_output_exposes_superseded_plan_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    report = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
    )
    report["plan_lineage"]["supersedes_plan_id"] = "plan_previous"

    text = render_reconciliation(report, "text")
    markdown = render_reconciliation(report, "markdown")

    assert "supersedes plan_previous" in text
    assert "supersedes plan_previous" in markdown


def test_reconcile_repeats_advice_when_advisory_policy_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    first = reconcile(
        _config("economy", triage_model="first-model"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now,
    )
    second = reconcile(
        _config("economy", triage_model="replacement-model"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert first["evidence_revision"] == second["evidence_revision"]
    assert second["outcome"] == "action_required"
    assert len(provider.requests) == 2


def test_reconcile_repeats_advice_when_execution_starts_requiring_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    first = reconcile(
        _config("economy", require_advice=False),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now,
    )
    second = reconcile(
        _config("economy", require_advice=True),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert first["evidence_revision"] == second["evidence_revision"]
    assert second["outcome"] == "action_required"
    assert len(provider.requests) == 2


def test_failed_provider_attempt_is_reported_as_model_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider(fail=True)
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )

    result = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
    )

    assert result["outcome"] == "partial"
    assert result["capabilities"]["model_calls"] is True
    assert result["provider_usage"]["call_count"] == 1
    assert result["provider_usage"]["successful_call_count"] == 0


def test_reconcile_sanitizes_repository_collection_errors_before_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "raw-github-error-with-sensitive-context"
    inspection = _inspection()
    inspection["status"] = "partial"
    inspection["summary"]["error_count"] = 1
    repository = inspection["repositories"][0]
    repository["errors"] = [
        {
            "repository": "example-org/example-app",
            "surface": "open_alerts",
            "message": secret,
        }
    ]
    repository["summary"]["error_count"] = 1
    repository["manifest_evidence"]["errors"] = [
        {"path": "requirements.txt", "message": secret}
    ]
    repository["manifest_evidence"]["status"] = "partial"
    repository["pull_requests"][0]["classification"] = "collection_error"
    repository["pull_requests"][0]["reasons"] = [secret]
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: inspection,
    )

    result = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
    )

    persisted = (tmp_path / "state" / "latest-run.json").read_text(encoding="utf-8")
    assert secret not in json.dumps(result)
    assert secret not in persisted
    assert result["inspection"]["repositories"][0]["errors"][0]["message"] == (
        "GitHub collection failed for open_alerts."
    )


def test_reconcile_no_work_skips_provider_and_revokes_previous_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _no_work_inspection(),
    )

    result = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
    )

    assert result["outcome"] == "no_work"
    assert result["plan"] is None
    assert result["notification"]["needed"] is False
    assert provider.requests == []
    assert load_run_state(tmp_path / "state")["latest_plan"]["revoked_reason"] == "no_work"
    # Empty is fresh evidence, not unavailable evidence; execution authority
    # remains revoked even while the supervisor can assess the exact base.
    queue = ReviewQueue(_config("economy"), tmp_path / "state")
    context = queue.supervision_context("example-org/example-app")
    assert context["evidence_status"] == "complete"
    assert context["repository_plan"]["recommended_order"] == []
    assert context["repository_plan"]["evidence_snapshot"]["base_sha"] == "b" * 40
    assert provider.requests == []
    assert load_run_state(queue.directory)["latest_plan"]["plan_id"] is None


def test_repeated_empty_collection_keeps_fresh_context_without_restoring_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspections = []
    for hour in (10, 11, 12):
        inspection = _no_work_inspection()
        timestamp = f"2026-09-27T{hour}:00:00Z"
        inspection["generated_at"] = timestamp
        inspection["repositories"][0]["evidence_snapshot"]["observed_at"] = timestamp
        inspections.append(inspection)
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: inspections.pop(0))
    config = _config()
    directory = tmp_path / "state"
    queue = ReviewQueue(config, directory)
    first_context = None
    for expected in ("no_work", "unchanged", "unchanged"):
        result = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
                           state_directory=directory)
        assert result["outcome"] == expected and result["plan"] is None
        context = queue.supervision_context(config.repositories[0].name)
        if first_context is None:
            first_context = context
        assert context == first_context
        assert context["evidence_status"] == "complete" and context["proposals"] == []
        assert context["repository_plan"]["evidence_snapshot"]["base_sha"] == "b" * 40
        assert load_run_state(directory)["latest_plan"]["plan_id"] is None


@pytest.mark.parametrize("changed", ["run_id", "revision", "base", "branch", "scope"])
def test_empty_collection_context_rejects_mismatched_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: _no_work_inspection())
    config = _config()
    directory = tmp_path / "state"
    reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml", state_directory=directory)
    report = load_private_json(directory, Path("latest-run.json"))
    if changed == "run_id":
        report["run_id"] = "run_" + "0" * 20
    elif changed == "revision":
        report["evidence_revision"] = "evidence_" + "0" * 24
    elif changed == "base":
        report["inspection"]["repositories"][0]["evidence_snapshot"]["base_sha"] = "c" * 40
    else:
        repository = config.repositories[0]
        repository = replace(repository, **({"default_branch": "other"} if changed == "branch"
                                             else {"name": "example-org/other-app"}))
        config = replace(config, repositories=(repository,))
    save_private_json(directory, Path("latest-run.json"), report)
    queue = ReviewQueue(config, directory)
    with pytest.raises(ReviewQueueError, match="Empty-queue evidence"):
        queue.supervision_context(config.repositories[0].name)
    assert load_run_state(directory)["latest_plan"]["plan_id"] is None


def test_reconcile_defers_unchanged_provider_failure_until_retry_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider(fail=True)
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    first = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now,
    )
    second = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=tmp_path / "state",
        provider=provider,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert first["outcome"] == "partial"
    assert first["notification"]["needed"] is True
    assert second["outcome"] == "retry_deferred"
    assert second["notification"]["needed"] is False
    assert len(provider.requests) == 1


def test_reconcile_persists_sanitized_github_failure_and_deduplicates_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    secret = "github-token-must-not-be-persisted"

    def fail_collection(*args: object, **kwargs: object) -> dict[str, Any]:
        raise GitHubError(f"authentication failed with {secret}")

    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", fail_collection)
    state_directory = tmp_path / "state"
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    first = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
        clock=lambda: now,
        notification_mode="desktop",
    )
    record_notification_delivery(
        state_directory,
        first["run_id"],
        {
            "mode": "desktop",
            "adapter": "windows",
            "status": "delivered",
            "attempted_at": "2026-09-27T10:00:01Z",
            "delivered_at": "2026-09-27T10:00:02Z",
            "error": None,
        },
    )
    second = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
        clock=lambda: now + timedelta(minutes=5),
        notification_mode="desktop",
    )

    persisted = (state_directory / "latest-run.json").read_text(encoding="utf-8")
    assert first["status"] == "partial"
    assert first["outcome"] == "failed"
    assert first["failure"] == {"category": "github_collection_error"}
    assert first["inspection"]["errors"][0]["category"] == "github_collection_error"
    assert secret not in persisted
    assert provider.requests == []
    assert second["notification"]["needed"] is False
    assert second["notification"]["reason"] == "unchanged"
    latest_plan = load_run_state(state_directory)["latest_plan"]
    assert latest_plan["plan_id"] is None
    assert latest_plan["revoked_reason"] == "github_collection_error"


def test_repeated_failures_preserve_cumulative_revoked_plan_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    current = _inspection()
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: current,
    )
    state_directory = tmp_path / "state"
    first = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
    )
    first_plan_id = first["plan_lineage"]["plan_id"]

    current = _inspection(generated_at="2026-09-27T10:05:00Z")
    current["repositories"][0]["pull_requests"][0]["candidate_identity"][
        "head_sha"
    ] = "c" * 40
    second = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
    )
    second_plan_id = second["plan_lineage"]["plan_id"]
    assert second_plan_id != first_plan_id

    def fail_collection(*args: object, **kwargs: object) -> dict[str, Any]:
        raise GitHubError("secret failure")

    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", fail_collection)
    reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
    )
    reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
    )

    latest = load_run_state(state_directory)["latest_plan"]
    assert first_plan_id in latest["invalidated_plan_ids"]
    assert second_plan_id in latest["invalidated_plan_ids"]
    assert latest["lineage_overflow"] is False


def test_legacy_revocation_marker_fails_closed_when_older_lineage_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    save_run_state(
        state_directory,
        {
            "schema_version": 1,
            "last_attempt": None,
            "last_success": None,
            "latest_plan": {
                "plan_id": None,
                "supersedes_plan_id": "plan_legacy_b",
                "revoked_reason": "github_collection_error",
            },
            "history": [],
        },
    )

    def fail_collection(*args: object, **kwargs: object) -> dict[str, Any]:
        raise GitHubError("secret failure")

    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", fail_collection)
    reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
    )

    latest = load_run_state(state_directory)["latest_plan"]
    assert latest["invalidated_plan_ids"] == ["plan_legacy_b"]
    assert latest["lineage_overflow"] is True


@pytest.mark.parametrize("transition", ["failure", "no_work"])
def test_legacy_successful_plan_carries_fail_closed_lineage_into_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    state_directory = tmp_path / "state"
    save_run_state(
        state_directory,
        {
            "schema_version": 1,
            "last_attempt": None,
            "last_success": None,
            "latest_plan": {
                "plan_id": "plan_legacy_current",
                "supersedes_plan_id": "plan_legacy_previous",
            },
            "history": [],
        },
    )

    if transition == "failure":
        def inspect_transition(*args: object, **kwargs: object) -> dict[str, Any]:
            raise GitHubError("secret failure")
    else:
        def inspect_transition(*args: object, **kwargs: object) -> dict[str, Any]:
            return _no_work_inspection()

    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        inspect_transition,
    )
    reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
    )

    latest = load_run_state(state_directory)["latest_plan"]
    assert latest["revoked_reason"] in {"github_collection_error", "no_work"}
    assert latest["invalidated_plan_ids"] == [
        "plan_legacy_previous",
        "plan_legacy_current",
    ]
    assert latest["lineage_overflow"] is True


def test_unexpected_provider_exception_becomes_sanitized_partial_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedFailureProvider(_Provider):
        def analyze(self, request: AdvisoryRequest, *, stage: str) -> ProviderResponse:
            raise RuntimeError("provider-secret-must-not-escape")

    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    state_directory = tmp_path / "state"

    report = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=UnexpectedFailureProvider(),
    )

    assert report["outcome"] == "partial"
    assert report["provider_usage"]["call_count"] == 1
    assert "provider-secret-must-not-escape" not in json.dumps(report)
    assert "provider-secret-must-not-escape" not in (
        state_directory / "latest-run.json"
    ).read_text(encoding="utf-8")


def test_reconcile_records_overdue_catch_up_after_missed_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _no_work_inspection(),
    )
    state_directory = tmp_path / "state"
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    first = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        clock=lambda: now,
        expected_interval_minutes=60,
    )
    second = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        clock=lambda: now + timedelta(hours=2),
        expected_interval_minutes=60,
    )

    assert first["schedule_observation"] == {
        "expected_interval_minutes": 60,
        "previous_completed_at": None,
        "elapsed_seconds": None,
        "overdue_catch_up": False,
    }
    assert second["schedule_observation"]["previous_completed_at"] == (
        "2026-09-27T10:00:00Z"
    )
    assert second["schedule_observation"]["elapsed_seconds"] == 7200
    assert second["schedule_observation"]["overdue_catch_up"] is True


def test_failed_notification_retries_without_repeating_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    state_directory = tmp_path / "state"

    first = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
        clock=lambda: now,
        notification_mode="desktop",
    )
    failed = record_notification_delivery(
        state_directory,
        first["run_id"],
        {
            "mode": "desktop",
            "adapter": "windows",
            "status": "failed",
            "attempted_at": "2026-09-27T10:00:01Z",
            "delivered_at": None,
            "error": "desktop_notification_unavailable",
        },
    )
    second = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
        clock=lambda: now + timedelta(minutes=5),
        notification_mode="desktop",
    )
    delivered = record_notification_delivery(
        state_directory,
        second["run_id"],
        {
            "mode": "desktop",
            "adapter": "windows",
            "status": "delivered",
            "attempted_at": "2026-09-27T10:05:01Z",
            "delivered_at": "2026-09-27T10:05:02Z",
            "error": None,
        },
    )
    third = reconcile(
        _config("economy"),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        provider=provider,
        clock=lambda: now + timedelta(minutes=10),
        notification_mode="desktop",
    )

    assert first["notification"]["delivery"]["status"] == "pending"
    assert failed["notification"]["delivery"]["status"] == "failed"
    assert failed["capabilities"]["notifications"] is False
    assert second["outcome"] == "unchanged"
    assert second["notification"]["needed"] is True
    assert second["notification"]["delivery"]["status"] == "pending"
    assert delivered["notification"]["delivery"]["status"] == "delivered"
    assert third["notification"]["needed"] is False
    assert len(provider.requests) == 1


def test_undelivered_notice_survives_unrelated_execution_revision_change() -> None:
    previous = {
        "execution_revision": "execution_old",
        "notification": {
            "needed": True,
            "reason": "approval_ready",
            "message": "Approval is ready.",
        },
        "notification_delivery": {
            "mode": "desktop",
            "status": "failed",
        },
    }

    result = _suppressed_notification(
        previous,
        default={"needed": False, "reason": "unchanged", "message": None},
        notification_mode="desktop",
        execution_revision="execution_new_but_safe",
    )

    assert result["needed"] is True
    assert result["reason"] == "approval_ready"


def test_notification_delivery_validates_latest_report_before_updating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    state_directory = tmp_path / "state"
    result = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        notification_mode="desktop",
    )
    report = load_private_json(state_directory, Path("latest-run.json"))
    report["run_id"] = "run_replaced"
    save_private_json(state_directory, Path("latest-run.json"), report)

    with pytest.raises(ReconciliationError, match="changed before notification"):
        record_notification_delivery(
            state_directory,
            result["run_id"],
            {
                "mode": "desktop",
                "adapter": "windows",
                "status": "delivered",
                "attempted_at": "2026-09-27T10:00:01Z",
                "delivered_at": "2026-09-27T10:00:02Z",
                "error": None,
            },
        )

    state = load_run_state(state_directory)
    assert state["last_attempt"]["notification_delivery"]["status"] == "pending"


@pytest.mark.parametrize("provide_original", [False, True])
def test_late_accepted_delivery_preserves_newer_report_and_suppresses_equivalent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provide_original: bool,
) -> None:
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: _inspection())
    config, state = _config(), tmp_path / "state"
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    first = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", clock=lambda: now)
    second = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", clock=lambda: now + timedelta(minutes=1))
    latest = (state / "latest-run.json").read_bytes()
    delivery = {"mode": "desktop", "adapter": "windows", "status": "delivered",
                "assurance": "adapter_accepted_only"}
    result = record_notification_delivery(state, first["run_id"], delivery,
        original_report=first if provide_original else None)
    assert result["run_id"] == first["run_id"]
    assert result["mode"] == ("run" if provide_original else "notification_delivery")
    assert (state / "latest-run.json").read_bytes() == latest
    history = {item["run_id"]: item for item in load_run_state(state)["history"]}
    assert history[first["run_id"]]["notification_delivery"] == delivery
    assert history[second["run_id"]]["notification_delivery"]["status"] == "pending"
    third = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", clock=lambda: now + timedelta(minutes=2))
    assert third["notification"]["needed"] is False


def test_late_delivery_does_not_suppress_changed_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inspection = _inspection()
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: inspection)
    config, state = _config(), tmp_path / "state"
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    first = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", clock=lambda: now)
    inspection = json.loads(json.dumps(inspection))
    inspection["repositories"][0]["pull_requests"][0]["candidate_identity"]["head_sha"] = "c" * 40
    reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", clock=lambda: now + timedelta(minutes=1))
    record_notification_delivery(state, first["run_id"], {"mode": "desktop", "status": "delivered"}, original_report=first)
    third = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", clock=lambda: now + timedelta(minutes=2))
    assert third["notification"]["needed"] is True


def test_late_delivery_does_not_acknowledge_changed_advice_on_same_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspection()
    inspection["status"] = "partial"
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: inspection)
    config, state, provider = _config("economy"), tmp_path / "state", _Provider()
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    first = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", provider=provider, clock=lambda: now)
    provider.summary = "Changed recommendation after further investigation."
    second = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", provider=provider, clock=lambda: now + timedelta(hours=1))
    assert first["evidence_revision"] == second["evidence_revision"]
    assert first["notification"]["reason"] == second["notification"]["reason"]
    assert first["plan"]["plan_id"] != second["plan"]["plan_id"]
    record_notification_delivery(state, first["run_id"], {"mode": "desktop", "status": "delivered"}, original_report=first)
    third = reconcile(config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
        state_directory=state, notification_mode="desktop", provider=provider, clock=lambda: now + timedelta(hours=1, minutes=1))
    assert third["notification"]["needed"] is True


def test_failed_execution_routes_one_deduplicated_recovery_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dependabot_watcher.reconciliation.inspect",
        lambda *args, **kwargs: _inspection(),
    )
    state_directory = tmp_path / "state"
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    first = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        clock=lambda: now,
        notification_mode="desktop",
    )
    record_notification_delivery(
        state_directory,
        first["run_id"],
        {
            "mode": "desktop",
            "adapter": "windows",
            "status": "failed",
            "attempted_at": "2026-09-27T10:00:01Z",
            "delivered_at": None,
            "error": "desktop_notification_unavailable",
        },
    )
    event = {
        "schema_version": 1,
        "event_id": "event_failed",
        "event_type": "execution_failed",
        "occurred_at": "2026-09-27T10:01:00Z",
        "action_id": "action_failed",
        "binding": {
            "repository": "example-org/example-app",
            "pull_request": 42,
            "head_sha": "a" * 40,
        },
        "outcome": "github_error",
        "recovery": "Create a fresh plan before retrying.",
    }
    audit_path = state_directory / "execution-audit.jsonl"
    audit_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    audit_path.chmod(0o600)

    second = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        clock=lambda: now + timedelta(minutes=5),
        notification_mode="desktop",
    )
    record_notification_delivery(
        state_directory,
        second["run_id"],
        {
            "mode": "desktop",
            "adapter": "windows",
            "status": "delivered",
            "attempted_at": "2026-09-27T10:05:01Z",
            "delivered_at": "2026-09-27T10:05:02Z",
            "error": None,
        },
    )
    third = reconcile(
        _config(),
        object(),  # type: ignore[arg-type]
        config_path=tmp_path / "config.toml",
        state_directory=state_directory,
        clock=lambda: now + timedelta(minutes=10),
        notification_mode="desktop",
    )

    assert second["outcome"] == "unchanged"
    assert second["execution_observation"]["recovery_required"] is True
    assert second["notification"]["reason"] == "action_recovery_required"
    assert third["notification"]["needed"] is False
    assert third["notification"]["reason"] == "unchanged"


def test_runtime_state_rejects_malformed_state_and_overlapping_lock(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    with run_lock(state_directory):
        with pytest.raises(RuntimeStateError, match="Saved state is busy"):
            with run_lock(state_directory):
                pass

    state_path = state_directory / "run-state.json"
    state_path.write_text("not-json", encoding="utf-8")
    state_path.chmod(0o600)
    with pytest.raises(RuntimeStateError, match="malformed or unreadable"):
        load_run_state(state_directory)


def test_remote_refresh_leaves_feedback_status_and_permission_decisions_usable(
    queue_case: tuple[ReviewQueue, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, _ = queue_case
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    jobs.start(job["job_id"])
    jobs.attach_session(job["job_id"], "session1")
    permissions = SupervisorPermissions(jobs)
    collecting, release = Event(), Event()

    def collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        collecting.set()
        assert release.wait(10), "Test did not release the controlled collection boundary"
        return _inspection()

    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", collect)
    try:
        with permissions.live_connection(job["job_id"], "session1") as connection:
            permissions.bind_turn(connection, "turn1")
            pending = permissions.request(connection, provider_request_id=1, turn_id="turn1", item_id="item1",
                digest="a" * 64, description="Synthetic scoped read", choices=["deny"])
            with ThreadPoolExecutor(max_workers=1) as worker:
                future = worker.submit(reconcile, queue.config, cast(GitHubClient, object()), config_path=tmp_path / "config.toml",
                                       state_directory=queue.directory)
                try:
                    assert collecting.wait(3)
                    permissions.decide(connection, pending["permission_id"], pending["digest"], "deny")
                    status = supervision_status(queue, {"schema_version": 1, "request_id": "read_during_refresh",
                        "operation": "supervisor_status", "repository": job["repository"]})
                    assert status["jobs"][0]["status"] == "running"
                    queue.handle(decision(queue, "feedback", message="Saved while remote collection was paused."))
                    assert permissions._load()["connections"][connection]["requests"][pending["permission_id"]]["decision"] == "deny"
                    assert not future.done()
                finally:
                    release.set()
                assert future.result(timeout=5)["status"] == "complete"
    finally:
        jobs.fail(job["job_id"], interrupted=True)
        jobs.release_owner(job["job_id"])


@pytest.mark.parametrize("changed", ["run_state", "execution"])
@pytest.mark.parametrize("failed_collection", [False, True])
def test_intervening_state_or_action_prevents_stale_collection_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str, failed_collection: bool,
) -> None:
    directory = tmp_path / "state"
    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", lambda *args, **kwargs: _inspection())
    reconcile(_config(), object(), config_path=tmp_path / "config.toml", state_directory=directory)  # type: ignore[arg-type]

    def collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Simulate another valid journal transaction while network collection
        # is outside its lock. No GitHub action actually occurs in this test.
        with run_lock(directory):
            if changed == "run_state":
                state = load_run_state(directory)
                state["latest_plan"]["recorded_at"] = "2026-09-28T10:00:00Z"
                save_run_state(directory, state)
            else:
                event = {"schema_version": 1, "event_id": "event_started", "event_type": "execution_started",
                    "occurred_at": "2026-09-28T10:00:00Z", "action_id": "action_started",
                    "binding": {"repository": "example-org/example-app", "pull_request": 42, "head_sha": "a" * 40},
                    "outcome": "pending"}
                audit = directory / "execution-audit.jsonl"
                audit.write_text(json.dumps(event) + "\n", encoding="utf-8")
                audit.chmod(0o600)
        if failed_collection:
            raise GitHubError("PRIVATE_TEST_ONLY")
        return _inspection()

    monkeypatch.setattr("dependabot_watcher.reconciliation.inspect", collect)
    before_report = (directory / "latest-run.json").read_bytes()
    with pytest.raises(ReconciliationError, match="changed during collection"):
        reconcile(_config(), object(), config_path=tmp_path / "config.toml", state_directory=directory)  # type: ignore[arg-type]
    assert (directory / "latest-run.json").read_bytes() == before_report
    assert load_run_state(directory)["latest_plan"]["plan_id"] is not None
    assert len(load_run_state(directory)["history"]) == 1
