from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.providers.codex_app_server_settings import CodexAppServerSettings
from dependabot_watcher.providers.codex_app_server_transport import CodexAppServerTransport
from dependabot_watcher.providers.codex_app_server_supervisor import CodexAppServerSupervisor, _available_decisions, _output_schema
from dependabot_watcher.config import load_config
from dependabot_watcher.review_queue import ReviewConfigurationChanged, ReviewQueue, ReviewQueueError
from dependabot_watcher.runtime_state import load_private_json, run_lock
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_permissions import SupervisorPermissions
from dependabot_watcher.supervisor_runner import SupervisorRunner
from dependabot_watcher.supervisor_events import SupervisorEvents
from dependabot_watcher.supervisor_workspace import _instruction_fingerprint
from test_review_queue import decision, file_bound_queue, workspace_receipt
from test_supervisor_runner import FixtureProvider, runner_case


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL agent runtime")


@pytest.mark.parametrize('turns', [[{'id': 'turn'}] * 65,
    [{'id': 'turn', 'items': [{'type': 'agentMessage', 'text': 'private transcript'}]}],
    [{'id': 'turn', 'transcript': 'unexpected'}], [{'id': 0}]])
def test_historical_turn_response_must_be_bounded_and_metadata_only(turns: Any) -> None:
    with pytest.raises(ReviewQueueError, match='historical turns'):
        CodexAppServerSupervisor._historical_turn_ids(turns)


def test_historical_turn_metadata_projection_preserves_ids() -> None:
    assert CodexAppServerSupervisor._historical_turn_ids(
        [{'id': 'turn', 'items': [], 'itemsView': 'notLoaded'}]) == {'turn'}


@pytest.mark.parametrize('turns', [[{'id': 'turn'}, {'id': 'turn'}],
    [{'id': 'turn', 'itemsView': 'full'}], [{'id': 'turn', 'startedAt': True}],
    [{'id': 'turn', 'rootTurnId': 'invalid id'}], [{'id': 'turn', 'status': {}}],
    [{'id': 'turn', 'error': {'message': 'x' * 5000}}]])
def test_historical_metadata_rejects_duplicates_loaded_views_and_invalid_values(turns: Any) -> None:
    with pytest.raises(ReviewQueueError):
        CodexAppServerSupervisor._historical_turn_ids(turns)


def test_bounded_history_page_keeps_conversation_when_unloaded_history_exists() -> None:
    response = {'thread': {'turns': []}, 'initialTurnsPage': {'data': [{'id': 'prior-turn'}],
                'nextCursor': 'opaque_unloaded_history', 'backwardsCursor': None}}
    assert CodexAppServerSupervisor._historical_response_ids(response) == {'prior-turn'}


@pytest.mark.parametrize('page', [{'data': [], 'nextCursor': 1}, {'data': [], 'backwardsCursor': 'x' * 4097},
    {'data': [], 'unknown': True}, {'data': [{'id': f'page{i}'} for i in range(33)]}])
def test_history_page_envelope_and_combined_identity_count_are_bounded(page: dict[str, Any]) -> None:
    response = {'thread': {'turns': [{'id': f'thread{i}'} for i in range(32)]}, 'initialTurnsPage': page}
    with pytest.raises(ReviewQueueError):
        CodexAppServerSupervisor._historical_response_ids(response)


