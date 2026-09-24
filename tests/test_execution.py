from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.config import AdvisoryConfig, parse_config
from dependabot_watcher.execution import (
    ExecutionError,
    approval_token,
    execute_approved,
    load_plan,
    preflight_execution,
)
from dependabot_watcher.github import GitHubError
from dependabot_watcher.output import render_execution
from dependabot_watcher.planning import (
    _overall_plan_identifier,
    _repository_plan_identifier,
    _step_identifier,
)


HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


def execution_config(*, required_checks: list[str] | None = None) -> Any:
    return parse_config(
        {
            "schema_version": 1,
            "operation": {
                "autonomy": "approval_required",
                "mutation_enabled": True,
            },
            "repositories": [
                {
                    "name": "example-org/example-app",
                    "default_branch": "main",
                    "workflow": {
                        "merge_method": "squash",
                        "required_checks": required_checks or ["CI"],
                    },
                }
            ],
        }
    )


def saved_plan() -> dict[str, Any]:
    evidence_snapshot = {"base_sha": BASE_SHA}
    alert_snapshot = {"digest": "d" * 64}
    step_core = {
        "kind": "pull_request",
        "action": "validate_then_merge",
        "deterministic_classification": "baseline_candidate",
        "blockers": [],
        "identity": {
            "repository": "example-org/example-app",
            "number": 42,
            "expected_base": "main",
            "observed_base": "main",
            "head_sha": HEAD_SHA,
        },
        "freshness": {
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "alert_snapshot_digest": alert_snapshot["digest"],
        },
        "status_checks": [
            {
                "name": "CI",
                "state": "passing",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
        "prerequisites": [],
        "conflicts": [],
        "mutation_authorized": False,
    }
    repository_shell = {
        "evidence_snapshot": evidence_snapshot,
        "alert_snapshot": alert_snapshot,
    }
    step = {
        "step_id": _step_identifier(
            "example-org/example-app", repository_shell, step_core
        ),
        **step_core,
        "order": 1,
    }
    repository_core = {
        "repository": "example-org/example-app",
        "status": "complete",
        "evidence_snapshot": evidence_snapshot,
        "alert_snapshot": alert_snapshot,
        "workflow_profile": {
            "merge_method": "squash",
            "promotion_branch": None,
            "required_checks": ["CI"],
            "deployment_environments": [],
            "validation_notes": [],
        },
        "recommended_order": [step],
        "mutation_authorized": False,
    }
    repository_plan_id = _repository_plan_identifier(repository_core)
    return {
        "schema_version": 1,
        "mode": "plan",
        "plan_id": _overall_plan_identifier("complete", [repository_plan_id]),
        "status": "complete",
        "mutation_authorized": False,
        "repositories": [{"plan_id": repository_plan_id, **repository_core}],
    }


def refresh_plan_identities(report: dict[str, Any]) -> dict[str, Any]:
    repository = report["repositories"][0]
    step = repository["recommended_order"][0]
    step_core = dict(step)
    step_core.pop("step_id")
    step_core.pop("order", None)
    step_core["prerequisites"] = []
    step_core["conflicts"] = []
    step["step_id"] = _step_identifier(
        repository["repository"], repository, step_core
    )
    repository_core = dict(repository)
    repository_core.pop("plan_id")
    repository["plan_id"] = _repository_plan_identifier(repository_core)
    report["plan_id"] = _overall_plan_identifier(
        report["status"], [repository["plan_id"]]
    )
    return report


def plan_step_id(report: dict[str, Any]) -> str:
    return str(report["repositories"][0]["recommended_order"][0]["step_id"])


class ExecutionClient:
    def __init__(self, *, state: str = "OPEN", merge_error: bool = False) -> None:
        self.state = state
        self.merge_error = merge_error
        self.merges: list[dict[str, Any]] = []
        self.fetches = 0

    def fetch_pull_request_state(self, repository: str, number: int) -> dict[str, Any]:
        self.fetches += 1
        return {
            "number": number,
            "state": self.state,
            "mergedAt": "2026-09-24T12:00:00Z" if self.state == "MERGED" else None,
            "headRefOid": HEAD_SHA,
            "baseRefName": "main",
            "url": f"https://github.com/{repository}/pull/{number}",
        }

    def merge_pull_request(
        self,
        repository: str,
        number: int,
        *,
        head_sha: str,
        merge_method: str,
    ) -> dict[str, Any]:
        self.merges.append(
            {
                "repository": repository,
                "number": number,
                "head_sha": head_sha,
                "merge_method": merge_method,
            }
        )
        if self.merge_error:
            raise GitHubError("simulated protected-branch rejection")
        self.state = "MERGED"
        return {
            "merged": True,
            "message": "merged",
            "merge_commit_sha": "c" * 40,
        }


def test_load_plan_requires_complete_non_authorizing_json(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(saved_plan()), encoding="utf-8")
    assert load_plan(path)["status"] == "complete"

    malformed = saved_plan()
    malformed["mutation_authorized"] = True
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ExecutionError, match="invalid mutation authority"):
        load_plan(path)

    tampered = saved_plan()
    tampered["repositories"][0]["recommended_order"][0]["action"] = "hold"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExecutionError, match="identity validation failed"):
        load_plan(path)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe\x00",
        (b"[" * 2_000) + (b"]" * 2_000),
        b'{"value":' + (b"9" * 5_000) + b"}",
    ],
    ids=("non_utf8", "deep_json", "oversized_integer"),
)
def test_load_plan_normalizes_pathological_json_errors(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "plan.json"
    path.write_bytes(payload)

    with pytest.raises(ExecutionError, match="valid JSON|Could not read"):
        load_plan(path)


def test_preflight_rechecks_and_binds_exact_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)

    step_id = plan_step_id(report)
    result = preflight_execution(execution_config(), ExecutionClient(), report, step_id)

    assert result["status"] == "ready"
    assert result["mutation_performed"] is False
    assert result["mutation_authorized"] is False
    assert result["approval_token"] == approval_token(result["binding"])
    assert result["binding"] == {
        "repository_plan_id": report["repositories"][0]["plan_id"],
        "step_id": step_id,
        "repository": "example-org/example-app",
        "pull_request": 42,
        "expected_base": "main",
        "head_sha": HEAD_SHA,
        "merge_method": "squash",
    }


