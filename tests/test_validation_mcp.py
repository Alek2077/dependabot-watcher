from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dependabot_watcher.providers.codex_app_server_supervisor import CodexAppServerSupervisor
from dependabot_watcher.review_queue import ReviewQueueError
from dependabot_watcher.validation_mcp import MAX_FRAME, SERVER_NAME, ValidationMcpBridge, _wire_receipt
from dependabot_watcher.validation_tools import ValidationTools, validation_tool_specs
from dependabot_watcher.validation_runtime import ValidationRuntimeError
from test_validation_tools import case as validation_case
from test_review_actions import action_case, reviews_case
from test_maintenance_completion_runtime import record_completed_merge
from test_validation_snapshot import repository
from test_supervisor_workspace import git
from dependabot_watcher.config import SupervisorConfig, SupervisorValidationConfig, SupervisorWorkspaceConfig
from dependabot_watcher.review_queue import ReviewQueue, supervisor_context_digest
from dependabot_watcher.supervision import SupervisorJobs, _validate_context

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Local Linux/WSL relay contract')


def test_saved_merge_job_admits_action_validation_and_rejects_context_drift(
    reviews_case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, sha = repository(tmp_path)
    maintenance = tmp_path / 'maintenance'
    git(workspace, 'worktree', 'add', '--detach', str(maintenance), sha)
    merge = reviews_case['client'].merge_pull_request
    monkeypatch.setattr(reviews_case['client'], 'merge_pull_request',
        lambda *args, **kwargs: {**merge(*args, **kwargs), 'merge_commit_sha': sha})
    case = record_completed_merge(reviews_case, monkeypatch)
    policy = SupervisorConfig(mode='approval_first', provider='codex_app_server', model='fixture-model',
        reasoning_effort='high', expected_account_type='chatgpt', model_use_acknowledged=True,
        workspaces=(SupervisorWorkspaceConfig(repository='example-org/example-app', directory=str(maintenance),
            instruction_sources=(str(maintenance / 'AGENTS.md'),)),), validation_authorized=True,
        validation=SupervisorValidationConfig(preparation_authorized=True))
    queue = ReviewQueue(replace(case['queue'].config, supervisor=policy), case['queue'].directory)
    proof = {'repository': 'example-org/example-app', 'directory': str(maintenance),
        'head_sha': sha, 'git_directory': git(maintenance, 'rev-parse', '--absolute-git-dir'),
        'common_directory': str(workspace / '.git'),
        'instruction_fingerprints': [[str(maintenance / 'AGENTS.md'), 'c' * 64]]}
    queue.record_workspace_evidence(proof)
    jobs = SupervisorJobs(queue)
    job = jobs.prepare('example-org/example-app')
    assert job['context']['validation_capability_revision'] == 'merged_action_v1'
    legacy = {key: value for key, value in job['context'].items()
              if key != 'validation_capability_revision'}
    _validate_context(legacy)  # Existing saved conversations/journals remain readable.
    assert supervisor_context_digest(legacy) != job['context_digest']
    with pytest.raises(ReviewQueueError, match='capability revision'):
        _validate_context({**job['context'], 'validation_capability_revision': 'unknown'})
    action = job['context']['maintenance_completion']['obligation']['action_id']
    assert job['context']['maintenance_completion']['review_id'] == case['review_id']
    # A real queue change must invalidate preparation, not merely mutate a
    # fixture's context behind an unchanged digest.
    changed = dict(proof)
    changed['instruction_fingerprints'] = [[str(maintenance / 'AGENTS.md'), 'd' * 64]]
    queue.record_workspace_evidence(changed)
    with pytest.raises(ReviewQueueError, match='context changed'):
        jobs.start(job['job_id'])
    queue.record_workspace_evidence(proof)
    running = jobs.start(job['job_id'])
    tools = ValidationTools(running, queue.directory, lambda: queue.require_current_configuration())
    bridge = ValidationMcpBridge(running['job_id'], running['context_digest'])
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge, arguments={'action_id': action})) as client:
            deadline = time.monotonic() + 10
            while bridge.delivered_calls == 0 and time.monotonic() < deadline:
                pump(bridge, tools)
                time.sleep(.01)
            result = json.loads(client.recv(16_384))
        assert result['head_sha'] == sha and result['action_id'] == action
        assert 'proposal_id' not in result
        assert jobs.read(job['job_id'])['context_digest'] == running['context_digest']
    finally:
        tools.close()
        bridge.close()
        jobs.fail(job['job_id'], interrupted=True)