PROGRAM = r'''
import json, sys
from dependabot_watcher.validation_tools import validation_tool_specs
validation_config=None
def send(frame):
    print(json.dumps(frame),flush=True)
def response(identifier,result):
    send({'id':identifier,'result':result})
for line in sys.stdin:
    frame=json.loads(line)
    method=frame.get('method')
    if method=='initialize':
        response(frame['id'],{'userAgent':'fixture/0.160.0'})
    elif method=='account/read':
        response(frame['id'],{'account':{'type':'apiKey' if FAULT=='wrong_account' else 'chatgpt'}})
    elif method in ('thread/start','thread/resume'):
        params=frame['params']
        validation_config=params['config'].get('mcp_servers.watcher_validation')
        response(frame['id'],{'thread':{'id':'thread1','turns':[{'id':'prior-turn'}]},'cwd':params['cwd'],
            'model':params['model'],'reasoningEffort':params['config']['model_reasoning_effort'],
            'modelProvider':'openai','approvalPolicy':'on-request','approvalsReviewer':'user',
            'sandbox':{'type':'workspaceWrite'},'instructionSources':INSTRUCTIONS,
            'activePermissionProfile':{'id':'watcher-fixture'}})
    elif method=='mcpServerStatus/list':
        if 'threadId' not in frame['params']:
            response(frame['id'],{'data':[],'nextCursor':None})
            continue
        assert validation_config is not None
        response(frame['id'],{'data':[{'name':'watcher_validation','runtimeStatus':'connected',
            'serverInfo':{'name':'watcher_validation','version':'1'},'toolsError':None,
            'tools':{s['name']:{k:v for k,v in s.items() if k!='type'} for s in validation_tool_specs()}}],
            'nextCursor':None})
    elif method=='turn/start':
        data=json.loads(frame['params']['input'][0]['text'].rsplit('\n',1)[-1])
        schema=frame['params']['outputSchema']
        assert schema['properties']['job_id']['enum']==[data['job_id']]
        assert schema['properties']['context_digest']['enum']==[data['context_digest']]
        assert schema['additionalProperties'] is False
        result_schema=schema['properties']['analysis']['properties']
        assert result_schema['recommendations']['items']['properties']['proposal_id']['enum']==sorted(p['proposal_id'] for p in data['context']['proposals'])
        for p in data['context']['proposals']:
            for m in p['feedback']:
                assert any(branch['properties']['proposal_id']['enum']==[p['proposal_id']]
                    and m['request_id'] in branch['properties']['request_id']['enum']
                    for branch in result_schema['replies']['items']['anyOf'])
        response(frame['id'],{'turn':{'id':'turn1','status':'inProgress'}})
        if FAULT=='historical_usage':
            send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread1','turnId':'prior-turn',
                  'tokenUsage':{'last':{'inputTokens':999,'cachedInputTokens':0,'outputTokens':999,'reasoningOutputTokens':0}}}})
        if PERMISSION:
            send({'id':7,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread1',
                  'turnId':'wrong-turn' if FAULT=='wrong_turn' else 'turn1','itemId':'item1','startedAtMs':1,
                  'availableDecisions':([] if FAULT=='available_empty' else 'accept' if FAULT=='available_wrong_type'
                    else ['unknown'] if FAULT=='available_unknown_choice' else ['accept','decline','cancel']),
                  'command':'cat synthetic-fixture.txt',
                  'sensitive_marker':'PRIVATE_TEST_ONLY'}})
            if FAULT=='unanswered_resolution':
                send({'method':'serverRequest/resolved','params':{'threadId':'thread1','requestId':7}})
            grant=json.loads(sys.stdin.readline())
            if grant.get('id') != 7:
                sys.exit(2)
            if FAULT!='missing_resolution':
                send({'method':'serverRequest/resolved','params':{'threadId':'thread1','requestId':7}})
            command_status='declined' if grant['result']['decision'] in ('decline','cancel') else 'completed'
            if FAULT in ('denied_completed','network_denied_completed','mismatched_command'):
                command_status='completed'
            send({'method':'item/completed','params':{'threadId':'thread1','turnId':'turn1',
                  'item':{'id':'item2' if FAULT=='mismatched_command' else 'item1','type':'commandExecution','status':command_status,
                          'exitCode':None if command_status=='declined' else 0,
                          'command':'PRIVATE_TEST_ONLY','aggregatedOutput':'PRIVATE_TEST_ONLY'}}})
        proposals=data['context']['proposals']
        analysis={'summary':'Observed fixture investigation, no real GitHub action.',
                  'recommendations':[{'proposal_id':p['proposal_id'],'conclusion':'investigate','explanation':'Review compatibility.'} for p in proposals],
                  'replies':[{'proposal_id':p['proposal_id'],'request_id':m['request_id'],'message':'Exact fixture question answered.'} for p in proposals for m in p['feedback']]}
        final={'job_id':'wrong-job' if FAULT=='wrong_job' else data['job_id'],
               'context_digest':data['context_digest'],'analysis':analysis}
        if FAULT=='unknown_proposal':
            analysis['recommendations'][0]['proposal_id']='proposal_'+'f'*16
        send({'method':'item/completed','params':{'threadId':'thread1','turnId':'turn1',
              'item':{'id':'answer1','type':'agentMessage','text':json.dumps(final)}}})
        if FAULT!='missing_usage':
            counts={'inputTokens':10,'cachedInputTokens':5,'outputTokens':3,'reasoningOutputTokens':1}
            def wire_counts(values):
                return {**values,'totalTokens':values['inputTokens']+values['outputTokens'],'cacheWriteInputTokens':0}
            usage={'last':wire_counts(counts),'total':wire_counts({key:value+100 for key,value in counts.items()})}
            if FAULT=='missing_total':
                del usage['total']
            elif FAULT=='bad_usage_shape':
                usage['last']=[]
            elif FAULT=='boolean_usage':
                usage['total']['inputTokens']=True
            elif FAULT=='negative_usage':
                usage['last']['inputTokens']=-1
            elif FAULT=='oversized_usage':
                usage['total']['inputTokens']=2**63
            elif FAULT=='total_below_last':
                usage['total']['inputTokens']=9
                usage['total']['totalTokens']=112
            elif FAULT=='unknown_usage_counter':
                usage['last']['futureTokens']=7
            elif FAULT=='cache_write_usage':
                usage['last']['cacheWriteInputTokens']=3
            elif FAULT=='bad_total_tokens':
                usage['last']['totalTokens']=14
            elif FAULT=='missing_total_tokens':
                del usage['last']['totalTokens']
            elif FAULT=='zero_first_request':
                usage['last']=wire_counts({key:0 for key in counts})
            def token_event(snapshot):
                send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread1','turnId':'turn1',
                      'tokenUsage':snapshot}})
            if FAULT in ('initial_compaction_usage','initial_compaction_only','initial_unmarked_compaction'):
                if FAULT!='initial_unmarked_compaction':
                    send({'method':'item/started','params':{'threadId':'thread1','turnId':'turn1',
                          'item':{'id':'compact0','type':'contextCompaction'}}})
                compact_usage={'last':wire_counts({key:0 for key in counts}),
                    'total':wire_counts({key:100 for key in counts})}
                compact_usage['last']['totalTokens']=84427
                token_event(compact_usage)
                if FAULT!='initial_unmarked_compaction':
                    send({'method':'item/completed','params':{'threadId':'thread1','turnId':'turn1',
                          'item':{'id':'compact0','type':'contextCompaction'}}})
                    token_event(compact_usage)
                if FAULT=='initial_compaction_only':
                    send({'method':'turn/completed','params':{'threadId':'thread1','turn':{'id':'turn1','status':'completed'}}})
                    continue
            token_event(usage)
            if FAULT in ('compaction_usage','unmarked_compaction_usage','compaction_changed_total'):
                if FAULT!='unmarked_compaction_usage':
                    send({'method':'item/started','params':{'threadId':'thread1','turnId':'turn1',
                          'item':{'id':'compact1','type':'contextCompaction'}}})
                compact_usage={'last':wire_counts({key:0 for key in counts}), 'total':dict(usage['total'])}
                compact_usage['last']['totalTokens']=85895
                if FAULT=='compaction_changed_total':
                    compact_usage['total']['inputTokens']+=1
                    compact_usage['total']['totalTokens']+=1
                token_event(compact_usage)
                send({'method':'item/completed','params':{'threadId':'thread1','turnId':'turn1',
                      'item':{'id':'compact1','type':'contextCompaction'}}})
                if FAULT=='compaction_usage':
                    token_event(compact_usage)
                    token_event({'last':wire_counts(counts),'total':wire_counts({key:value+100+counts[key] for key,value in counts.items()})})
            if FAULT in ('multiple_usage','duplicate_usage','regressed_usage','missing_request_usage','contradictory_duplicate'):
                next_usage={'last':wire_counts(counts),'total':wire_counts({key:value+100+counts[key] for key,value in counts.items()})}
                if FAULT=='regressed_usage':
                    next_usage['total']['inputTokens']=109
                    next_usage['total']['totalTokens']=215
                elif FAULT=='missing_request_usage':
                    next_usage['total']['inputTokens']=130
                    next_usage['total']['totalTokens']=236
                elif FAULT=='contradictory_duplicate':
                    next_usage['last']=wire_counts({key:value*2 for key,value in counts.items()})
                    next_usage['total']=dict(usage['total'])
                token_event(next_usage)
                if FAULT=='duplicate_usage':
                    token_event(next_usage)
        send({'method':'turn/completed','params':{'threadId':'thread1','turn':{'id':'turn1',
              'status':'failed' if FAULT=='failed_turn' else 'completed'}}})
'''