def test_already_applied_human_output_explains_that_no_action_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)

    result = preflight_execution(
        execution_config(),
        ExecutionClient(state="MERGED"),
        report,
        plan_step_id(report),
    )

    assert result["approval_token"] is None
    assert "No action is needed" in render_execution(result, "text")
    assert "No action is needed" in render_execution(result, "markdown")


def test_execution_reinspection_is_model_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    observed_modes: list[str] = []

    def fresh_plan(config, client):
        observed_modes.append(config.advisory.mode)
        return report

    monkeypatch.setattr("dependabot_watcher.execution.plan", fresh_plan)
    config = replace(execution_config(), advisory=AdvisoryConfig(mode="reviewed"))

    preflight_execution(config, ExecutionClient(), report, plan_step_id(report))

    assert observed_modes == ["off"]


def test_preflight_rejects_changed_material_repository_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    changed = saved_plan()
    changed["repositories"][0]["recommended_order"][0]["status_checks"][0][
        "conclusion"
    ] = "NEUTRAL"
    refresh_plan_identities(changed)
    monkeypatch.setattr(
        "dependabot_watcher.execution.plan",
        lambda config, client: changed,
    )

    with pytest.raises(ExecutionError, match="evidence changed"):
        preflight_execution(
            execution_config(), ExecutionClient(), report, plan_step_id(report)
        )


def test_preflight_rejects_saved_plan_content_that_no_longer_matches_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    step_id = plan_step_id(report)
    report["repositories"][0]["recommended_order"][0]["action"] = "hold"
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)

    with pytest.raises(ExecutionError, match="identity validation failed"):
        preflight_execution(execution_config(), ExecutionClient(), report, step_id)


