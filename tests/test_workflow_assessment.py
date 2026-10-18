from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from dependabot_watcher.config import load_config
from dependabot_watcher.local_sync import confirm_sync, prepare_sync
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import load_private_json, save_private_json
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_runner import SupervisorRunner
from dependabot_watcher.supervisor_workspace import _instruction_fingerprint, verify_supervisor_workspace
from dependabot_watcher.workflow_assessment import _verify_sources, checkout_workflow, guidance_snapshot, repository_workflow, validate_assessment, verify_workflow_for_action
from test_execution import refresh_plan_identities, saved_plan
from test_local_sync import sync_case
from test_review_queue import persist_plan
from test_supervisor_runner import ReceiptProvider
from test_supervisor_workspace import git, workspace_case


@pytest.fixture
def workflow_case(sync_case, workspace_case, tmp_path):
    case = sync_case
    workspace = workspace_case["worktree"]
    policy = tmp_path / "integration-policy.md"
    policy.write_text("Use the explicitly approved clean fast-forward for integration; production promotion is a separate decision.\n")
    (workspace / "AGENTS.md").write_text(f"For local integration read {policy}. Preserve user work.\n")
    git(workspace, "add", "AGENTS.md")
    git(workspace, "commit", "-m", "Select fixture workflow guidance")
    config_path = case["config_path"]
    config_path.write_text(config_path.read_text() + '\n[supervisor]\nmode = "approval_first"\n'
        'provider = "codex_app_server"\nmodel = "fixture-model"\nreasoning_effort = "low"\n'
        'expected_account_type = "chatgpt"\naccess = "inherit"\nmodel_use_acknowledged = true\n'
        '[[supervisor.workspaces]]\nrepository = "example-org/example-app"\n'
        f'directory = {json.dumps(str(workspace))}\ninstruction_sources = [{json.dumps(str(workspace / "AGENTS.md"))}]\n')
    config = load_config(config_path)
    report = saved_plan()
    head = git(workspace, "rev-parse", "HEAD")
    report["repositories"][0]["evidence_snapshot"]["base_sha"] = head
    for step in report["repositories"][0]["recommended_order"]:
        step["freshness"]["base_sha"] = head
    report = refresh_plan_identities(report)
    queue = ReviewQueue(config, case["state"])
    persist_plan(queue.directory, report)
    queue.record_workspace_evidence(asdict(verify_supervisor_workspace(config, "example-org/example-app", report["repositories"][0])))
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    return {**case, "config": config, "queue": queue, "jobs": jobs, "job": job,
            "policy": policy, "workspace": workspace}


def finish(case, method="fast_forward", status="established"):
    provider = ReceiptProvider(case["queue"])
    original = provider.analyze
    def analysis(job):
        observed = original(job)
        assessment = observed["analysis"]["workflow_assessment"]
        assessment["status"] = status
        assessment["local_checkouts"][0].update(method=method, explanation=f"The fixture ecosystem requires {method}.")
        assessment["sources"].append({"path": str(case["policy"]), "sha256": _instruction_fingerprint(case["policy"])})
        assessment["follow_up"] = [{"stage": "production promotion", "status": "pending", "required": True,
                                     "explanation": "Review all integration changes before a separately approved release."}]
        if status == "needs_clarification":
            assessment["uncertainties"] = ["Two applicable rules disagree about integration."]
        return observed
    provider.analyze = analysis
    return SupervisorRunner(case["jobs"], provider).run(case["job"]["job_id"])


@pytest.mark.parametrize("method", ["rebase", "merge", "custom team integration", "none"])
def test_different_workflows_are_preserved_not_silently_substituted(workflow_case, method):
    case = workflow_case
    finish(case, method)
    before = git(case["path"], "show-ref")
    view = checkout_workflow(case["config"], case["state"], "example-org/example-app", str(case["path"]), "integration")
    assert view["state"] == "unsupported" and view["method"] == method
    assert view["follow_up"][0]["stage"] == "production promotion"
    with pytest.raises(ReviewQueueError, match="No different integration method was substituted"):
        prepare_sync(case["config"], case["config_path"], case["state"], case["client"], case["target_id"])
    assert git(case["path"], "show-ref") == before
    assert not (case["state"] / "local-sync.json").exists()
    assert case["client"].reads == []  # Refuse before even fetching remote objects.


