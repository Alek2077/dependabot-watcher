from __future__ import annotations

import copy
import io
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.config import load_config, parse_config
from dependabot_watcher.planning import (
    _alert_step, _overall_plan_identifier, _repository_plan_identifier, _step_identifier,
)
from dependabot_watcher.review_bridge import exchange
from dependabot_watcher.review_queue import ReviewConfigurationChanged, ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import (
    RuntimeStateError, load_private_json, load_run_state, run_lock,
    save_private_json, save_run_state,
)
from test_execution import refresh_plan_identities, saved_plan


def persist_plan(directory: Path, report: dict[str, Any]) -> None:
    save_private_json(directory, Path("plans") / f'{report["plan_id"]}.json', report)
    state = load_run_state(directory)
    state["latest_plan"] = {"plan_id": report["plan_id"], "path": "/ignored/untrusted/path"}
    save_run_state(directory, state)


@pytest.fixture
def queue_case(tmp_path: Path) -> tuple[ReviewQueue, dict[str, Any]]:
    config = parse_config({
        "schema_version": 1, "repositories": [{"name": "example-org/example-app"}],
    })
    report = saved_plan()
    directory = tmp_path / "state"
    persist_plan(directory, report)
    return ReviewQueue(config, directory), report


@pytest.fixture
def file_bound_queue(tmp_path: Path) -> tuple[ReviewQueue, Path]:
    path = tmp_path / "config.toml"
    path.write_text('schema_version = 1\n[supervisor]\nmode = "approval_first"\n'
        'provider = "codex_app_server"\nmodel = "gpt-5.6-luna"\nreasoning_effort = "xhigh"\n'
        'expected_account_type = "chatgpt"\naccess = "inherit"\nmodel_use_acknowledged = true\n'
        'workspaces = [{repository = "example-org/example-app", directory = '
        + json.dumps(str(tmp_path)) + ', instruction_sources = [' + json.dumps(str(tmp_path / "AGENTS.md")) + ']}]\n'
        '[[repositories]]\nname = "example-org/example-app"\n', encoding="utf-8")
    path.chmod(0o600)
    queue = ReviewQueue(load_config(path), tmp_path / "state", config_path=path)
    persist_plan(queue.directory, saved_plan())
    return queue, path


def snapshot(queue: ReviewQueue) -> dict[str, Any]:
    return queue.handle({"schema_version": 1, "request_id": "read_view", "operation": "snapshot"})


def workspace_receipt(queue: ReviewQueue) -> dict[str, Any]:
    """A schema fixture; real Git verification is covered in the workspace suite."""
    registration = queue.config.supervisor.workspaces[0]
    return {"repository": registration.repository, "directory": registration.directory,
            "git_directory": str(queue.directory / "git" / "worktrees" / "maintenance"),
            "common_directory": str(queue.directory / "git"), "head_sha": "b" * 40,
            "instruction_fingerprints": [[source, "c" * 64] for source in registration.instruction_sources]}