def test_preflight_requires_configured_passing_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    report["repositories"][0]["workflow_profile"]["required_checks"] = [
        "CI",
        "Security Scan",
    ]
    refresh_plan_identities(report)
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)

    with pytest.raises(ExecutionError, match="Security Scan"):
        preflight_execution(
            execution_config(required_checks=["CI", "Security Scan"]),
            ExecutionClient(),
            report,
            plan_step_id(report),
        )


def test_execute_requires_token_then_writes_locked_append_only_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)

    with pytest.raises(ExecutionError, match="does not authorize"):
        execute_approved(
            execution_config(), client, report, step_id, "approve_wrong", state_directory=tmp_path
        )
    result = execute_approved(
        execution_config(),
        client,
        report,
        step_id,
        preflight["approval_token"],
        state_directory=tmp_path,
    )

    assert result["status"] == "merged"
    assert result["mutation_performed"] is True
    assert client.merges == [
        {
            "repository": "example-org/example-app",
            "number": 42,
            "head_sha": HEAD_SHA,
            "merge_method": "squash",
        }
    ]
    audit_path = tmp_path / "execution-audit.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == [
        "execution_started",
        "execution_succeeded",
    ]
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    assert not (tmp_path / "execution.lock").exists()

    retry = execute_approved(
        execution_config(),
        client,
        report,
        step_id,
        preflight["approval_token"],
        state_directory=tmp_path,
    )
    assert retry["status"] == "already_applied"
    assert len(client.merges) == 1
    reconciled = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert reconciled["event_type"] == "execution_reconciled"
    assert reconciled["prior_success_recorded"] is True


def test_invalid_token_fails_before_live_reinspection_or_state_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    client = ExecutionClient()
    monkeypatch.setattr(
        "dependabot_watcher.execution.plan",
        lambda config, client: pytest.fail("invalid tokens must fail before planning"),
    )
    state_path = tmp_path / "execution-state"

    with pytest.raises(ExecutionError, match="does not authorize"):
        execute_approved(
            execution_config(),
            client,
            report,
            plan_step_id(report),
            "approve_wrong",
            state_directory=state_path,
        )

    assert client.fetches == 0
    assert not state_path.exists()


def test_approved_execution_performs_one_locked_live_reinspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    calls = 0

    def fresh_plan(config, client):
        nonlocal calls
        calls += 1
        return report

    monkeypatch.setattr("dependabot_watcher.execution.plan", fresh_plan)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    token = approval_token(
        {
            "repository_plan_id": report["repositories"][0]["plan_id"],
            "step_id": step_id,
            "repository": "example-org/example-app",
            "pull_request": 42,
            "expected_base": "main",
            "head_sha": HEAD_SHA,
            "merge_method": "squash",
        }
    )

    result = execute_approved(
        execution_config(),
        client,
        report,
        step_id,
        token,
        state_directory=tmp_path,
    )

    assert result["status"] == "merged"
    assert calls == 1
    assert client.fetches == 1