def test_compatible_workflow_still_needs_exact_consent_and_preserves_follow_up(workflow_case):
    case = workflow_case
    finish(case)
    before = {path: path.read_bytes() for path in case["state"].rglob("*") if path.is_file()}
    view = checkout_workflow(case["config"], case["state"], "example-org/example-app", str(case["path"]), "integration")
    assert view["state"] == "ready" and view["follow_up"][0]["required"]
    assert {path: path.read_bytes() for path in case["state"].rglob("*") if path.is_file()} == before
    prepared = prepare_sync(case["config"], case["config_path"], case["state"], case["client"], case["target_id"])
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    result = confirm_sync(case["config"], case["config_path"], case["state"], case["client"], prepared["review_id"], prepared["approval_token"])
    assert result["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "HEAD") == case["new"]
    assert result["binding"]["workflow"]["follow_up"][0]["status"] == "pending"


def test_repository_workflow_is_shared_in_queue_without_self_invalidating_context(workflow_case):
    case = workflow_case
    finish(case)
    context_before = case["queue"]._supervision_context_under_lock("example-org/example-app", persist=False)
    shared = repository_workflow(case["config"], case["state"], "example-org/example-app")
    assert shared["state"] == "established"
    assert shared["assessment"]["local_checkouts"][0]["method"] == "fast_forward"
    snapshot = case["queue"].handle({"schema_version": 1, "request_id": "read_shared_workflow", "operation": "snapshot"})
    assert snapshot["repository_workflows"]["example-org/example-app"] == shared
    assert case["queue"]._supervision_context_under_lock("example-org/example-app", persist=False) == context_before
    case["policy"].write_text("Changed integration guidance.\n")
    assert repository_workflow(case["config"], case["state"], "example-org/example-app")["state"] == "assessment_needed"


@pytest.mark.parametrize("change", ["referenced_rule", "root_rule", "missing_receipt", "unclear"])
def test_stale_or_unestablished_guidance_cannot_integrate(workflow_case, change):
    case = workflow_case
    finish(case, status="needs_clarification" if change == "unclear" else "established")
    if change == "unclear":
        with pytest.raises(ReviewQueueError):
            prepare_sync(case["config"], case["config_path"], case["state"], case["client"], case["target_id"])
        return
    prepared = prepare_sync(case["config"], case["config_path"], case["state"], case["client"], case["target_id"])
    if change == "referenced_rule":
        case["policy"].write_text("Use rebase instead.\n")
    elif change == "root_rule":
        (case["workspace"] / "AGENTS.md").write_text("The local workflow changed.\n")
    else:
        (case["state"] / "supervisor-observations" / f'{case["job"]["job_id"]}.json').unlink()
    with pytest.raises(ReviewQueueError):
        confirm_sync(case["config"], case["config_path"], case["state"], case["client"], prepared["review_id"], prepared["approval_token"])
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    assert (case["path"] / "app.txt").read_text() == "before\n"


def test_local_checkout_instruction_chain_is_not_assumed_to_match_maintenance_copy(workflow_case):
    case = workflow_case
    before = guidance_snapshot(case["config"], "example-org/example-app")
    local = case["path"] / "AGENTS.md"
    local.write_text("Developer checkout requires its own integration convention.\n")
    current = guidance_snapshot(case["config"], "example-org/example-app")
    assert current != before
    assert [str(local), _instruction_fingerprint(local)] in current["instruction_fingerprints"]
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_unknown_workflow_and_unrelated_host_citations_are_not_action_authority(workflow_case, tmp_path):
    case = workflow_case
    assert checkout_workflow(case["config"], case["state"], "example-org/example-app", str(case["path"]), "integration")["state"] == "assessment_needed"
    outcome = finish(case)
    assessment = copy.deepcopy(outcome["job"]["result"]["workflow_assessment"])
    assessment["local_checkouts"] = []
    with pytest.raises(ReviewQueueError, match="every selected checkout"):
        validate_assessment(assessment, case["job"]["context"])
    outsider = tmp_path / "unrelated" / "policy.md"
    outsider.parent.mkdir()
    outsider.write_text("Unrelated repository policy.\n")
    journal = case["jobs"]._load()
    assessment = journal["jobs"][case["job"]["job_id"]]["result"]["workflow_assessment"]
    assessment["sources"].append({"path": str(outsider), "sha256": _instruction_fingerprint(outsider)})
    save_private_json(case["state"], Path("supervisor-jobs.json"), journal)
    relative = Path("supervisor-observations") / f'{case["job"]["job_id"]}.json'
    observation = load_private_json(case["state"], relative)
    observation["analysis"]["workflow_assessment"] = copy.deepcopy(assessment)
    save_private_json(case["state"], relative, observation)
    view = checkout_workflow(case["config"], case["state"], "example-org/example-app", str(case["path"]), "integration")
    assert view["state"] == "assessment_needed" and "outside selected scope" in view["message"]


def test_changed_referenced_rule_revokes_remote_readiness_as_well(workflow_case):
    case = workflow_case
    completed = finish(case)["job"]
    verify_workflow_for_action(completed["context"], completed["result"])
    case["policy"].write_text("Release policy changed; a new review is needed.\n")
    with pytest.raises(ReviewQueueError, match="guidance changed"):
        verify_workflow_for_action(completed["context"], completed["result"])


def test_required_local_capability_mismatch_cannot_become_remote_readiness(workflow_case):
    completed = finish(workflow_case, "team-specific integration")["job"]
    context = copy.deepcopy(completed["context"])
    context["repository_config"]["completion_policy"] = "selected_local_sync"
    with pytest.raises(ReviewQueueError, match="unsupported integration method"):
        verify_workflow_for_action(context, completed["result"])


@pytest.mark.parametrize("drift", ["superseded", "model", "workspace", "plan"])
def test_historical_or_drifted_assessment_cannot_prepare_update(workflow_case, drift):
    case = workflow_case
    finish(case)
    config = case["config"]
    if drift == "superseded":
        journal = case["jobs"]._load()
        journal["jobs"][case["job"]["job_id"]]["status"] = "superseded"
        journal["jobs"][case["job"]["job_id"]]["failure"] = "context_changed"
        save_private_json(case["state"], Path("supervisor-jobs.json"), journal)
    elif drift == "model":
        config = replace(config, supervisor=replace(config.supervisor, model="another-model"))
    elif drift == "workspace":
        (case["workspace"] / "uncommitted.txt").write_text("Preserve this local work.\n")
    else:
        report = saved_plan()
        persist_plan(case["state"], report)
    view = checkout_workflow(config, case["state"], "example-org/example-app", str(case["path"]), "integration")
    assert view["state"] == "assessment_needed"
    assert case["client"].reads == []
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_unregistered_workspace_is_a_repair_state_not_an_assessment_action(workflow_case):
    case = workflow_case
    config = replace(case["config"], supervisor=replace(case["config"].supervisor, workspaces=()))
    view = checkout_workflow(config, case["state"], "example-org/example-app", str(case["path"]), "integration")
    assert view["state"] == "unsupported" and "No supervisor workspace" in view["message"]


def test_missing_guidance_is_reported_in_snapshot(workflow_case):
    case = workflow_case
    registration = case["config"].supervisor.workspaces[0]
    missing = str(case["workspace"] / "missing.md")
    config = replace(case["config"], supervisor=replace(case["config"].supervisor,
        workspaces=(replace(registration, instruction_sources=(missing,)),)))
    snapshot = guidance_snapshot(config, "example-org/example-app")
    assert missing in snapshot["unavailable_sources"]
    view = checkout_workflow(config, case["state"], "example-org/example-app", str(case["path"]), "integration")
    assert view["state"] == "unsupported" and missing in view["message"]


@pytest.mark.parametrize("name", ["unreferenced-policy.md", "policy.md", "integration-policy.md.bak.md"])
def test_a_referenced_file_does_not_authorize_unreferenced_siblings(workflow_case, name):
    case = workflow_case
    sibling = case["policy"].with_name(name)
    sibling.write_text("Not referenced by trusted instructions.\n")
    completed = finish(case)["job"]
    assessment = copy.deepcopy(completed["result"])
    assessment["workflow_assessment"]["sources"].append({"path": str(sibling), "sha256": _instruction_fingerprint(sibling)})
    with pytest.raises(ReviewQueueError, match="outside selected scope"):
        verify_workflow_for_action(completed["context"], assessment)


@pytest.mark.parametrize("reference", ["absolute_directory", "relative_directory", "relative_file", "dot_relative_file"])
def test_explicit_directory_and_markdown_file_references_remain_usable(tmp_path, reference):
    rules = tmp_path / "rules"
    rules.mkdir()
    policy = rules / "integration-policy.md"
    policy.write_text("Preserve the selected workflow.\n")
    root = tmp_path / "AGENTS.md"
    text = {"absolute_directory": str(rules), "relative_directory": "rules/",
            "relative_file": "rules/integration-policy.md", "dot_relative_file": "./rules/integration-policy.md"}[reference]
    root.write_text(f"Read applicable guidance at [{text}]({text}).\n")
    sources = [{"path": str(path), "sha256": _instruction_fingerprint(path)} for path in (root, policy)]
    _verify_sources({"sources": sources}, {"instruction_fingerprints": [[str(root), sources[0]["sha256"]]]})
