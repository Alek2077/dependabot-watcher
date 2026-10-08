from __future__ import annotations

import time
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.config import SupervisorValidationConfig
from dependabot_watcher.review_queue import ReviewQueueError
from dependabot_watcher.runtime_state import RuntimeLockBusyError
from dependabot_watcher.validation_runtime import ValidationLimits, ValidationRuntimeError
from dependabot_watcher.validation_tools import ValidationTools
from test_validation_snapshot import repository
from test_supervisor_workspace import git

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or shutil.which('bwrap') is None
    or shutil.which('node') is None, reason='Linux/WSL Bubblewrap and Node sandbox integration')


def case(tmp_path: Path) -> tuple[ValidationTools, dict[str, Any], Path]:
    workspace, sha = repository(tmp_path)
    job = {'job_id': 'job_' + 'a' * 32, 'repository': 'synthetic/project', 'context_digest': 'b' * 64,
           'context': {'supervisor': {'validation_authorized': True,
               'validation': asdict(SupervisorValidationConfig(preparation_authorized=True))},
               'workspace_evidence': {'directory': str(workspace)},
               'proposals': [{'proposal_id': 'proposal_' + 'c' * 16,
                    'step': {'kind': 'pull_request', 'identity': {'head_sha': sha}}}]}}
    return ValidationTools(job, tmp_path / 'state', lambda: None), job, workspace


def wait(tools: ValidationTools) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = tools.poll()
        if result is not None:
            return result[1]
        time.sleep(.02)
    raise AssertionError('Validation did not return within its test deadline')


def test_agent_selects_exact_proposal_runs_real_sandbox_and_receipts_survive_cleanup(tmp_path: Path) -> None:
    tools, job, workspace = case(tmp_path)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        prepared = wait(tools)
        assert prepared['head_sha'] == job['context']['proposals'][0]['step']['identity']['head_sha']
        assert prepared['git_metadata_present'] is False
        tools.submit('run1', 'watcher_validation_run', {'validation_id': prepared['validation_id'],
            'command': 'node -e "console.log(2+2)"; printf disposable > probe.txt'})
        result = wait(tools)
        assert result['exit_code'] == 0 and result['output'] == '4\n'
        assert result['proposal_id'] == prepared['proposal_id'] and result['head_sha'] == prepared['head_sha']
        assert result['tracked_source_changed'] is False
        assert (tools.root / (result['operation_id'] + '.json')).exists()
        assert not (workspace / 'probe.txt').exists()
        snapshot = tools.root / prepared['validation_id']
        assert (snapshot / 'probe.txt').read_text() == 'disposable'
    finally:
        tools.close()
    assert not snapshot.exists()
    assert tools.cache.exists()
    assert (tools.root / (result['operation_id'] + '.json')).exists()


@pytest.mark.parametrize('command', [
    'cp app.js /tmp/original; printf changed > app.js; cp /tmp/original app.js',
    'chmod +x app.js',
])
def test_source_write_restore_and_mode_changes_are_attributed(tmp_path: Path, command: str) -> None:
    tools, job, _workspace = case(tmp_path)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        prepared = wait(tools)
        tools.submit('run1', 'watcher_validation_run',
                     {'validation_id': prepared['validation_id'], 'command': command})
        result = wait(tools)
        assert result['exit_code'] == 0
        assert result['tracked_source_changed'] is True
    finally:
        tools.close()


def test_failed_revision_acquisition_returns_scoped_blocked_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, job, _workspace = case(tmp_path)
    def unavailable(*args, **kwargs):
        raise ReviewQueueError('Synthetic unavailable object/fetch')
    monkeypatch.setattr('dependabot_watcher.validation_tools._git', unavailable)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        result = wait(tools)
        assert result['status'] == 'blocked'
        assert result['job_id'] == job['job_id'] and result['context_digest'] == job['context_digest']
        assert 'Synthetic unavailable' not in result['reason']
    finally:
        tools.close()


def test_busy_cache_lease_returns_scoped_blocked_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, job, _workspace = case(tmp_path)
    def busy(*args, **kwargs):
        raise RuntimeLockBusyError('synthetic private lock detail')
    monkeypatch.setattr('dependabot_watcher.validation_tools.run_lock', busy)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        result = wait(tools)
        assert result['status'] == 'blocked' and result['job_id'] == job['job_id']
        assert 'synthetic private' not in result['reason']
    finally:
        tools.close()