def test_audit_capacity_failure_prevents_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    monkeypatch.setattr("dependabot_watcher.execution._MAX_AUDIT_BYTES", 1)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)

    with pytest.raises(ExecutionError, match="safety ceiling"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    assert client.merges == []


def test_incomplete_start_audit_write_prevents_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    real_write = os.write
    write_calls = 0

    def incomplete_write(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            first_chunk = bytes(payload[:8])
            return real_write(descriptor, first_chunk)
        return 0

    monkeypatch.setattr("dependabot_watcher.execution.os.write", incomplete_write)

    with pytest.raises(ExecutionError, match="written completely"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    assert client.merges == []


def test_start_audit_fsync_failure_prevents_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_audit_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("simulated audit fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr("dependabot_watcher.execution.os.fsync", fail_audit_fsync)

    with pytest.raises(ExecutionError, match="synchronized safely"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    assert client.merges == []


def test_malformed_existing_audit_blocks_normal_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    tmp_path.chmod(0o700)
    audit_path = tmp_path / "execution-audit.jsonl"
    audit_path.write_text("not-json\n", encoding="utf-8")
    audit_path.chmod(0o600)

    with pytest.raises(ExecutionError, match="malformed JSON"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    assert client.merges == []


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe\x00",
        (b"[" * 2_000) + (b"]" * 2_000),
        b'{"value":' + (b"9" * 5_000) + b"}",
    ],
    ids=("non_utf8", "deep_json", "oversized_integer"),
)
def test_pathological_audit_json_fails_closed_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    tmp_path.chmod(0o700)
    audit_path = tmp_path / "execution-audit.jsonl"
    audit_path.write_bytes(payload)
    audit_path.chmod(0o600)

    with pytest.raises(ExecutionError, match="audit"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    assert client.merges == []


def test_oversized_merge_response_is_omitted_from_bounded_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)

    class OversizedResponseClient(ExecutionClient):
        def merge_pull_request(
            self,
            repository: str,
            number: int,
            *,
            head_sha: str,
            merge_method: str,
        ) -> dict[str, Any]:
            result = super().merge_pull_request(
                repository,
                number,
                head_sha=head_sha,
                merge_method=merge_method,
            )
            result["merge_commit_sha"] = "x" * 100_000
            return result

    client = OversizedResponseClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    result = execute_approved(
        execution_config(),
        client,
        report,
        step_id,
        preflight["approval_token"],
        state_directory=tmp_path,
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "execution-audit.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert result["result"]["merge_commit_sha"] is None
    assert events[-1]["event_type"] == "execution_succeeded"
    assert events[-1]["merge_commit_sha"] is None


def test_saved_binding_rejects_non_full_head_sha_before_live_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    report["repositories"][0]["recommended_order"][0]["identity"]["head_sha"] = "short"
    report["repositories"][0]["recommended_order"][0]["freshness"]["head_sha"] = "short"
    refresh_plan_identities(report)
    client = ExecutionClient()
    monkeypatch.setattr(
        "dependabot_watcher.execution.plan",
        lambda config, client: pytest.fail("invalid SHA must fail before planning"),
    )

    with pytest.raises(ExecutionError, match="full Git commit SHA"):
        preflight_execution(
            execution_config(), client, report, plan_step_id(report)
        )

    assert client.fetches == 0


def test_regular_file_state_path_fails_as_execution_error_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    state_path = tmp_path / "not-a-directory"
    state_path.write_text("occupied\n", encoding="utf-8")

    with pytest.raises(ExecutionError, match="must be a directory"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=state_path,
        )

    assert client.merges == []


def test_non_regular_audit_path_fails_closed_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    tmp_path.chmod(0o700)
    (tmp_path / "execution-audit.jsonl").mkdir()

    with pytest.raises(ExecutionError, match="Execution audit"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    assert client.merges == []


def test_execute_records_sanitized_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient(merge_error=True)
    preflight = preflight_execution(
        execution_config(), client, report, plan_step_id(report)
    )

    with pytest.raises(GitHubError, match="simulated"):
        execute_approved(
            execution_config(),
            client,
            report,
            plan_step_id(report),
            preflight["approval_token"],
            state_directory=tmp_path,
        )

    events = [
        json.loads(line)
        for line in (tmp_path / "execution-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event_type"] == "execution_failed"
    assert events[-1]["outcome"] == "github_error"
    assert "simulated" not in json.dumps(events[-1])


def test_execute_reconciles_exact_already_merged_head_without_mutation() -> None:
    client = ExecutionClient(state="MERGED")
    report = saved_plan()
    result = preflight_execution(
        execution_config(), client, report, plan_step_id(report)
    )

    assert result["status"] == "already_applied"
    assert result["approval_token"] is None
    assert client.merges == []


def test_execution_lock_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = saved_plan()
    monkeypatch.setattr("dependabot_watcher.execution.plan", lambda config, client: report)
    client = ExecutionClient()
    step_id = plan_step_id(report)
    preflight = preflight_execution(execution_config(), client, report, step_id)
    tmp_path.chmod(0o700)
    (tmp_path / "execution.lock").write_text("occupied\n", encoding="utf-8")

    with pytest.raises(ExecutionError, match="may be active"):
        execute_approved(
            execution_config(),
            client,
            report,
            step_id,
            preflight["approval_token"],
            state_directory=tmp_path,
        )