def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fault: str = "", permission: bool = False,
             session: str | None = None, presentation: bool = True) -> CodexAppServerSupervisor:
    instruction = tmp_path / "AGENTS.md"
    program = tmp_path / "runtime.py"
    program.write_text(f"FAULT={fault!r}\nPERMISSION={permission!r}\nINSTRUCTIONS={[str(instruction)]!r}\n" + PROGRAM)
    monkeypatch.setattr(CodexAppServerSettings, "command", lambda self, config_overrides=(): [sys.executable, "-u", str(program)])
    settings = CodexAppServerSettings(tmp_path, "gpt-5.6-luna", "xhigh", expected_instruction_sources=(instruction,),
                                      access="restricted", permission_profile="watcher-fixture", timeout_seconds=10)
    return CodexAppServerSupervisor(settings, expected_account_type="chatgpt", session_id=session,
                                    permission_presentation=(lambda params: ("Read only the synthetic test fixture.", ["allow_once", "deny", "cancel"])) if presentation else None)


def test_runner_persists_effective_session_before_result_and_answers_exact_feedback(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, _unused, job = runner_case
    result = SupervisorRunner(jobs, provider(tmp_path, monkeypatch)).run(job["job_id"])
    assert result["job"]["status"] == "completed"
    runtime = load_private_json(jobs.queue.directory, Path("supervisor-runtime") / f'{job["job_id"]}.json')
    assert runtime["job_id"] == job["job_id"] and runtime["context_digest"] == job["context_digest"]
    assert runtime["effective_settings"]["session_id"] == "thread1"
    assert runtime["effective_settings"]["access"] == "restricted"
    assert result["observation"]["provider"] == "codex_app_server"
    assert result["observation"]["analysis"]["replies"][0]["request_id"] == job["context"]["proposals"][0]["feedback"][0]["request_id"]
    assert SupervisorPermissions(jobs).snapshot(job["repository"])["connections"][0]["status"] == "closed"


@pytest.mark.parametrize("revoke", ["configuration", "instructions", "instruction_after_consume"])
def test_file_authority_revocation_stops_actual_child_wait_and_invalidates_pending_access(
    file_bound_queue: tuple[ReviewQueue, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    revoke: str,
) -> None:
    queue, path = file_bound_queue
    source = tmp_path / "AGENTS.md"
    if revoke != "configuration":
        source.write_text("First trusted policy.\n")
        receipt = workspace_receipt(queue)
        receipt["instruction_fingerprints"][0][1] = _instruction_fingerprint(source)
        queue.record_workspace_evidence(receipt)
    if revoke == "instruction_after_consume":
        original_consume = SupervisorPermissions.consume
        def consume_then_revoke(self: SupervisorPermissions, connection: str, permission: str, digest: str) -> str | None:
            choice = original_consume(self, connection, permission, digest)
            if choice is not None:
                source.write_text("Changed trusted policy.\n")
            return choice
        monkeypatch.setattr(SupervisorPermissions, "consume", consume_then_revoke)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    instance = provider(tmp_path, monkeypatch, permission=True)
    if revoke != "configuration":
        instance.settings = replace(instance.settings, access="inherit", permission_profile=None)
    outcomes: list[Any] = []

    def run() -> None:
        try:
            outcomes.append(SupervisorRunner(jobs, instance).run(job["job_id"]))
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    journal = SupervisorPermissions(jobs)
    records: list[Any] = []
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            records = journal.snapshot(job["repository"])["connections"]
            if records and records[0]["requests"]:
                break
            time.sleep(.02)
        else:
            pytest.fail("Actual protocol child never published its pending access request")
        # No UI response: changing the saved policy must stop polling/transport,
        # not wait until the child timeout or grant the outstanding request.
        if revoke == "configuration":
            path.write_text(path.read_text().replace('reasoning_effort = "xhigh"', 'reasoning_effort = "low"'))
        elif revoke == "instructions":
            source.write_text("Changed trusted policy.\n")
        else:
            connection = records[0]
            requested = next(iter(connection["requests"].values()))
            journal.decide(connection["connection_id"], requested["permission_id"], requested["digest"], "allow_once")
    finally:
        worker.join(timeout=4)
    assert not worker.is_alive() and len(outcomes) == 1
    assert isinstance(outcomes[0], ReviewConfigurationChanged)
    assert jobs.read(job["job_id"])["status"] == "interrupted"
    connection = next(iter(journal._load()["connections"].values()))
    requested = next(iter(connection["requests"].values()))
    assert connection["status"] == "closed" and requested["status"] == "invalidated"
    assert requested["decision"] == ("allow_once" if revoke == "instruction_after_consume" else None)
    assert not (queue.directory / "supervisor-observations" / f'{job["job_id"]}.json').exists()
    if revoke == "configuration":
        refreshed = SupervisorPermissions(SupervisorJobs(ReviewQueue(load_config(path), queue.directory, config_path=path)))
        assert refreshed.snapshot(job["repository"])["connections"][0]["current_context"] is False


@pytest.mark.parametrize("change", ["workspace", "instructions", "model", "effort", "access", "account", "validation", "override", "timeout"])
def test_receipt_bound_provider_refuses_independent_launch_settings_before_transport(
    file_bound_queue: tuple[ReviewQueue, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    queue, _ = file_bound_queue
    source = tmp_path / "AGENTS.md"
    source.write_text("Selected trusted policy.\n")
    receipt = workspace_receipt(queue)
    receipt["instruction_fingerprints"][0][1] = _instruction_fingerprint(source)
    queue.record_workspace_evidence(receipt)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    instance = provider(tmp_path, monkeypatch)
    instance.settings = replace(instance.settings, access="inherit", permission_profile=None)
    if change == "workspace":
        other = tmp_path / "other"
        other.mkdir()
        instance.settings = replace(instance.settings, workspace=other)
    elif change == "instructions":
        instance.settings = replace(instance.settings, expected_instruction_sources=(tmp_path / "other.md",))
    elif change == "model":
        instance.settings = replace(instance.settings, model="different-model")
    elif change == "effort":
        instance.settings = replace(instance.settings, reasoning_effort="low")
    elif change == "access":
        instance.settings = replace(instance.settings, access="restricted", permission_profile="watcher-fixture")
    elif change == "account":
        instance.expected_account_type = "apiKey"
    elif change == "validation":
        instance.validation_authorized = True
    elif change == "override":
        instance.config_overrides = ('model="different-model"',)
    else:
        instance.settings = replace(instance.settings, timeout_seconds=901)
    def forbidden_command(*args: Any, **kwargs: Any) -> list[str]:
        raise AssertionError("A mismatched provider must be rejected before even resolving its command")
    monkeypatch.setattr(CodexAppServerSettings, "command", forbidden_command)
    with pytest.raises(ReviewQueueError, match="selected|saved job policy"):
        SupervisorRunner(jobs, instance).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"
    assert not (queue.directory / "supervisor-runtime" / f'{job["job_id"]}.json').exists()
    assert SupervisorPermissions(jobs).snapshot(job["repository"])["connections"] == []


def test_matching_receipt_bound_provider_completes_through_actual_protocol_child(
    file_bound_queue: tuple[ReviewQueue, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, _ = file_bound_queue
    source = tmp_path / "AGENTS.md"
    source.write_text("Selected trusted policy.\n")
    receipt = workspace_receipt(queue)
    receipt["instruction_fingerprints"][0][1] = _instruction_fingerprint(source)
    queue.record_workspace_evidence(receipt)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    instance = provider(tmp_path, monkeypatch)
    instance.settings = replace(instance.settings, access="inherit", permission_profile=None)
    result = SupervisorRunner(jobs, instance).run(job["job_id"])
    assert result["job"]["status"] == "completed"
    runtime = load_private_json(queue.directory, Path("supervisor-runtime") / f'{job["job_id"]}.json')
    assert runtime["effective_settings"]["workspace"] == receipt["directory"]
    assert runtime["effective_settings"]["instruction_sources"] == [str(source)]


def test_returned_runtime_receipt_cannot_redirect_a_selected_instruction_workspace(
    file_bound_queue: tuple[ReviewQueue, Path],
) -> None:
    queue, _ = file_bound_queue
    source = Path(queue.config.supervisor.workspaces[0].instruction_sources[0])
    source.write_text("Selected trusted policy.\n")
    receipt = workspace_receipt(queue)
    receipt["instruction_fingerprints"][0][1] = _instruction_fingerprint(source)
    queue.record_workspace_evidence(receipt)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare("example-org/example-app")
    job = jobs.start(job["job_id"])
    events = SupervisorEvents(jobs, job)
    try:
        with pytest.raises(ReviewQueueError, match="selected"):
            events.connected("thread1", {"session_id": "thread1", "workspace": "/different/workspace",
                "instruction_sources": [str(source)], "model": queue.config.supervisor.model,
                "reasoning_effort": queue.config.supervisor.reasoning_effort, "access": "inherit",
                "account_type": "chatgpt", "validation_authorized": False})
        assert jobs.read(job["job_id"])["session_id"] is None
        assert not (queue.directory / "supervisor-runtime" / f'{job["job_id"]}.json').exists()
    finally:
        events.close()
        jobs.fail(job["job_id"])


def test_same_session_continuation_excludes_historical_usage_from_current_result(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, _unused, job = runner_case
    SupervisorRunner(jobs, provider(tmp_path, monkeypatch)).run(job["job_id"])
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Continue this exact maintenance job"))
    followup = jobs.prepare(job["repository"])
    result = SupervisorRunner(jobs, provider(tmp_path, monkeypatch, session="thread1", fault="historical_usage")).run(followup["job_id"])
    assert result["job"]["session_id"] == "thread1"
    assert result["observation"]["usage"]["input_tokens"] == 10
    assert len(result["observation"]["analysis"]["replies"]) == 2


@pytest.mark.parametrize("fault", ["multiple_usage", "duplicate_usage", "compaction_usage"])
def test_current_turn_counts_all_requests_once_not_prior_thread_totals(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    jobs, _unused, job = runner_case
    SupervisorRunner(jobs, provider(tmp_path, monkeypatch)).run(job["job_id"])
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Answer this follow-up only."))
    followup = jobs.prepare(job["repository"])
    result = SupervisorRunner(jobs, provider(tmp_path, monkeypatch, session="thread1", fault=fault)).run(followup["job_id"])
    expected = {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 6, "reasoning_output_tokens": 2}
    assert result["observation"]["usage"] == expected
    observed = load_private_json(jobs.queue.directory, Path("supervisor-observations") / f'{followup["job_id"]}.json')
    assert observed["usage"] == expected
    assert result["job"]["session_id"] == "thread1"


def test_resumed_turn_initial_compaction_is_context_not_an_inference_request(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, _unused, job = runner_case
    SupervisorRunner(jobs, provider(tmp_path, monkeypatch)).run(job["job_id"])
    jobs.queue.handle(decision(jobs.queue, "feedback", message="Continue after automatic compaction."))
    followup = jobs.prepare(job["repository"])
    result = SupervisorRunner(jobs, provider(tmp_path, monkeypatch, session="thread1",
        fault="initial_compaction_usage")).run(followup["job_id"])
    assert result["job"]["session_id"] == "thread1"
    assert result["observation"]["usage"] == {
        "input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 3, "reasoning_output_tokens": 1}


@pytest.mark.parametrize("fault", ["missing_total", "bad_usage_shape", "boolean_usage", "negative_usage",
                                  "oversized_usage", "total_below_last", "regressed_usage", "missing_request_usage",
                                  "unknown_usage_counter", "cache_write_usage", "bad_total_tokens", "missing_total_tokens",
                                  "contradictory_duplicate", "unmarked_compaction_usage", "compaction_changed_total",
                                  "initial_unmarked_compaction", "zero_first_request"])
def test_incomplete_or_contradictory_usage_does_not_claim_completed_accounting(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    jobs, _unused, job = runner_case
    with pytest.raises(ReviewQueueError, match="usage"):
        SupervisorRunner(jobs, provider(tmp_path, monkeypatch, fault=fault)).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"
    assert not (jobs.queue.directory / "supervisor-observations" / f'{job["job_id"]}.json').exists()
    assert all(record["status"] == "closed" for record in SupervisorPermissions(jobs).snapshot(job["repository"])["connections"])


def test_compaction_without_an_observed_request_cannot_complete_accounting(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, _unused, job = runner_case
    with pytest.raises(ReviewQueueError, match="fully observed turn"):
        SupervisorRunner(jobs, provider(tmp_path, monkeypatch, fault="initial_compaction_only")).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"
    assert not (jobs.queue.directory / "supervisor-observations" / f'{job["job_id"]}.json').exists()


@pytest.mark.parametrize("choice", ["allow_once", "deny", "cancel"])
@pytest.mark.parametrize("fault", ["", "missing_resolution", "denied_completed", "network_denied_completed", "mismatched_command", "contended_poll", "contended_request", "contended_resolution"])
def test_real_child_waits_for_exact_durable_decision_and_records_resolution(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    choice: str, fault: str,
) -> None:
    jobs, _unused, job = runner_case
    results: list[Any] = []
    def run() -> None:
        try:
            results.append(SupervisorRunner(jobs, provider_instance).run(job["job_id"]))
        except Exception as exc:
            results.append(exc)
    provider_instance = provider(tmp_path, monkeypatch, permission=True, fault=fault)
    contended_polls: list[bool] = []
    responses: list[bool] = []
    if fault.startswith("contended_"):
        operation = {"contended_poll": "consume", "contended_request": "request", "contended_resolution": "resolve"}[fault]
        original_operation = getattr(SupervisorPermissions, operation)
        def contended_operation(self: SupervisorPermissions, *arguments: Any, **keywords: Any) -> Any:
            if not contended_polls:
                contended_polls.append(True)
                with run_lock(self.queue.directory):
                    return original_operation(self, *arguments, **keywords)
            return original_operation(self, *arguments, **keywords)
        monkeypatch.setattr(SupervisorPermissions, operation, contended_operation)
        original_respond = CodexAppServerTransport.respond
        def record_response(self: CodexAppServerTransport, request: dict[str, Any], result: dict[str, Any]) -> None:
            if request.get("method") == "item/commandExecution/requestApproval":
                responses.append(True)
            original_respond(self, request, result)
        monkeypatch.setattr(CodexAppServerTransport, "respond", record_response)
    if fault == "network_denied_completed":
        program = tmp_path / "runtime.py"
        program.write_text(program.read_text().replace("'startedAtMs':1,", "'startedAtMs':1, 'networkApprovalContext':{'host':'fixture.invalid','protocol':'https'},"))
    worker = threading.Thread(target=run)
    worker.start()
    journal = SupervisorPermissions(jobs)
    deadline = time.monotonic() + 5
    connection: dict[str, Any] | None = None
    requested: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        for candidate in journal.snapshot(job["repository"])["connections"]:
            if candidate["requests"]:
                connection = candidate
                requested = next(iter(candidate["requests"].values()))
        if requested:
            break
        time.sleep(.02)
    try:
        assert connection is not None and requested is not None and requested["status"] == "waiting"
        assert jobs.read(job["job_id"])["status"] == "running" and results == []
        assert "PRIVATE_TEST_ONLY" not in json.dumps(journal.snapshot(job["repository"]))
        journal.decide(connection["connection_id"], requested["permission_id"], requested["digest"], choice)
    finally:
        worker.join(timeout=12)
    assert not worker.is_alive() and len(results) == 1
    if fault.startswith("contended_"):
        assert contended_polls == [True] and responses == [True]
    saved = journal.snapshot(job["repository"])["connections"][0]
    assert saved["status"] == "closed"
    if fault in {"missing_resolution", "mismatched_command"} or fault == "denied_completed" and choice in {"deny", "cancel"}:
        assert isinstance(results[0], ReviewQueueError)
        assert jobs.read(job["job_id"])["status"] == "failed"
        # A delivered decision can be resolved before a contradictory command
        # completion arrives. Retain that real delivery; reject job completion.
        assert saved["requests"][requested["permission_id"]]["status"] == ("resolved" if fault in {"denied_completed", "mismatched_command"} else "invalidated")
        assert not (jobs.queue.directory / "supervisor-observations" / f'{job["job_id"]}.json').exists()
    else:
        assert not isinstance(results[0], Exception)
        assert saved["requests"][requested["permission_id"]]["status"] == "resolved"
        expected = ({"status": "declined", "exit_code": None}
                    if choice in {"deny", "cancel"} and fault != "network_denied_completed"
                    else {"status": "completed", "exit_code": 0})
        assert results[0]["observation"]["command_observations"] == [expected]
        observed = load_private_json(jobs.queue.directory, Path("supervisor-observations") / f'{job["job_id"]}.json')
        assert observed["command_observations"] == [expected]
        assert "PRIVATE_TEST_ONLY" not in json.dumps(observed)
    assert saved["requests"][requested["permission_id"]]["decision"] == choice


@pytest.mark.parametrize("fault", ["wrong_account", "wrong_job", "unknown_proposal", "failed_turn", "missing_usage", "wrong_turn",
                                  "available_empty", "available_wrong_type", "available_unknown_choice", "unanswered_resolution"])
def test_failed_or_misattributed_runtime_does_not_complete_or_leave_live_consent(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    jobs, _unused, job = runner_case
    with pytest.raises(ReviewQueueError):
        SupervisorRunner(jobs, provider(tmp_path, monkeypatch, fault=fault,
                                       permission=fault in {"wrong_turn", "unanswered_resolution"} or fault.startswith("available_"))).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"
    assert all(c["status"] == "closed" for c in SupervisorPermissions(jobs).snapshot(job["repository"])["connections"])


def schema_job(proposals: list[dict[str, Any]], retired: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"job_id": "job_" + "a" * 32, "context_digest": "b" * 64,
            "context": {"proposals": proposals, "retired_proposals": retired or []}}


def test_post_merge_generation_requires_an_exact_action_verdict_without_changing_ordinary_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = schema_job([])
    runtime = provider(tmp_path, monkeypatch)
    assert "maintenance_assessment" not in runtime._prompt(job).split("Job context/evidence")[0]
    ordinary = _output_schema(job)["properties"]["analysis"]
    assert "maintenance_assessment" not in ordinary["properties"]
    job["context"]["maintenance_completion"] = {"obligation": {"action_id": "action_" + "c" * 24}}
    analysis = _output_schema(job)["properties"]["analysis"]
    assert "maintenance_assessment" in analysis["required"]
    verdict = analysis["properties"]["maintenance_assessment"]
    assert verdict["properties"]["action_id"]["enum"] == ["action_" + "c" * 24]
    assert set(verdict["properties"]["conclusion"]["enum"]) == {"ready_for_completion", "blocked", "needs_attention"}
    assert verdict["additionalProperties"] is False
    return_template = runtime._prompt(job).split("Return only JSON", 1)[1].split("Job context/evidence", 1)[0]
    assert "maintenance_assessment: {action_id, conclusion, explanation}" in return_template
    guidance = runtime._prompt(job).split("Job context/evidence", 1)[0]
    assert "absent local integration receipts are expected at this stage" in guidance
    assert "watcher_validation_prepare accepts the exact obligation.action_id" in guidance
    assert "Prioritize this action's required follow-up" in guidance
    assert "A maintenance batch is a proposed sequence, not authority to merge several PRs" in guidance
    assert "Refresh after each merge" in guidance


def test_generation_schema_binds_current_recommendations_and_exact_current_or_retired_message_pairs() -> None:
    first, second, old = ("proposal_" + letter * 16 for letter in "abc")
    schema = _output_schema(schema_job([
        {"proposal_id": first, "feedback": [{"request_id": "decision_1_first"}]},
        {"proposal_id": second, "feedback": [{"request_id": "decision_2_second"}]},
    ], [{"proposal_id": old, "feedback": [{"request_id": "decision_3_old"}]}]))
    fields = schema["properties"]["analysis"]["properties"]
    recommendation = fields["recommendations"]["items"]
    assert recommendation["properties"]["ready_for_action"] == {"type": "boolean"}
    assert "ready_for_action" in recommendation["required"]
    assert fields["recommendations"]["items"]["properties"]["proposal_id"]["enum"] == [first, second]
    branches = fields["replies"]["items"]["anyOf"]
    assert [(branch["properties"]["proposal_id"]["enum"], branch["properties"]["request_id"]["enum"])
            for branch in branches] == [([first], ["decision_1_first"]), ([second], ["decision_2_second"]),
                                      ([old], ["decision_3_old"])]
    assert all(branch["additionalProperties"] is False for branch in branches)


@pytest.mark.parametrize("has_proposal", [False, True])
def test_empty_generation_namespaces_require_empty_arrays_without_invalid_empty_enums(has_proposal: bool) -> None:
    proposals = [{"proposal_id": "proposal_" + "a" * 16, "feedback": []}] if has_proposal else []
    fields = _output_schema(schema_job(proposals))["properties"]["analysis"]["properties"]
    assert fields["replies"]["maxItems"] == 0
    assert "anyOf" not in fields["replies"]["items"]
    if not has_proposal:
        assert fields["recommendations"]["maxItems"] == 0
        assert "enum" not in fields["recommendations"]["items"]["properties"]["proposal_id"]


def test_retired_duplicate_feedback_namespaces_merge_without_losing_exact_question_bindings() -> None:
    proposal = "proposal_" + "a" * 16
    fields = _output_schema(schema_job([
        {"proposal_id": proposal, "feedback": [{"request_id": "decision_1_same"}]},
    ], [
        {"proposal_id": proposal, "feedback": [{"request_id": "decision_1_same"}, {"request_id": "decision_2_old"}]},
    ]))["properties"]["analysis"]["properties"]
    assert len(fields["replies"]["items"]["anyOf"]) == 1
    assert fields["replies"]["items"]["anyOf"][0]["properties"]["request_id"]["enum"] == [
        "decision_1_same", "decision_2_old"]


@pytest.mark.parametrize("limit", ["enum_total", "enum_characters"])
def test_oversized_generation_identity_schema_refuses_instead_of_dropping_bindings(limit: str) -> None:
    if limit == "enum_total":
        # Each enum is small; the combined recommendation/reply count exceeds 1k.
        job = schema_job([{"proposal_id": f"proposal_{number:016x}", "feedback": [
            {"request_id": f"decision_{number + 1}_first"}, {"request_id": f"decision_{number + 1}_second"},
        ]} for number in range(249)])
    else:
        # Below the global count limit, but this individual enum exceeds 15k.
        job = schema_job([{"proposal_id": "proposal_" + "a" * 16,
                           "feedback": [{"request_id": f"decision_1_{number:032x}"} for number in range(400)]}])
    with pytest.raises(ReviewQueueError, match="provider schema limits"):
        _output_schema(job)


def test_generation_identity_schema_accepts_supported_size_near_combined_enum_limit() -> None:
    job = schema_job([{"proposal_id": f"proposal_{number:016x}", "feedback": [
        {"request_id": f"decision_{number + 1}_first"}, {"request_id": f"decision_{number + 1}_second"},
    ]} for number in range(248)])
    assert len(_output_schema(job)["properties"]["analysis"]["properties"]["replies"]["items"]["anyOf"]) == 248


def test_absent_decisions_use_defaults_but_persistent_amendments_are_not_offered() -> None:
    assert _available_decisions({}) == _available_decisions({"availableDecisions": None}) == {
        "accept", "decline", "cancel"}
    assert _available_decisions({"availableDecisions": ["accept", "decline",
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["cat"]}}]}) == {"accept", "decline"}
    assert _available_decisions({"availableDecisions": ["acceptForSession", "accept", "cancel"]}) == {"accept", "cancel"}
    assert _available_decisions({"availableDecisions": ["acceptForSession"]}) == set()


@pytest.mark.parametrize("advertised", [[], {}, [False], ["unknown"], [{}], [{"unexpected": {}}]])
def test_invalid_explicit_decisions_are_not_default_grants(advertised: Any) -> None:
    with pytest.raises(ReviewQueueError, match="permission decisions"):
        _available_decisions({"availableDecisions": advertised})


def test_unsupported_permission_never_defaults_to_accept(
    runner_case: tuple[SupervisorJobs, FixtureProvider, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, _unused, job = runner_case
    with pytest.raises(ReviewQueueError, match="supported presentation"):
        SupervisorRunner(jobs, provider(tmp_path, monkeypatch, permission=True, presentation=False)).run(job["job_id"])
    assert jobs.read(job["job_id"])["status"] == "failed"