def test_supervisor_context_carries_only_its_repository_action_observations(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    before = queue.supervision_context("example-org/example-app")
    assert "recent_actions" not in before
    state = load_run_state(queue.directory)
    action = {"action_id": "action_" + "a" * 24, "repository": "example-org/example-app",
              "pull_request": 42, "head_sha": "b" * 40, "status": "success_observed", "recovery_required": False}
    state["last_attempt"] = {"execution_observation": {"observations": [
        {**action, "latest_outcome": "not forwarded", "recovery": "not forwarded"},
        {**action, "repository": "other/repository"},
    ]}}
    save_run_state(queue.directory, state)
    context = queue.supervision_context("EXAMPLE-ORG/EXAMPLE-APP")
    assert context["recent_actions"] == [action]
    assert context["mutation_authorized"] is False
    assert context["proposals"] == before["proposals"]


def test_legacy_queue_migration_preserves_feedback_handling_and_receipts(
    queue_case: tuple[ReviewQueue, dict[str, Any]],
) -> None:
    queue, _ = queue_case
    queue.handle(decision(queue, "feedback", message="Preserve this saved question."))
    queue.handle(decision(queue, "defer", seconds=86400))
    before = snapshot(queue)
    state = load_private_json(queue.directory, Path("review-queue.json"))
    state.pop("workspace_evidence")
    state["schema_version"] = 2
    save_private_json(queue.directory, Path("review-queue.json"), state)
    assert snapshot(queue) == before
    migrated = load_private_json(queue.directory, Path("review-queue.json"))
    assert migrated == {**state, "schema_version": 3, "workspace_evidence": {}}


@pytest.mark.parametrize("schema", [2, 3])
def test_ambiguous_queue_is_not_migrated_or_synchronized(queue_case: tuple[ReviewQueue, dict[str, Any]], schema: int) -> None:
    queue, _ = queue_case
    queue.handle(decision(queue, "feedback", message="Preserve this exact message."))
    state = load_private_json(queue.directory, Path("review-queue.json"))
    state["schema_version"] = schema
    if schema == 2:
        state.pop("workspace_evidence")
    payload = json.dumps(state)
    payload = payload[:-1] + ',"proposals":{}}'
    path = queue.directory / "review-queue.json"
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(RuntimeStateError, match="duplicate"):
        snapshot(queue)
    assert path.read_bytes() == before
    output = io.BytesIO()
    assert exchange(queue, io.BytesIO(b'{"schema_version":1,"request_id":"read_ambiguous","operation":"snapshot"}'), output) == 2
    assert json.loads(output.getvalue())["status"] == "error"
    assert path.read_bytes() == before


def test_workspace_receipt_persists_in_exact_scoped_context_and_native_digest(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, path = file_bound_queue
    before = snapshot(queue)
    evidence = workspace_receipt(queue)
    evidence["repository"] = "EXAMPLE-ORG/Example-App"
    queue.record_workspace_evidence(evidence)
    reopened = ReviewQueue(load_config(path), queue.directory, config_path=path)
    context = reopened.supervision_context("example-org/example-app")
    assert context["workspace_evidence"] == evidence
    assert context["mutation_authorized"] is False
    after = snapshot(reopened)
    assert after["proposals"] == before["proposals"] and after["next_request_sequence"] == before["next_request_sequence"]
    assert after["supervisor_context_digests"] != before["supervisor_context_digests"]
    context["workspace_evidence"]["instruction_fingerprints"][0][1] = "d" * 64
    assert reopened.supervision_context("example-org/example-app")["workspace_evidence"] == evidence
    queue.record_workspace_evidence(evidence)
    assert snapshot(queue) == after


@pytest.mark.parametrize("change", ["directory", "instructions", "head", "repository", "disabled", "unbound"])
def test_receipt_must_match_current_registered_policy_and_collected_base_without_overwriting_state(
    file_bound_queue: tuple[ReviewQueue, Path], change: str,
) -> None:
    original, _ = file_bound_queue
    evidence = workspace_receipt(original)
    original.record_workspace_evidence(evidence)
    before = load_private_json(original.directory, Path("review-queue.json"))
    # Policy variations use diagnostic in-memory queues, not a changed-file bypass.
    queue = ReviewQueue(original.config, original.directory)
    if change == "directory":
        evidence["directory"] += "/another"
    elif change == "instructions":
        evidence["instruction_fingerprints"] = [["/another/AGENTS.md", "c" * 64]]
    elif change == "head":
        evidence["head_sha"] = "a" * 40
    elif change == "repository":
        evidence["repository"] = "other/repository"
    elif change == "disabled":
        queue.config = replace(queue.config, supervisor=replace(queue.config.supervisor, mode="off"))
    else:
        queue.config = replace(queue.config, supervisor=replace(queue.config.supervisor, workspaces=()))
    with pytest.raises(ReviewQueueError):
        queue.record_workspace_evidence(evidence)
    assert load_private_json(queue.directory, Path("review-queue.json")) == before


@pytest.mark.parametrize("change", ["base", "source", "directory", "disabled"])
def test_obsolete_receipt_becomes_noncurrent_without_erasing_historical_evidence(
    file_bound_queue: tuple[ReviewQueue, Path], change: str,
) -> None:
    original, _ = file_bound_queue
    evidence = workspace_receipt(original)
    original.record_workspace_evidence(evidence)
    queue = ReviewQueue(original.config, original.directory)
    registration = queue.config.supervisor.workspaces[0]
    if change == "base":
        report = saved_plan()
        report["repositories"][0]["evidence_snapshot"]["base_sha"] = "a" * 40
        persist_plan(queue.directory, refresh_plan_identities(report))
    elif change == "disabled":
        queue.config = replace(queue.config, supervisor=replace(queue.config.supervisor, mode="off"))
    else:
        registration = replace(registration, **({"instruction_sources": ("/new/AGENTS.md",)}
                              if change == "source" else {"directory": "/new/worktree"}))
        queue.config = replace(queue.config, supervisor=replace(queue.config.supervisor, workspaces=(registration,)))
    assert queue.supervision_context("example-org/example-app")["workspace_evidence"] is None
    state = load_private_json(queue.directory, Path("review-queue.json"))
    assert state["workspace_evidence"]["example-org/example-app"] == evidence


@pytest.mark.parametrize("field,value", [("repository", "bad identity"), ("repository", " example-org/example-app"),
    ("directory", "/owned/../another"), ("directory", "/owned\x00"), ("git_directory", "relative"),
    ("head_sha", "bad"), ("instruction_fingerprints", [["/AGENTS.md", "bad"]]),
    ("instruction_fingerprints", [["/AGENTS.md", "c" * 64], ["/AGENTS.md", "d" * 64]])])
def test_corrupt_workspace_journal_is_rejected_without_rewrite(
    file_bound_queue: tuple[ReviewQueue, Path], field: str, value: Any,
) -> None:
    queue, _ = file_bound_queue
    snapshot(queue)
    evidence = workspace_receipt(queue)
    evidence[field] = value
    state = load_private_json(queue.directory, Path("review-queue.json"))
    state["workspace_evidence"] = {"example-org/example-app": evidence}
    save_private_json(queue.directory, Path("review-queue.json"), state)
    with pytest.raises(ReviewQueueError):
        snapshot(queue)
    assert load_private_json(queue.directory, Path("review-queue.json")) == state


def test_ui_cannot_supply_a_workspace_receipt(file_bound_queue: tuple[ReviewQueue, Path]) -> None:
    queue, _ = file_bound_queue
    snapshot(queue)
    before = load_private_json(queue.directory, Path("review-queue.json"))
    for request in ({"schema_version": 1, "request_id": "read_forged", "operation": "snapshot",
                     "workspace_evidence": workspace_receipt(queue)},
                    {"schema_version": 1, "request_id": "decision_1_forged", "operation": "record_workspace_evidence",
                     "workspace_evidence": workspace_receipt(queue)}):
        output = io.BytesIO()
        assert exchange(queue, io.BytesIO(json.dumps(request).encode()), output) == 2
        assert json.loads(output.getvalue())["status"] == "error"
        assert load_private_json(queue.directory, Path("review-queue.json")) == before


def decision(queue: ReviewQueue, operation: str, *, request_id: str = "choice", **fields: Any) -> dict[str, Any]:
    view = snapshot(queue)
    item = view["proposals"][0]
    return {"schema_version": 1, "request_id": f'decision_{view["next_request_sequence"]}_{request_id}', "operation": operation,
            "proposal_id": item["proposal_id"], "version": item["version"], **fields}


def test_read_only_queue_is_visible_without_execution_or_provider(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = queue_case
    view = snapshot(queue)
    assert view["plan_id"] == report["plan_id"]
    assert view["proposals"][0]["step"]["identity"]["number"] == 42
    assert view["capabilities"] == {"github_mutations": False, "model_calls": False}
    assert not (queue.directory / "execution-audit.jsonl").exists()


@pytest.mark.parametrize("change", ["model", "disabled", "missing", "invalid",
    pytest.param("untrusted", marks=pytest.mark.skipif(os.name != "posix", reason="POSIX config mode check"))])
def test_file_bound_context_and_feedback_refuse_changed_or_unavailable_policy(
    file_bound_queue: tuple[ReviewQueue, Path], change: str,
) -> None:
    queue, path = file_bound_queue
    before = snapshot(queue)
    request = decision(queue, "feedback", message="Do not save against a revoked policy.")
    if change == "model":
        path.write_text(path.read_text().replace('model = "gpt-5.6-luna"', 'model = "gpt-6.1-sol"'))
    elif change == "disabled":
        path.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    elif change == "missing":
        path.unlink()
    elif change == "invalid":
        path.write_text("invalid = [")
    else:
        path.chmod(0o666)
    with pytest.raises(ReviewConfigurationChanged):
        queue.supervision_context("example-org/example-app")
    with pytest.raises(ReviewConfigurationChanged):
        queue.handle(request)
    state = load_private_json(queue.directory, Path("review-queue.json"))
    assert list(state["proposals"].values()) == before["proposals"]
    assert state["next_request_sequence"] == before["next_request_sequence"]


def test_nonsemantic_file_change_preserves_policy_binding(file_bound_queue: tuple[ReviewQueue, Path]) -> None:
    queue, path = file_bound_queue
    before = queue.supervision_context("example-org/example-app")
    path.write_text(path.read_text() + "\n# An operator comment does not change permissions.\n")
    queue.require_current_configuration()
    assert queue.supervision_context("example-org/example-app") == before


def test_snapshot_preserves_exact_material_evidence_and_does_not_alias_plan(
    queue_case: tuple[ReviewQueue, dict[str, Any]],
) -> None:
    queue, report = queue_case
    expected = report["repositories"][0]["recommended_order"][0]
    view = snapshot(queue)
    assert view["proposals"][0]["step"] == expected
    assert view["proposals"][0]["plan_id"] == report["plan_id"]
    assert view["proposals"][0]["repository"] == expected["identity"]["repository"]
    view["proposals"][0]["step"]["identity"]["head_sha"] = "f" * 40
    assert snapshot(queue)["proposals"][0]["step"] == expected


def test_case_only_config_change_preserves_handling_feedback_and_exact_saved_plan(
    queue_case: tuple[ReviewQueue, dict[str, Any]],
) -> None:
    queue, report = queue_case
    queue.handle(decision(queue, "feedback", message="Keep this exact head under review."))
    queue.handle(decision(queue, "defer", seconds=86400))
    before = snapshot(queue)
    queue.config = replace(queue.config, repositories=(
        replace(queue.config.repositories[0], name="EXAMPLE-ORG/Example-App"),))
    after = snapshot(queue)
    assert after["proposals"] == before["proposals"]
    assert after["history"] == []
    assert queue._current_plan() == report
    context = queue.supervision_context("EXAMPLE-org/EXAMPLE-app")
    assert context["repository_plan"] == report["repositories"][0]
    assert context["proposals"][0]["feedback"][0]["message"] == "Keep this exact head under review."
    assert context["proposals"][0]["handling"] == "deferred"
    assert context["repository"] in after["supervisor_context_digests"]


def test_case_aliases_in_saved_plan_are_rejected_instead_of_combining_evidence(
    queue_case: tuple[ReviewQueue, dict[str, Any]],
) -> None:
    queue, report = queue_case
    second = copy.deepcopy(report["repositories"][0])
    second["repository"] = "EXAMPLE-ORG/Example-App"
    for step in second["recommended_order"]:
        step["identity"]["repository"] = second["repository"]
        core = {key: value for key, value in step.items() if key not in {"step_id", "order"}}
        step["step_id"] = _step_identifier(second["repository"], second, core)
    second["plan_id"] = _repository_plan_identifier(
        {key: value for key, value in second.items() if key != "plan_id"})
    report["repositories"].append(second)
    report["plan_id"] = _overall_plan_identifier(report["status"], [item["plan_id"] for item in report["repositories"]])
    persist_plan(queue.directory, report)
    with pytest.raises(ReviewQueueError, match="repeats a repository"):
        snapshot(queue)


def test_same_pr_number_in_different_repositories_does_not_cross_wire_feedback(tmp_path: Path) -> None:
    report = saved_plan()
    second = copy.deepcopy(report["repositories"][0])
    second["repository"] = "example-org/other-app"
    step = second["recommended_order"][0]
    step["identity"].update(repository=second["repository"], title="Other repository update", head_sha="e" * 40)
    step["freshness"]["head_sha"] = "e" * 40
    core = {key: value for key, value in step.items() if key not in {"step_id", "order"}}
    step["step_id"] = _step_identifier(second["repository"], second, core)
    second["plan_id"] = _repository_plan_identifier({key: value for key, value in second.items() if key != "plan_id"})
    report["repositories"].append(second)
    report["plan_id"] = _overall_plan_identifier("complete", [item["plan_id"] for item in report["repositories"]])
    config = parse_config({"schema_version": 1, "repositories": [
        {"name": "example-org/example-app"}, {"name": "example-org/other-app"},
    ]})
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, report)
    view = snapshot(queue)
    selected = view["proposals"][1]
    message = "Please handle this repo first — café 日本語 🙂"
    request = {
        "schema_version": 1, "request_id": f'decision_{view["next_request_sequence"]}_second',
        "operation": "feedback", "proposal_id": selected["proposal_id"],
        "version": selected["version"], "message": message,
    }
    output = io.BytesIO()
    assert exchange(queue, io.BytesIO(json.dumps(request, ensure_ascii=False).encode("utf-8")), output) == 0
    response = json.loads(output.getvalue())
    assert response["request_id"] == request["request_id"]
    assert response["proposals"][0]["feedback"] == []
    assert response["proposals"][1]["feedback"][0]["message"] == message
    restarted = snapshot(ReviewQueue(config, queue.directory))
    assert restarted["proposals"] == response["proposals"]
    # A changed configured scope never keeps the excluded repository actionable.
    narrowed = replace(config, repositories=(config.repositories[0],))
    filtered = snapshot(ReviewQueue(narrowed, queue.directory))
    assert len(filtered["proposals"]) == 1
    assert filtered["proposals"][0]["repository"] == "example-org/example-app"
    assert filtered["history"][0]["feedback"][0]["message"] == message


def test_feedback_stays_with_retired_evidence_and_is_not_reassigned_to_a_new_head(
    queue_case: tuple[ReviewQueue, dict[str, Any]],
) -> None:
    queue, report = queue_case
    request = decision(queue, "feedback", message="Review this exact old head before deciding.")
    queue.handle(request)
    step = report["repositories"][0]["recommended_order"][0]
    step["identity"]["head_sha"] = "e" * 40
    step["freshness"]["head_sha"] = "e" * 40
    persist_plan(queue.directory, refresh_plan_identities(report))
    view = snapshot(queue)
    assert view["proposals"][0]["feedback"] == []
    retired = view["history"][0]
    assert retired["proposal_id"] == request["proposal_id"]
    assert retired["step"]["identity"]["head_sha"] != "e" * 40
    assert retired["feedback"][0]["message"] == request["message"]


@pytest.mark.parametrize("message, accepted", [("🙂" * 2048, True), ("🙂" * 2049, False)])
def test_feedback_byte_boundary_is_utf8_not_character_count(
    queue_case: tuple[ReviewQueue, dict[str, Any]], message: str, accepted: bool,
) -> None:
    queue, _ = queue_case
    request = decision(queue, "feedback", message=message)
    if accepted:
        assert queue.handle(request)["proposals"][0]["feedback"][0]["message"] == message
    else:
        with pytest.raises(ReviewQueueError, match="8192 UTF-8 bytes"):
            queue.handle(request)
        assert snapshot(queue)["proposals"][0]["feedback"] == []


def test_reject_survives_backend_restart_and_unrelated_replanning(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = queue_case
    original_plan_id = report["plan_id"]
    request = decision(queue, "reject")
    queue.handle(request)
    repository = report["repositories"][0]
    second = copy.deepcopy(repository["recommended_order"][0])
    second["identity"]["number"] = 43
    core = {key: value for key, value in second.items() if key not in {"step_id", "order"}}
    second["step_id"] = _step_identifier(repository["repository"], repository, core)
    second["order"] = 2
    repository["recommended_order"].append(second)
    persist_plan(queue.directory, refresh_plan_identities(report))
    restarted = ReviewQueue(queue.config, queue.directory)
    view = snapshot(restarted)
    assert view["proposals"][0]["handling"] == "rejected"
    assert view["proposals"][1]["handling"] == "pending"
    assert view["plan_id"] != original_plan_id
    assert restarted.handle(request)["replayed"] is True


def test_changed_head_retires_old_handling_and_refuses_stale_decision(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = queue_case
    request = decision(queue, "reject")
    queue.handle(request)
    step = report["repositories"][0]["recommended_order"][0]
    step["identity"]["head_sha"] = "c" * 40
    step["freshness"]["head_sha"] = "c" * 40
    persist_plan(queue.directory, refresh_plan_identities(report))
    view = snapshot(queue)
    assert view["proposals"][0]["handling"] == "pending"
    assert view["history"][0]["handling"] == "rejected"
    stale = {**request, "request_id": f'decision_{view["next_request_sequence"]}_stale'}
    with pytest.raises(ReviewQueueError, match="superseded"):
        queue.handle(stale)


def test_defer_has_real_resume_time_and_resume_invalidates_old_ui_version(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    queue.clock = lambda: now
    deferred = queue.handle(decision(queue, "defer", seconds=60))["proposals"][0]
    assert deferred["resume_at"] == "2026-09-30T00:01:00Z"
    stale = decision(queue, "reject", request_id="later")
    queue.clock = lambda: now + timedelta(seconds=60)
    view = snapshot(queue)
    assert view["proposals"][0]["handling"] == "pending"
    assert view["proposals"][0]["version"] == deferred["version"] + 1
    with pytest.raises(ReviewQueueError, match="handling changed"):
        queue.handle(stale)


def test_feedback_preserves_rejection_and_is_honestly_not_sent_to_agent(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    queue.handle(decision(queue, "reject"))
    request = decision(queue, "feedback", request_id="question", message="Why this order?")
    view = queue.handle(request)
    item = view["proposals"][0]
    assert item["handling"] == "rejected"
    assert item["feedback"][0]["message"] == "Why this order?"
    assert item["feedback"][0]["status"] == "saved_not_submitted"
    assert queue.handle(request)["replayed"] is True
    assert len(snapshot(queue)["proposals"][0]["feedback"]) == 1
    with pytest.raises(ReviewQueueError, match="different operation"):
        queue.handle({**request, "message": "Another question"})


def test_local_version_prevents_concurrent_lost_decisions(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    first = decision(queue, "reject")
    queue.handle(first)
    second = {**first, "request_id": f'decision_{snapshot(queue)["next_request_sequence"]}_another', "operation": "resume"}
    with pytest.raises(ReviewQueueError, match="handling changed"):
        queue.handle(second)
    with run_lock(queue.directory), pytest.raises(RuntimeStateError, match="already active"):
        snapshot(queue)


def test_missing_current_plan_does_not_leave_an_actionable_old_proposal(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    request = decision(queue, "reject")
    state = load_run_state(queue.directory)
    state["latest_plan"] = {"plan_id": None}
    save_run_state(queue.directory, state)
    view = snapshot(queue)
    assert view["proposals"] == [] and view["evidence_status"] == "unavailable"
    assert len(view["history"]) == 1
    with pytest.raises(ReviewQueueError, match="superseded"):
        queue.handle(request)


def test_tampered_plan_is_not_rendered(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = queue_case
    report["repositories"][0]["recommended_order"][0]["identity"]["number"] = 999
    persist_plan(queue.directory, report)
    with pytest.raises(ReviewQueueError, match="material identity"):
        snapshot(queue)


def test_failed_save_is_not_acknowledged(queue_case: tuple[ReviewQueue, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _ = queue_case
    request = decision(queue, "reject")
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeStateError("simulated storage failure")
    monkeypatch.setattr("dependabot_watcher.review_queue.save_private_json", fail)
    with pytest.raises(RuntimeStateError, match="storage failure"):
        queue.handle(request)
    stored = load_private_json(queue.directory, Path("review-queue.json"))
    assert stored["proposals"][request["proposal_id"]]["handling"] == "pending"


@pytest.mark.parametrize("body", [b"", b"[]", b"not-json", b"\xff", b"x" * 65537,
    b'{"schema_version":1,"request_id":"bad","operation":[]}',
    b'{"schema_version":true,"request_id":"bad","operation":"snapshot"}',
    b'{"schema_version":1,"request_id":"bad","operation":"approve"}',
    b'{"schema_version":1,"request_id":"bad","operation":"snapshot","path":"/tmp"}',
])
def test_bridge_rejects_untrusted_requests_with_structured_errors(queue_case: tuple[ReviewQueue, dict[str, Any]], body: bytes) -> None:
    queue, _ = queue_case
    output = io.BytesIO()
    assert exchange(queue, io.BytesIO(body), output) == 2
    assert json.loads(output.getvalue())["status"] == "error"
    assert not (queue.directory / "execution-audit.jsonl").exists()


def test_bridge_returns_matching_request_id_and_backend_snapshot(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    output = io.BytesIO()
    body = json.dumps({"schema_version": 1, "request_id": "read_native-1", "operation": "snapshot"}).encode()
    assert exchange(queue, io.BytesIO(body), output) == 0
    response = json.loads(output.getvalue())
    assert response["request_id"] == "read_native-1" and len(response["proposals"]) == 1


@pytest.mark.parametrize("corruption", ["schema", "defer", "receipt", "feedback"])
def test_bridge_reports_corrupt_persisted_state_without_acknowledging_a_decision(
    queue_case: tuple[ReviewQueue, dict[str, Any]], corruption: str,
) -> None:
    queue, _ = queue_case
    request = decision(queue, "reject")
    state = load_private_json(queue.directory, Path("review-queue.json"))
    item = state["proposals"][request["proposal_id"]]
    if corruption == "schema":
        state["schema_version"] = True
    elif corruption == "defer":
        item["handling"] = "deferred"
    elif corruption == "receipt":
        state["receipts"] = [{"request_id": "bad"}]
    else:
        item["feedback"] = [{"message": []}]
    save_private_json(queue.directory, Path("review-queue.json"), state)
    output = io.BytesIO()
    assert exchange(queue, io.BytesIO(json.dumps(request).encode()), output) == 2
    assert json.loads(output.getvalue())["status"] == "error"
    assert load_private_json(queue.directory, Path("review-queue.json")) == state


def test_bridge_reports_malformed_plan_status(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = queue_case
    report["status"] = []
    persist_plan(queue.directory, report)
    output = io.BytesIO()
    body = b'{"schema_version":1,"request_id":"read_check","operation":"snapshot"}'
    assert exchange(queue, io.BytesIO(body), output) == 2
    assert "invalid review contract" in json.loads(output.getvalue())["message"]


def test_retired_request_ids_never_become_reusable_after_receipt_pruning(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    old = decision(queue, "reject", request_id="old")
    queue.handle(old)
    for index in range(100):
        queue.handle(decision(queue, "resume", request_id=f"write{index}"))
    state = load_private_json(queue.directory, Path("review-queue.json"))
    assert len(state["receipts"]) == 100 and state["next_request_sequence"] == 102
    assert all(receipt["request_id"] != old["request_id"] for receipt in state["receipts"])
    differing_reuse = decision(queue, "reject")
    differing_reuse["request_id"] = old["request_id"]
    with pytest.raises(ReviewQueueError, match="retired or stale"):
        queue.handle(differing_reuse)
    with pytest.raises(ReviewQueueError, match="retired or stale"):
        queue.handle(old)
    assert snapshot(queue)["proposals"][0]["handling"] == "pending"


def test_snapshot_ids_cannot_be_reused_for_local_writes(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    snapshot(queue)
    request = decision(queue, "reject")
    with pytest.raises(ReviewQueueError, match="next request sequence"):
        queue.handle({**request, "request_id": "read_view"})
    with pytest.raises(ReviewQueueError, match="read namespace"):
        queue.handle({"schema_version": 1, "request_id": request["request_id"], "operation": "snapshot"})


def test_alert_completeness_change_invalidates_old_ui_request(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, report = queue_case
    repository = report["repositories"][0]
    alert = _alert_step(repository["repository"], {"number": 7, "dependency_name": "example", "severity": "high"},
                        snapshot=repository["evidence_snapshot"], alert_snapshot=repository["alert_snapshot"],
                        workflow=repository["workflow_profile"], advice=None)
    assert alert is not None
    step = {**alert[1], "order": 1}
    repository["recommended_order"] = [step]
    persist_plan(queue.directory, refresh_plan_identities(report))
    request = decision(queue, "reject")
    original_step_id = step["step_id"]
    repository["status"] = "partial"
    repository["unresolved_evidence"] = {"manifest_status": "partial", "errors": [{"message": "Collection unavailable"}]}
    report["status"] = "partial"
    persist_plan(queue.directory, refresh_plan_identities(report))
    assert repository["recommended_order"][0]["step_id"] == original_step_id
    with pytest.raises(ReviewQueueError, match="superseded"):
        queue.handle(request)
    view = snapshot(queue)
    assert view["proposals"][0]["evidence_status"] == "partial"
    assert view["history"][0]["proposal_id"] == request["proposal_id"]


def test_changed_workflow_policy_invalidates_local_proposal(queue_case: tuple[ReviewQueue, dict[str, Any]]) -> None:
    queue, _ = queue_case
    request = decision(queue, "reject")
    repository = queue.config.repositories[0]
    queue.config = replace(queue.config, repositories=(replace(
        repository, workflow=replace(repository.workflow, merge_method="merge")),))
    with pytest.raises(ReviewQueueError, match="superseded"):
        queue.handle(request)


@pytest.mark.parametrize("corruption", ["history", "history_evidence", "feedback_time", "receipt_duplicate"])
def test_archived_feedback_and_receipt_metadata_are_validated(queue_case: tuple[ReviewQueue, dict[str, Any]], corruption: str) -> None:
    queue, report = queue_case
    request = decision(queue, "feedback", message="Please explain")
    queue.handle(request)
    if corruption in {"history", "history_evidence"}:
        report["repositories"][0]["recommended_order"][0]["identity"]["head_sha"] = "c" * 40
        persist_plan(queue.directory, refresh_plan_identities(report))
        snapshot(queue)
    state = load_private_json(queue.directory, Path("review-queue.json"))
    if corruption == "history":
        state["history"][0]["retired_at"] = "not-a-time"
    elif corruption == "history_evidence":
        state["history"][0]["step"]["identity"] = []
    elif corruption == "feedback_time":
        state["proposals"][request["proposal_id"]]["feedback"][0]["created_at"] = "2026-09-30"
    else:
        state["receipts"].append(copy.deepcopy(state["receipts"][0]))
    save_private_json(queue.directory, Path("review-queue.json"), state)
    with pytest.raises(ReviewQueueError):
        snapshot(queue)
