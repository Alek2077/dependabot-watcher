from __future__ import annotations

import json
import os
import sys
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from dependabot_watcher import supervisor_launch
from dependabot_watcher.config import load_config
from dependabot_watcher.github import GitHubClient
from dependabot_watcher.providers.codex_app_server_settings import CodexAppServerSettings
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_launch import run_configured_supervisor, _session_policy_identity
from test_codex_app_server_supervisor import PROGRAM
from test_execution import refresh_plan_identities, saved_plan
from test_review_queue import decision, persist_plan
from test_supervisor_workspace import workspace_case


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL configured supervisor lifecycle")


@pytest.fixture
def launch_case(workspace_case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    worktree = workspace_case["worktree"]
    source = worktree / "AGENTS.md"
    path = tmp_path / "config.toml"
    path.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n'
        '[supervisor]\nmode = "approval_first"\nprovider = "codex_app_server"\n'
        'model = "gpt-5.6-luna"\nreasoning_effort = "xhigh"\nexpected_account_type = "chatgpt"\n'
        'access = "inherit"\nmodel_use_acknowledged = true\n'
        'workspaces = [{repository = "example-org/example-app", directory = ' + json.dumps(str(worktree))
        + ', instruction_sources = [' + json.dumps(str(source)) + ']}]\n', encoding="utf-8")
    path.chmod(0o600)
    queue = ReviewQueue(load_config(path), tmp_path / "state", config_path=path)
    report = saved_plan()
    report["repositories"][0]["evidence_snapshot"]["base_sha"] = workspace_case["head"]
    report["repositories"][0]["recommended_order"][0]["freshness"]["base_sha"] = workspace_case["head"]
    report = refresh_plan_identities(report)
    collections: list[Any] = []
    def refresh(config: Any, client: Any, **kwargs: Any) -> dict[str, Any]:
        assert config == queue.config and kwargs["config_path"] == path
        assert kwargs["notification_mode"] == "off"
        collections.append(client)
        persist_plan(kwargs["state_directory"], report)
        return {"status": "complete"}
    monkeypatch.setattr(supervisor_launch, "reconcile", refresh)
    program = tmp_path / "protocol.py"
    program.write_text("FAULT=''\nPERMISSION=False\nINSTRUCTIONS=" + repr([str(source)]) + "\n" + PROGRAM)
    launches: list[CodexAppServerSettings] = []
    def command(settings: CodexAppServerSettings, config_overrides: tuple[str, ...] = ()) -> list[str]:
        assert not config_overrides
        launches.append(settings)
        return [sys.executable, "-u", str(program)]
    monkeypatch.setattr(CodexAppServerSettings, "command", command)
    return {**workspace_case, "queue": queue, "path": path, "report": report,
            "collections": collections, "launches": launches, "program": program}