@pytest.mark.parametrize('target', ['oversized', '/cache/oversized', 'near_limit_cache'])
def test_storage_limit_returns_blocked_and_allows_smaller_followup(tmp_path: Path, target: str) -> None:
    tools, job, workspace = case(tmp_path)
    tools.limits = ValidationLimits(maximum_bytes=1024 * 1024)
    proposal = job['context']['proposals'][0]['proposal_id']
    try:
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': proposal})
        first = wait(tools)
        command = ("dd if=/dev/zero of=/cache/near bs=1024 count=1010 2>/dev/null; "
                   "dd if=/dev/zero of=extra bs=1024 count=20 2>/dev/null") if target == 'near_limit_cache' else (
                   f'dd if=/dev/zero of={target} bs=1048576 count=2 2>/dev/null')
        tools.submit('run1', 'watcher_validation_run', {'validation_id': first['validation_id'],
            'command': command})
        blocked = wait(tools)
        assert blocked['status'] == 'blocked' and 'budget' in blocked['reason']
        assert not (tools.root / first['validation_id']).exists()
        assert not (workspace / 'oversized').exists()
        tools.submit('prepare2', 'watcher_validation_prepare', {'proposal_id': proposal})
        second = wait(tools)
        tools.submit('run2', 'watcher_validation_run', {'validation_id': second['validation_id'],
                     'command': 'printf smaller'})
        assert wait(tools)['output'] == 'smaller'
    finally:
        tools.close()


def test_validation_caches_do_not_cross_revision_boundaries(tmp_path: Path) -> None:
    tools, job, workspace = case(tmp_path)
    original = job['context']['proposals'][0]['proposal_id']
    (workspace / 'app.js').write_text('console.log("new revision")\n')
    git(workspace, 'add', 'app.js')
    git(workspace, 'commit', '-m', 'Synthetic second revision')
    other = 'proposal_' + 'd' * 16
    job['context']['proposals'].append({'proposal_id': other,
        'step': {'kind': 'pull_request', 'identity': {'head_sha': git(workspace, 'rev-parse', 'HEAD')}}})
    try:
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': original})
        first = wait(tools)
        tools.submit('run1', 'watcher_validation_run', {'validation_id': first['validation_id'],
                     'command': 'printf private > /cache/from_other_revision'})
        assert wait(tools)['exit_code'] == 0
        tools.submit('prepare2', 'watcher_validation_prepare', {'proposal_id': other})
        second = wait(tools)
        tools.submit('run2', 'watcher_validation_run', {'validation_id': second['validation_id'],
                     'command': 'test ! -e /cache/from_other_revision'})
        assert wait(tools)['exit_code'] == 0
    finally:
        tools.close()


@pytest.mark.parametrize('combined', [False, True])
def test_nearly_full_retained_cache_leaves_room_for_preparation(tmp_path: Path, combined: bool) -> None:
    tools, job, _workspace = case(tmp_path)
    tools.limits = ValidationLimits(maximum_bytes=1024 * 1024)
    sha = job['context']['proposals'][0]['step']['identity']['head_sha']
    cache = tools.cache / sha
    cache.mkdir(mode=0o700)
    (cache / 'near_limit').write_bytes(b'0' * ((200 if combined else 1000) * 1024))
    if combined:
        (tools.root / 'prior_owned_evidence').write_bytes(b'0' * (650 * 1024))
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        result = wait(tools)
        assert 'validation_id' in result
        assert not (cache / 'near_limit').exists()
    finally:
        tools.close()


@pytest.mark.parametrize('tool,args', [
    ('unexpected', {}), ('watcher_validation_prepare', {'proposal_id': 'x', 'path': '/host'}),
    ('watcher_validation_run', {'validation_id': 'x'}),
])
def test_tool_contract_does_not_accept_host_paths_or_unknown_operations(tmp_path: Path, tool: str,
                                                                     args: dict[str, object]) -> None:
    tools, _job, _workspace = case(tmp_path)
    try:
        with pytest.raises(ValidationRuntimeError):
            tools.submit('call1', tool, args)
    finally:
        tools.close()


def test_unknown_proposal_and_validation_identity_return_blocked_not_host_execution(tmp_path: Path) -> None:
    tools, _job, _workspace = case(tmp_path)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': 'unknown'})
        assert wait(tools)['status'] == 'blocked'
        tools.submit('run1', 'watcher_validation_run', {'validation_id': 'unknown', 'command': 'exit 0'})
        assert wait(tools)['status'] == 'blocked'
    finally:
        tools.close()