class Controller:
    """Transport double only; real sandbox integration lives in supervisor_launch."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.limits = SimpleNamespace(timeout_seconds=1)
        self.calls: list[tuple[str, str, object]] = []
        self.revoked = False
        self.result_ready = True
        self.result = {'status': 'completed', 'operation_id': 'validation_receipt',
                       'head_sha': 'a' * 40, 'proposal_id': 'proposal_exact'}

    def ensure_current(self) -> None:
        if self.revoked:
            raise ReviewQueueError('Synthetic revoked authority')

    def ensure_authority(self) -> None:
        self.ensure_current()

    def submit(self, call: str, tool: str, arguments: object) -> None:
        self.ensure_current()
        self.calls.append((call, tool, arguments))

    def poll(self) -> tuple[str, dict[str, Any]] | None:
        if not self.result_ready:
            return None
        return self.calls[-1][0], self.result


def request(bridge: ValidationMcpBridge, **changes: Any) -> bytes:
    args = bridge.configuration(1)['args']
    offset = args.index('dependabot_watcher.validation_mcp') + 1
    _path, nonce, job, digest, _timeout = args[offset:]
    value = {'call_id': 'mcp_call1', 'tool': 'watcher_validation_prepare',
             'arguments': {'proposal_id': 'proposal_exact'}, 'nonce': nonce,
             'job_id': job, 'context_digest': digest}
    return (json.dumps({**value, **changes}) + '\n').encode()


def pump(bridge: ValidationMcpBridge, controller: Controller | ValidationTools, *, active: bool = True) -> None:
    bridge.pump(cast(ValidationTools, controller), active=active, dynamic_pending=False)


def connect(bridge: ValidationMcpBridge, data: bytes) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(bridge.path))
    client.sendall(data)
    return client


def test_authenticated_exact_turn_delivery_receipt_and_endpoint_cleanup(tmp_path: Path) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    endpoint = bridge.path
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge)) as client:
            pump(bridge, controller)
            pending = json.loads((tmp_path / 'transport_mcp_call1.json').read_text())
            assert pending['status'] == 'pending' and pending['turn_id'] == 'turn_exact'
            pump(bridge, controller)
            pump(bridge, controller)
            result = json.loads(client.recv(4096))
            assert result['head_sha'] == 'a' * 40
        receipt = json.loads((tmp_path / 'transport_mcp_call1.json').read_text())
        assert receipt['job_id'] == 'job_exact' and receipt['context_digest'] == 'digest_exact'
        assert receipt['session_id'] == 'session_exact' and receipt['status'] == 'completed'
        assert receipt['operation_id'] == 'validation_receipt'
        assert 'nonce' not in receipt and 'arguments' not in receipt
        assert bridge.delivered_calls == 1 and not bridge.pending
        assert controller.calls == [('mcp_call1', 'watcher_validation_prepare', {'proposal_id': 'proposal_exact'})]
        with pytest.raises(ReviewQueueError, match='already'):
            bridge.bind_turn('different', 'different')
    finally:
        bridge.close()
    assert not endpoint.exists() and not endpoint.parent.exists()


def test_controller_completion_is_not_delivery_after_disconnect(tmp_path: Path) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        client = connect(bridge, request(bridge))
        pump(bridge, controller)
        pump(bridge, controller)
        assert json.loads((tmp_path / 'transport_mcp_call1.json').read_text())['status'] == 'controller_completed'
        client.close()
        with pytest.raises(ReviewQueueError, match='disconnected'):
            pump(bridge, controller)
        assert bridge.delivered_calls == 0
        assert json.loads((tmp_path / 'transport_mcp_call1.json').read_text())['status'] == 'controller_completed'
    finally:
        bridge.close()


def test_authenticated_invalid_admission_returns_blocked_receipt(tmp_path: Path) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    def invalid(*args: Any) -> None:
        raise ValidationRuntimeError('Unknown validation tool or argument fields.')
    controller.submit = invalid  # type: ignore[method-assign]
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge, arguments={'unknown': True})) as client:
            pump(bridge, controller)
            pump(bridge, controller)
            result = json.loads(client.recv(4096))
        assert result['status'] == 'blocked' and result['job_id'] == 'job_exact'
        assert bridge.delivered_calls == 1
    finally:
        bridge.close()


def test_real_controller_reserve_refusal_returns_blocked_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, job, _workspace = validation_case(tmp_path)
    bridge = ValidationMcpBridge(job['job_id'], job['context_digest'])
    tools._reserve_paths = (tmp_path,)
    monkeypatch.setattr('dependabot_watcher.validation_tools.shutil.disk_usage', lambda path: SimpleNamespace(free=0))
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge, arguments={'proposal_id': job['context']['proposals'][0]['proposal_id']})) as client:
            deadline = time.monotonic() + 3
            while bridge.delivered_calls == 0 and time.monotonic() < deadline:
                pump(bridge, tools)
                time.sleep(.01)
            result = json.loads(client.recv(4096))
        assert result['status'] == 'blocked' and 'reserve' in result['reason']
        assert bridge.delivered_calls == 1
    finally:
        tools.close()
        bridge.close()


def test_real_controller_merged_action_preparation_keeps_provenance_through_mcp(tmp_path: Path) -> None:
    tools, job, _workspace = validation_case(tmp_path)
    action = 'action_' + 'd' * 24
    sha = job['context']['proposals'][0]['step']['identity']['head_sha']
    job['context']['maintenance_completion'] = {'obligation': {
        'action_id': action, 'repository': job['repository'], 'merge_commit_sha': sha}}
    bridge = ValidationMcpBridge(job['job_id'], job['context_digest'])
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge, arguments={'action_id': action})) as client:
            deadline = time.monotonic() + 10
            while bridge.delivered_calls == 0 and time.monotonic() < deadline:
                pump(bridge, tools)
                time.sleep(.01)
            result = json.loads(client.recv(16_384))
        assert result['action_id'] == action and result['head_sha'] == sha
        assert 'proposal_id' not in result
        assert bridge.delivered_calls == 1
        journal = json.loads((tools.root / 'transport_mcp_call1.json').read_text())
        assert journal['action_id'] == action and journal['head_sha'] == sha
    finally:
        tools.close()
        bridge.close()


def test_real_invalid_admission_attempts_are_bounded_before_private_receipt_growth(tmp_path: Path) -> None:
    tools, job, _workspace = validation_case(tmp_path)
    bridge = ValidationMcpBridge(job['job_id'], job['context_digest'])
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        for index in range(40):
            with connect(bridge, request(bridge, call_id=f'mcp_invalid_{index}', arguments={'unknown': True})) as client:
                pump(bridge, tools)
                pump(bridge, tools)
                assert json.loads(client.recv(4096))['status'] == 'blocked'
        assert len(list(tools.root.glob('transport_*.json'))) == 40
        assert bridge.delivered_calls == 40
        with connect(bridge, request(bridge, call_id='mcp_over_limit', arguments={'unknown': True})):
            with pytest.raises(ReviewQueueError, match='identity'):
                pump(bridge, tools)
        assert len(list(tools.root.glob('transport_*.json'))) == 40
    finally:
        tools.close()
        bridge.close()


@pytest.mark.parametrize('change', [
    {'nonce': 'wrong'}, {'job_id': 'different'}, {'context_digest': 'different'},
    {'nonce': None}, {'extra': 'not admitted'},
])
def test_unauthenticated_wrong_job_and_extended_requests_never_reach_controller(tmp_path: Path, change: dict[str, Any]) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge, **change)):
            with pytest.raises(ReviewQueueError, match='authority'):
                pump(bridge, controller)
        assert controller.calls == [] and list(tmp_path.iterdir()) == []
    finally:
        bridge.close()


@pytest.mark.parametrize('bound,active', [(False, True), (True, False)])
def test_calls_before_or_outside_active_turn_are_refused(tmp_path: Path, bound: bool, active: bool) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    try:
        if bound:
            bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge)):
            with pytest.raises(ReviewQueueError, match='authority'):
                pump(bridge, controller, active=active)
        assert controller.calls == []
    finally:
        bridge.close()


@pytest.mark.parametrize('data', [b'{}\n{}\n', b'{invalid}\n',
    b'{"job_id":"one","job_id":"two"}\n', b'{"arguments":{"x":1,"x":2}}\n',
    b'x' * (MAX_FRAME + 1)])
def test_bad_or_over_limit_framing_is_fatal_without_execution(tmp_path: Path, data: bytes) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, data):
            with pytest.raises(ReviewQueueError):
                pump(bridge, controller)
        assert controller.calls == []
    finally:
        bridge.close()


def test_revocation_during_pending_call_blocks_result_delivery(tmp_path: Path) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, request(bridge)):
            pump(bridge, controller)
            controller.revoked = True
            with pytest.raises(ReviewQueueError, match='revoked'):
                pump(bridge, controller)
        assert bridge.delivered_calls == 0
        assert json.loads((tmp_path / 'transport_mcp_call1.json').read_text())['status'] == 'pending'
    finally:
        bridge.close()


def test_disconnected_pending_client_is_fatal(tmp_path: Path) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        client = connect(bridge, request(bridge))
        pump(bridge, controller)
        client.close()
        with pytest.raises(ReviewQueueError, match='disconnected'):
            pump(bridge, controller)
        assert bridge.delivered_calls == 0
    finally:
        bridge.close()


@pytest.mark.parametrize('selection_kind', ['proposal', 'action'])
def test_disconnect_cancels_real_sandbox_and_disposes_only_owned_snapshot(
    tmp_path: Path, selection_kind: str,
) -> None:
    if shutil.which('bwrap') is None:
        pytest.skip('Actual Bubblewrap cancellation integration')
    tools, job, workspace = validation_case(tmp_path)
    sha = job['context']['proposals'][0]['step']['identity']['head_sha']
    selection = {'proposal_id': job['context']['proposals'][0]['proposal_id']}
    if selection_kind == 'action':
        action = 'action_' + 'd' * 24
        job['context']['maintenance_completion'] = {'obligation': {
            'action_id': action, 'repository': job['repository'], 'merge_commit_sha': sha}}
        selection = {'action_id': action}
    bridge = ValidationMcpBridge(job['job_id'], job['context_digest'])
    client: socket.socket | None = None
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        client = connect(bridge, request(bridge, call_id='prepare_exact',
            arguments=selection))
        deadline = time.monotonic() + 5
        while bridge.delivered_calls == 0 and time.monotonic() < deadline:
            pump(bridge, tools)
            time.sleep(.01)
        assert bridge.delivered_calls == 1
        prepared = json.loads(client.recv(8192))
        client.close()
        snapshot = tools.root / str(prepared['validation_id'])
        client = connect(bridge, request(bridge, call_id='run_exact', tool='watcher_validation_run',
            arguments={'validation_id': prepared['validation_id'],
                       'command': 'printf started > validation-running; sleep 30'}))
        deadline = time.monotonic() + 5
        while not (snapshot / 'validation-running').exists() and time.monotonic() < deadline:
            pump(bridge, tools)
            time.sleep(.01)
        assert (snapshot / 'validation-running').exists(), 'Actual isolated command never started.'
        client.close()
        client = None
        with pytest.raises(ReviewQueueError, match='disconnected'):
            pump(bridge, tools)
        started = time.monotonic()
        tools.close()
        assert time.monotonic() - started < 5, 'Cancellation waited for the long command instead of draining it.'
        assert not snapshot.exists() and not (workspace / 'validation-running').exists()
        assert bridge.delivered_calls == 1
        receipt = json.loads((tools.root / 'transport_run_exact.json').read_text())
        assert receipt['status'] == 'pending'  # Never invent a successful delivery after disconnect.
        admission = json.loads((tools.root / 'call_run_exact.json').read_text())
        preparation = json.loads((tools.root / (prepared['validation_id'] + '.json')).read_text())
        for binding in (admission, preparation):
            assert binding['head_sha'] == sha and binding['validation_id'] == prepared['validation_id']
            assert all(binding[key] == value for key, value in selection.items())
            assert ('proposal_id' in binding) != ('action_id' in binding)
    finally:
        if client is not None:
            client.close()
        tools.close()
        bridge.close()


def test_partial_request_deadline_is_fatal(tmp_path: Path) -> None:
    controller = Controller(tmp_path)
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        with connect(bridge, b'{'):
            pump(bridge, controller)
            bridge._deadline = time.monotonic() - 1
            with pytest.raises(ReviewQueueError, match='deadline'):
                pump(bridge, controller)
        assert controller.calls == []
    finally:
        bridge.close()


def inventory() -> dict[str, Any]:
    return {'data': [{'name': SERVER_NAME, 'runtimeStatus': 'connected', 'toolsError': None,
                     'serverInfo': {'name': SERVER_NAME, 'version': '1'},
                     'tools': {s['name']: {k: v for k, v in s.items() if k != 'type'}
                               for s in validation_tool_specs()}}], 'nextCursor': None}


def test_large_omissions_and_output_are_flagged_bounded_evidence_not_transport_failure() -> None:
    omissions = [{'path': f'links/{index}/' + 'x' * 200, 'reason': 'symlink'} for index in range(50_000)]
    original = {'job_id': 'job_exact', 'context_digest': 'digest_exact', 'head_sha': 'a' * 40,
                'omitted_entries': len(omissions), 'omissions': omissions, 'snapshot_complete': False,
                'output': 'log' * 100_000, 'output_sha256': 'b' * 64}
    wire = _wire_receipt(original, 'mcp_exact')
    assert len(json.dumps(wire).encode()) < 16_384
    assert wire['omissions_total'] == 50_000 and wire['omitted_entries'] == 50_000
    assert wire['omissions_preview_truncated'] is True and wire['output_preview_truncated'] is True
    assert len(wire['omissions']) <= 20 and len(wire['omissions_sha256']) == 64
    assert wire['snapshot_complete'] is False and wire['output_sha256'] == 'b' * 64
    assert len(original['omissions']) == 50_000 and len(original['output']) == 300_000


def test_other_oversized_metadata_returns_explicit_bounded_refusal() -> None:
    wire = _wire_receipt({'job_id': 'job_exact', 'context_digest': 'digest_exact',
        'unexpected_metadata': 'x' * 2_000_001}, 'mcp_exact')
    assert wire['status'] == 'blocked' and wire['job_id'] == 'job_exact'
    assert wire['transport_call_id'] == 'mcp_exact' and len(wire['normalized_response_sha256']) == 64
    assert len(json.dumps(wire).encode()) < 1024


@pytest.mark.parametrize('fault', ['failed', 'missing', 'extra', 'schema', 'metadata', 'version', 'error', 'cursor'])
def test_unverified_inventory_cannot_authorize_a_turn(fault: str) -> None:
    value = inventory()
    server = value['data'][0]
    if fault == 'failed':
        server['runtimeStatus'] = 'failed'
    elif fault == 'missing':
        server['tools'].pop('watcher_validation_run')
    elif fault == 'extra':
        server['tools']['unexpected'] = {}
    elif fault == 'schema':
        server['tools']['watcher_validation_run']['inputSchema'] = {}
    elif fault == 'metadata':
        server['tools']['watcher_validation_run']['annotations'] = {'unexpected': True}
    elif fault == 'version':
        server['serverInfo']['version'] = 'unknown'
    elif fault == 'error':
        server['toolsError'] = 'unavailable'
    else:
        value['nextCursor'] = 'unverified_page'
    with pytest.raises(ReviewQueueError, match='exact backend-owned'):
        CodexAppServerSupervisor._verify_validation_inventory(value)


@pytest.mark.parametrize('fault', ['tool', 'missing_result', 'receipt', 'duplicate', 'failed',
                                  'is_error', 'numeric_error', 'extra_result', 'extra_content',
                                  'structured', 'boolean_number', 'integer_float'])
def test_completion_requires_exact_delivered_tool_and_receipt(fault: str) -> None:
    expected = {'status': 'completed', 'transport_call_id': 'mcp_exact', 'operation_id': 'validation_exact',
                'tracked_source_changed': False, 'exit_code': 1}
    bridge = cast(ValidationMcpBridge, SimpleNamespace(delivered_calls=1,
        delivered_receipts=[('watcher_validation_prepare', expected)]))
    item: dict[str, Any] = {'tool': 'watcher_validation_prepare', 'status': 'completed',
        'result': {'content': [{'type': 'text', 'text': json.dumps(expected)}]}}
    index = 0
    if fault == 'tool':
        item['tool'] = 'watcher_validation_run'
    elif fault == 'missing_result':
        item['result'] = None
    elif fault == 'receipt':
        item['result']['content'][0]['text'] = json.dumps({**expected, 'operation_id': 'different'})
    elif fault == 'duplicate':
        index = 1
    elif fault == 'is_error':
        item['result']['isError'] = True
    elif fault == 'numeric_error':
        item['result']['isError'] = 0
    elif fault == 'extra_result':
        item['result']['unknown'] = 'unverified'
    elif fault == 'extra_content':
        item['result']['content'][0]['annotations'] = {'unverified': True}
    elif fault == 'structured':
        item['result']['structuredContent'] = {'unverified': True}
    elif fault in {'boolean_number', 'integer_float'}:
        altered = {**expected, **({'tracked_source_changed': 0} if fault == 'boolean_number' else {'exit_code': 1.0})}
        item['result']['content'][0]['text'] = json.dumps(altered)
    else:
        item['status'] = 'failed'
    with pytest.raises(ReviewQueueError):
        CodexAppServerSupervisor._verify_validation_completion(item, bridge, index)


@pytest.mark.parametrize('envelope', [{}, {'isError': False}, {'structuredContent': None, '_meta': None}])
def test_controller_blocked_receipt_remains_evidence_not_transport_failure(envelope: dict[str, Any]) -> None:
    expected = {'status': 'blocked', 'reason': 'storage budget', 'transport_call_id': 'mcp_exact',
                'job_id': 'job_exact', 'context_digest': 'digest_exact'}
    bridge = cast(ValidationMcpBridge, SimpleNamespace(delivered_calls=1,
        delivered_receipts=[('watcher_validation_run', expected)]))
    item = {'tool': 'watcher_validation_run', 'status': 'completed',
            'result': {'content': [{'type': 'text', 'text': json.dumps(expected)}], **envelope}}
    CodexAppServerSupervisor._verify_validation_completion(item, bridge, 0)


@pytest.mark.parametrize('blocked', [False, True])
def test_stdio_child_actual_tool_call_uses_relay_with_empty_environment(tmp_path: Path, blocked: bool) -> None:
    bridge = ValidationMcpBridge('job_exact', 'digest_exact')
    controller = Controller(tmp_path)
    if blocked:
        controller.result = {'status': 'blocked', 'reason': 'synthetic storage refusal'}
    child: subprocess.Popen[bytes] | None = None
    try:
        bridge.bind_turn('session_exact', 'turn_exact')
        config = bridge.configuration(1)
        child = subprocess.Popen([config['command'], *config['args']], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={}, cwd=tmp_path)
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'watcher_validation_prepare', 'arguments': {'proposal_id': 'proposal_exact'}}}) + '\n').encode())
        child.stdin.flush()
        deadline = time.monotonic() + 5
        while bridge.delivered_calls == 0 and time.monotonic() < deadline:
            pump(bridge, controller)
            if child.poll() is not None:
                raise AssertionError('Relay child exited before delivering the exact controller receipt.')
            time.sleep(.01)
        assert bridge.delivered_calls == 1
        child.stdin.close()
        child.wait(timeout=2)
        response = json.loads(child.stdout.read())
        assert response['id'] == 1 and response['result']['isError'] is False
        received = json.loads(response['result']['content'][0]['text'])
        if blocked:
            assert received['status'] == 'blocked'
        else:
            assert received['head_sha'] == 'a' * 40
        assert child.returncode == 0
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        bridge.close()