def invoke(case: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    # Collection is replaced at its external boundary; provider transport,
    # worktree preflight, config/queues/jobs/events/observations are real.
    return run_configured_supervisor(case["queue"], cast(GitHubClient, object()), "EXAMPLE-ORG/Example-App",
        permission_presentation=lambda params: ("Synthetic permission", ["allow_once", "deny", "cancel"]), **kwargs)


def test_configured_supervisor_executes_exact_snapshot_through_dynamic_tools_and_records_results(
    launch_case: dict[str, Any],
) -> None:
    case = launch_case
    if not Path('/proc').is_dir() or shutil.which('bwrap') is None:
        pytest.skip('Linux/WSL Bubblewrap validation integration')
    path = case['path']
    path.write_text(path.read_text().replace('model_use_acknowledged = true',
        'model_use_acknowledged = true\nvalidation_authorized = true')
        + '\n[supervisor.validation]\npreparation_authorized = true\nnetwork_authorized = false\n')
    case['queue'].config = load_config(path)
    step = case['report']['repositories'][0]['recommended_order'][0]
    step['identity']['head_sha'] = case['head']
    step['freshness']['head_sha'] = case['head']
    case['report'].update(refresh_plan_identities(case['report']))
    program = case['program']
    dynamic = r'''
        assert 'watcher_validation_prepare' in frame['params']['input'][0]['text']
        proposal = data['context']['proposals'][0]['proposal_id']
        send({'id':70,'method':'item/tool/call','params':{'threadId':'thread1','turnId':'turn1',
              'callId':'prepare1','tool':'watcher_validation_prepare','arguments':{'proposal_id':proposal}}})
        prepared=json.loads(sys.stdin.readline())
        assert prepared['id']==70 and prepared['result']['success'] is True
        snapshot=json.loads(prepared['result']['contentItems'][0]['text'])
        assert snapshot['git_metadata_present'] is False and snapshot['tracked_files']>0
        send({'id':71,'method':'item/tool/call','params':{'threadId':'thread1','turnId':'turn1',
              'callId':'run1','tool':'watcher_validation_run','arguments':{
                'validation_id':snapshot['validation_id'],
                'command':'test ! -e .git && printf verified-sandbox'}}})
        completed=json.loads(sys.stdin.readline())
        assert completed['id']==71 and completed['result']['success'] is True
        execution=json.loads(completed['result']['contentItems'][0]['text'])
        assert execution['output']=='verified-sandbox' and execution['exit_code']==0
        assert execution['head_sha']==snapshot['head_sha'] and execution['tracked_source_changed'] is False
'''
    program.write_text(program.read_text().replace("        proposals=data['context']['proposals']", dynamic + "\n        proposals=data['context']['proposals']"))
    result = invoke(case)
    assert result['job']['status'] == 'completed'
    receipts = list((case['queue'].directory / 'supervisor-validation' / result['job']['job_id']).glob('*.json'))
    assert len(receipts) == 2
    assert not list((case['queue'].directory / 'supervisor-validation' / result['job']['job_id']).glob('validation_*/'))
    assert not (case['worktree'] / '.tmp').exists()
    assert not (case['queue'].directory / 'execution-audit.jsonl').exists()


def test_configured_launch_refreshes_real_worktree_and_uses_only_selected_runtime(launch_case: dict[str, Any]) -> None:
    case = launch_case
    result = invoke(case)
    assert result["reused"] is False and result["job"]["status"] == "completed"
    proof = result["job"]["context"]["workspace_evidence"]
    assert proof["directory"] == str(case["worktree"]) and proof["head_sha"] == case["head"]
    settings = case["launches"][0]
    assert settings.workspace == case["worktree"] and settings.model == "gpt-5.6-luna"
    assert settings.expected_instruction_sources == (case["worktree"] / "AGENTS.md",)
    assert settings.access == "inherit" and settings.full_access_acknowledged is False
    assert len(case["collections"]) == 1
    again = invoke(case)
    assert again["reused"] is True and again["job"]["job_id"] == result["job"]["job_id"]
    assert len(case["launches"]) == 1 and len(case["collections"]) == 2


def test_saved_feedback_continues_only_the_owned_matching_session_and_answers_exact_message(launch_case: dict[str, Any]) -> None:
    case = launch_case
    first = invoke(case)
    message = "Explain the remaining compatibility gap for this exact proposal."
    case["queue"].handle(decision(case["queue"], "feedback", message=message))
    second = invoke(case)
    assert second["reused"] is False and second["job"]["job_id"] != first["job"]["job_id"]
    assert second["job"]["session_id"] == first["job"]["session_id"]
    reply = second["observation"]["analysis"]["replies"][0]
    saved = second["job"]["context"]["proposals"][0]["feedback"][0]
    assert saved["message"] == message and reply["request_id"] == saved["request_id"]
    assert len(case["launches"]) == 2


@pytest.mark.parametrize("change", ["model", "effort", "timeout", "run_budget", "workspace_refresh"])
def test_compatible_settings_changes_reuse_conversation_with_new_checked_job(launch_case: dict[str, Any], change: str) -> None:
    case = launch_case
    first = invoke(case)
    text = case["path"].read_text()
    replacements = {
        "model": ('model = "gpt-5.6-luna"', 'model = "gpt-6.1-sol"'),
        "effort": ('reasoning_effort = "xhigh"', 'reasoning_effort = "low"'),
        "timeout": ('model_use_acknowledged = true', 'model_use_acknowledged = true\ntimeout_seconds = 600'),
        "run_budget": ('model_use_acknowledged = true', 'model_use_acknowledged = true\nmax_jobs_per_run = 2'),
        "workspace_refresh": (', instruction_sources = [', ', refresh_authorized = true, instruction_sources = ['),
    }
    old, new = replacements[change]
    case["path"].write_text(text.replace(old, new))
    case["queue"].config = load_config(case["path"])
    second = invoke(case)
    assert second["reused"] is False and second["job"]["job_id"] != first["job"]["job_id"]
    assert second["job"]["session_id"] == first["job"]["session_id"]
    assert second["observation"]["model"] == case["queue"].config.supervisor.model
    assert second["observation"]["reasoning_effort"] == case["queue"].config.supervisor.reasoning_effort


def test_session_policy_keeps_access_account_and_tool_capabilities_separate_from_resource_limits() -> None:
    policy: dict[str, Any] = {"provider": "codex_app_server", "access": "inherit", "expected_account_type": "chatgpt",
              "validation": {"preparation_authorized": True, "network_authorized": False, "maximum_bytes": 100}}
    identity = _session_policy_identity(policy)
    assert _session_policy_identity({**policy, "validation": {**policy["validation"], "maximum_bytes": 200}}) == identity
    for field, value in (("provider", "another"), ("access", "full"), ("expected_account_type", "apiKey")):
        assert _session_policy_identity({**policy, field: value}) != identity
    assert _session_policy_identity({**policy, "validation": {**policy["validation"], "network_authorized": True}}) != identity


def test_refresh_enabled_case_aliases_reuse_exact_job_and_owned_session(launch_case: dict[str, Any]) -> None:
    case = launch_case
    path = case["path"]
    path.write_text(path.read_text().replace(", instruction_sources = [", ", refresh_authorized = true, instruction_sources = ["))
    case["queue"].config = load_config(path)
    first = invoke(case)
    assert first["job"]["status"] == "completed"
    again = run_configured_supervisor(case["queue"], cast(GitHubClient, object()), "example-org/example-app",
        permission_presentation=lambda params: ("Synthetic permission", ["allow_once", "deny", "cancel"]))
    last = invoke(case)
    for result in (again, last):
        assert result["reused"] is True
        assert result["job"]["job_id"] == first["job"]["job_id"]
        assert result["job"]["session_id"] == first["job"]["session_id"]
        assert result["job"]["context_digest"] == first["job"]["context_digest"]
    assert len(case["launches"]) == 1


def test_failed_same_context_is_not_automatically_replayed(launch_case: dict[str, Any]) -> None:
    case = launch_case
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    with pytest.raises(ReviewQueueError):
        invoke(case)
    with pytest.raises(ReviewQueueError, match="recovery"):
        invoke(case)
    assert len(case["launches"]) == 1
    jobs = SupervisorJobs(case["queue"])._load()["jobs"]
    assert len(jobs) == 1 and next(iter(jobs.values()))["status"] == "failed"


def test_deliberate_retry_keeps_failure_and_does_not_replay_a_retry_request(launch_case: dict[str, Any]) -> None:
    case = launch_case
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    with pytest.raises(ReviewQueueError):
        invoke(case)
    jobs = SupervisorJobs(case["queue"])
    failed = next(iter(jobs._load()["jobs"].values()))
    case["program"].write_text(case["program"].read_text().replace("FAULT='failed_turn'", "FAULT=''").replace("'thread1'", "'thread2'"))
    fresh = invoke(case, retry_job_id=failed["job_id"])
    assert fresh["job"]["status"] == "completed" and fresh["job"]["retry_of"] == failed["job_id"]
    assert fresh["job"]["job_id"] != failed["job_id"]
    assert jobs.read(failed["job_id"]) == failed
    with pytest.raises(ReviewQueueError, match="Retry"):
        invoke(case, retry_job_id=failed["job_id"])
    assert invoke(case)["reused"] is True
    assert len(case["launches"]) == 2 and len(jobs._load()["jobs"]) == 2


def test_retry_failure_requires_a_new_exact_choice(launch_case: dict[str, Any]) -> None:
    case = launch_case
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    with pytest.raises(ReviewQueueError):
        invoke(case)
    jobs = SupervisorJobs(case["queue"])
    first = next(iter(jobs._load()["jobs"]))
    case["program"].write_text(case["program"].read_text().replace("'thread1'", "'thread2'"))
    with pytest.raises(ReviewQueueError):
        invoke(case, retry_job_id=first)
    history = jobs._load()["jobs"]
    second = next(identifier for identifier, record in history.items() if record.get("retry_of") == first)
    assert second != first and history[second]["retry_of"] == first
    with pytest.raises(ReviewQueueError, match="recovery"):
        invoke(case)
    with pytest.raises(ReviewQueueError, match="Retry"):
        invoke(case, retry_job_id=first)
    assert len(case["launches"]) == 2


def test_retry_after_failed_continuation_resumes_compatible_completed_session(launch_case: dict[str, Any]) -> None:
    case = launch_case
    completed = invoke(case)
    case["queue"].handle(decision(case["queue"], "feedback", message="Continue the completed investigation"))
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    with pytest.raises(ReviewQueueError):
        invoke(case)
    jobs = SupervisorJobs(case["queue"])
    failed = next(record for record in jobs._load()["jobs"].values() if record["status"] == "failed")
    assert failed["session_id"] == completed["job"]["session_id"]
    case["program"].write_text(case["program"].read_text().replace("FAULT='failed_turn'", "FAULT=''"))
    retried = invoke(case, retry_job_id=failed["job_id"])
    assert retried["job"]["status"] == "completed" and retried["job"]["session_id"] == completed["job"]["session_id"]
    assert retried["job"]["retry_of"] == failed["job_id"] and jobs.read(failed["job_id"]) == failed
    assert len(case["launches"]) == 3


@pytest.mark.parametrize("fault", ["wrong_id", "changed_context"])
def test_retry_requires_the_current_scoped_failed_attempt(launch_case: dict[str, Any], fault: str) -> None:
    case = launch_case
    case["program"].write_text(case["program"].read_text().replace("FAULT=''", "FAULT='failed_turn'"))
    with pytest.raises(ReviewQueueError):
        invoke(case)
    failed = next(iter(SupervisorJobs(case["queue"])._load()["jobs"]))
    if fault == "changed_context":
        case["queue"].handle(decision(case["queue"], "feedback", message="Different investigation context"))
    else:
        failed = "job_" + "f" * 32
    with pytest.raises(ReviewQueueError, match="Retry"):
        invoke(case, retry_job_id=failed)
    assert len(case["launches"]) == 1


@pytest.mark.parametrize("fault", ["scope", "unbound", "no_consumer", "base"])
def test_unsafe_or_unconnected_launch_makes_no_model_call(launch_case: dict[str, Any], fault: str) -> None:
    case = launch_case
    if fault == "scope":
        with pytest.raises(ReviewQueueError, match="scope"):
            run_configured_supervisor(case["queue"], cast(GitHubClient, object()), "other/repository")
    elif fault == "unbound":
        queue = ReviewQueue(case["queue"].config, case["queue"].directory)
        with pytest.raises(ReviewQueueError, match="saved configuration"):
            run_configured_supervisor(queue, cast(GitHubClient, object()), "example-org/example-app")
    elif fault == "no_consumer":
        with pytest.raises(ReviewQueueError, match="presentation"):
            run_configured_supervisor(case["queue"], cast(GitHubClient, object()), "example-org/example-app")
    else:
        case["report"]["repositories"][0]["evidence_snapshot"]["base_sha"] = "a" * 40
        refresh_plan_identities(case["report"])
        with pytest.raises(ReviewQueueError, match="collected base"):
            invoke(case)
    assert case["launches"] == []
    assert len(case["collections"]) == int(fault == "base")
