from __future__ import annotations

import time
import shutil
import sys
import os
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.config import SupervisorValidationConfig
from dependabot_watcher.review_queue import ReviewQueueError
from dependabot_watcher.runtime_state import RuntimeLockBusyError
from dependabot_watcher.runtime_state import save_private_json
from dependabot_watcher.validation_runtime import ValidationLimits, ValidationRuntimeError
from dependabot_watcher.validation_tools import ValidationTools
from dependabot_watcher.validation_snapshot import prepare_validation_snapshot
from dependabot_watcher.supervisor_workspace import _git as workspace_git
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


@pytest.mark.parametrize('cancel', [False, True])
def test_revision_acquisition_respects_preparation_deadline_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    tools, job, _workspace = case(tmp_path)
    tools.limits = ValidationLimits(timeout_seconds=1, maximum_bytes=268435456, minimum_free_bytes=1)
    marker = tmp_path / 'fetch.pid'
    executable = tmp_path / 'slow-git'
    executable.write_text('#!' + sys.executable + '\nimport os,time,pathlib\n'
        + 'pathlib.Path(' + repr(str(marker)) + ').write_text(str(os.getpid()))\ntime.sleep(30)\n')
    executable.chmod(0o700)
    original_which = shutil.which
    monkeypatch.setattr('dependabot_watcher.supervisor_workspace.shutil.which',
        lambda name: str(executable) if name == 'git' else original_which(name))
    def acquisition(workspace: Path, *arguments: str, **kwargs: Any) -> str:
        if arguments[0] == 'cat-file':
            raise ReviewQueueError('Synthetic missing exact object')
        return workspace_git(workspace, *arguments, **kwargs)
    monkeypatch.setattr('dependabot_watcher.validation_tools._git', acquisition)
    started = time.monotonic()
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        while not marker.exists() and time.monotonic() - started < 3:
            time.sleep(.01)
        assert marker.exists()
        if cancel:
            tools.close()
        else:
            result = wait(tools)
            assert result['status'] == 'blocked'
        assert time.monotonic() - started < 3
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    finally:
        tools.close()


def test_integrity_hashing_checks_cancellation_inside_file_reads(tmp_path: Path) -> None:
    source = tmp_path / 'large.js'
    source.write_bytes(b'x' * 100000)
    metadata = source.stat()
    checks = 0
    def current() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValidationRuntimeError('Synthetic cancellation during hashing')
    with pytest.raises(ValidationRuntimeError, match='cancellation'):
        ValidationTools._source_changed(tmp_path, {'large.js': hashlib.sha256(source.read_bytes()).hexdigest()},
            {'large.js': (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_ctime_ns)},
            ensure_current=current)
    assert checks == 2 and source.stat().st_size == 100000


def test_integrity_callbacks_do_not_rescan_dependency_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, job, _workspace = case(tmp_path)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        prepared = wait(tools)
        original = ValidationTools._source_changed
        def repeated(root: Path, fingerprints: Any, metadata: Any, *, ensure_current: Any) -> bool:
            # Model a large tracked source set. Authority/deadline checks must
            # still run, but each must not walk the installed dependencies.
            for _ in range(1000):
                ensure_current()
            return original(root, fingerprints, metadata, ensure_current=ensure_current)
        checks = 0
        real_resources = tools.ensure_current
        def resources(**kwargs: Any) -> None:
            nonlocal checks
            checks += 1
            real_resources(**kwargs)
        monkeypatch.setattr(ValidationTools, '_source_changed', staticmethod(repeated))
        monkeypatch.setattr(tools, 'ensure_current', resources)
        tools.submit('run1', 'watcher_validation_run', {'validation_id': prepared['validation_id'], 'command': 'true'})
        result = wait(tools)
        assert result['exit_code'] == 0 and result['tracked_source_changed'] is False
        assert checks <= 4
    finally:
        tools.close()


@pytest.mark.parametrize('stage', ['occupancy', 'snapshot_receipt', 'failure_cleanup', 'resources'])
@pytest.mark.parametrize('cancel', [False, True])
def test_all_allocation_boundaries_observe_operation_cancellation_and_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, cancel: bool,
) -> None:
    tools, job, _workspace = case(tmp_path)
    proposal = job['context']['proposals'][0]['proposal_id']
    prepared: dict[str, Any] | None = None
    if stage == 'failure_cleanup':
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': proposal})
        prepared = wait(tools)
    tools.limits = ValidationLimits(timeout_seconds=1, maximum_bytes=268435456, minimum_free_bytes=1)
    observed = False
    def interrupted_allocation(root: Path, *, ensure_current: Any = None, **kwargs: Any) -> int:
        nonlocal observed
        assert ensure_current is not None
        observed = True
        if cancel:
            tools._cancelled.set()
        else:
            time.sleep(1.05)
        ensure_current()
        raise AssertionError('Allocation callback accepted expired or cancelled work')
    def cheap_resources(**kwargs: Any) -> None:
        tools.ensure_authority()
        if kwargs.get('ensure_operation') is not None:
            kwargs['ensure_operation']()
    try:
        if stage == 'resources':
            deadline = time.monotonic() + 1
            def current() -> None:
                tools.ensure_authority()
                if time.monotonic() >= deadline:
                    raise ValidationRuntimeError('Synthetic operation lifetime exceeded')
            monkeypatch.setattr('dependabot_watcher.validation_tools.allocated_bytes', interrupted_allocation)
            with pytest.raises(ValidationRuntimeError, match='cancelled|lifetime'):
                tools.ensure_current(ensure_operation=current)
        else:
            if stage in {'occupancy', 'failure_cleanup'}:
                # Isolate the direct occupancy/failure scan from admission and
                # final resource scans, which have separate coverage above.
                monkeypatch.setattr(tools, 'ensure_current', cheap_resources)
                monkeypatch.setattr('dependabot_watcher.validation_tools.allocated_bytes', interrupted_allocation)
            else:
                monkeypatch.setattr('dependabot_watcher.validation_snapshot.allocated_bytes', interrupted_allocation)
            if stage == 'failure_cleanup':
                assert prepared is not None
                def refused(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    raise ValidationRuntimeError('Synthetic execution refusal')
                monkeypatch.setattr(tools, '_execute_operation', refused)
                tools.submit('run1', 'watcher_validation_run',
                             {'validation_id': prepared['validation_id'], 'command': 'true'})
            else:
                tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': proposal})
            result = wait(tools)
            assert result['status'] == 'blocked'
            assert 'cancelled' in result['reason'] if cancel else 'lifetime' in result['reason']
        assert observed
    finally:
        tools.close()


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_preparation_metadata_tail_cannot_return_success_after_deadline_or_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, audit_fails: bool,
) -> None:
    tools, job, _workspace = case(tmp_path)
    tools.limits = ValidationLimits(timeout_seconds=1, maximum_bytes=268435456, minimum_free_bytes=1)
    tail = False
    original_stat = Path.stat
    def snapshot(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal tail
        result = prepare_validation_snapshot(*args, **kwargs)
        tail = True
        return result
    def delayed_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if tail and path.name == 'app.js' and tools.root in path.parents:
            if cancel:
                tools._cancelled.set()
            else:
                time.sleep(1.05)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr('dependabot_watcher.validation_tools.prepare_validation_snapshot', snapshot)
    monkeypatch.setattr(Path, 'stat', delayed_stat)
    def audit(directory: Path, name: Path, value: Any) -> Any:
        if audit_fails and name.name.startswith('preparation_'):
            raise OSError('Synthetic audit storage failure')
        return save_private_json(directory, name, value)
    monkeypatch.setattr('dependabot_watcher.validation_tools.save_private_json', audit)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        result = wait(tools)
        assert tail and result['status'] == 'blocked'
        assert 'cancelled' in result['reason'] if cancel else 'lifetime' in result['reason']
        assert tools._prepared == {} and tools._fingerprints == {} and tools._source_metadata == {}
        assert tools._unadmitted == set()
        assert not list(tools.root.glob('validation_*/'))
        failures = list(tools.root.glob('preparation_*.json'))
        if audit_fails:
            assert failures == []
        else:
            assert len(failures) == 1
            assert json.loads(failures[0].read_text())['disposal'] == 'completed'
    finally:
        tools.close()


def test_failed_preparation_disposal_remains_tracked_for_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, job, _workspace = case(tmp_path)
    original_dispose = tools._dispose_snapshot
    def refused_snapshot(*args: Any, **kwargs: Any) -> dict[str, object]:
        prepare_validation_snapshot(*args, **kwargs)
        raise ValidationRuntimeError('Synthetic preparation refusal after creation')
    def failed_dispose(path: Path) -> None:
        raise OSError('Synthetic disposal failure')
    monkeypatch.setattr('dependabot_watcher.validation_tools.prepare_validation_snapshot', refused_snapshot)
    monkeypatch.setattr(tools, '_dispose_snapshot', failed_dispose)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        assert wait(tools)['status'] == 'blocked'
        assert not tools._prepared
        assert len(tools._unadmitted) == 1
        retained = next(iter(tools._unadmitted))
        assert retained.is_dir()
        failure = next(tools.root.glob('preparation_*.json'))
        assert json.loads(failure.read_text())['disposal'] == 'pending'
    finally:
        monkeypatch.setattr(tools, '_dispose_snapshot', original_dispose)
        tools.close()
    assert not retained.exists() and tools._unadmitted == set()


def test_real_command_integrity_scan_shares_operation_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, job, _workspace = case(tmp_path)
    tools.limits = ValidationLimits(timeout_seconds=1, maximum_bytes=268435456, minimum_free_bytes=1)
    original_scan = ValidationTools._source_changed
    def delayed_scan(root: Path, fingerprints: Any, metadata: Any, *, ensure_current: Any) -> bool:
        time.sleep(1.05)
        return original_scan(root, fingerprints, metadata, ensure_current=ensure_current)
    monkeypatch.setattr(ValidationTools, '_source_changed', staticmethod(delayed_scan))
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        prepared = wait(tools)
        tools.submit('run1', 'watcher_validation_run', {'validation_id': prepared['validation_id'], 'command': 'true'})
        result = wait(tools)
        assert result['status'] == 'blocked' and 'lifetime' in result['reason']
        assert len(list(tools.root.glob('validation_*.json'))) == 1
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


def test_validation_caches_do_not_cross_source_tree_boundaries(tmp_path: Path) -> None:
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


@pytest.mark.parametrize('separate_jobs', [False, True])
def test_equivalent_commit_trees_reuse_cache_but_not_working_copies(
    tmp_path: Path, separate_jobs: bool,
) -> None:
    tools, job, workspace = case(tmp_path)
    original = job['context']['proposals'][0]['proposal_id']
    git(workspace, 'commit', '--allow-empty', '-m', 'Same source, distinct commit')
    sha = git(workspace, 'rev-parse', 'HEAD')
    other = 'proposal_' + 'd' * 16
    job['context']['proposals'].append({'proposal_id': other,
        'step': {'kind': 'pull_request', 'identity': {'head_sha': sha}}})
    try:
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': original})
        first = wait(tools)
        tools.submit('run1', 'watcher_validation_run', {'validation_id': first['validation_id'],
            'command': 'printf cached > /cache/same_tree; printf disposable > local_probe'})
        assert wait(tools)['exit_code'] == 0
        if separate_jobs:
            tools.close()
            job = {**job, 'job_id': 'job_' + 'e' * 32}
            tools = ValidationTools(job, tmp_path / 'state', lambda: None)
        tools.submit('prepare2', 'watcher_validation_prepare', {'proposal_id': other})
        second = wait(tools)
        assert first['head_sha'] != second['head_sha']
        assert first['source_tree_sha'] == second['source_tree_sha']
        assert first['validation_id'] != second['validation_id']
        assert second['cache_scope'] == 'repository_and_identical_source_tree'
        assert second['historical_validation']['fresh_execution'] is False
        assert second['historical_validation']['environment_equivalence'] == 'not_established'
        assert len(second['historical_validation']['results']) == 1
        assert second['historical_validation']['results'][0]['head_sha'] == first['head_sha']
        assert len(tools._storage_roots()) == 2
        tools.submit('run2', 'watcher_validation_run', {'validation_id': second['validation_id'],
            'command': 'test "$(cat /cache/same_tree)" = cached && test ! -e local_probe'})
        result = wait(tools)
        assert result['exit_code'] == 0
        assert result['source_tree_sha'] == first['source_tree_sha']
        assert result['head_sha'] == sha  # Cache reuse does not rewrite execution identity.
    finally:
        tools.close()


def test_invalid_git_tree_identity_refuses_cache_adoption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, job, _workspace = case(tmp_path)
    actual = workspace_git
    def identity(workspace: Path, *arguments: str, **kwargs: Any) -> str:
        return '../untrusted' if arguments[0] == 'rev-parse' else actual(workspace, *arguments, **kwargs)
    monkeypatch.setattr('dependabot_watcher.validation_tools._git', identity)
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        assert wait(tools)['status'] == 'blocked'
        assert not list(tools.cache.iterdir())
    finally:
        tools.close()


@pytest.mark.parametrize('equivalent', [False, True])
def test_equivalent_preparation_preserves_active_cache_when_headroom_is_low(tmp_path: Path, equivalent: bool) -> None:
    tools, job, workspace = case(tmp_path)
    tools.limits = ValidationLimits(maximum_bytes=1024 * 1024)
    original = job['context']['proposals'][0]['proposal_id']
    if not equivalent:
        (workspace / 'different.txt').write_text('different source')
        git(workspace, 'add', 'different.txt')
    git(workspace, 'commit', '--allow-empty', '-m', 'Additional source revision')
    other = 'proposal_' + 'd' * 16
    job['context']['proposals'].append({'proposal_id': other,
        'step': {'kind': 'pull_request', 'identity': {'head_sha': git(workspace, 'rev-parse', 'HEAD')}}})
    try:
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': original})
        prepared = wait(tools)
        tools.submit('run1', 'watcher_validation_run', {'validation_id': prepared['validation_id'],
            'command': 'dd if=/dev/zero of=/cache/keep bs=1024 count=600 2>/dev/null'})
        assert wait(tools)['exit_code'] == 0
        tools.submit('prepare2', 'watcher_validation_prepare', {'proposal_id': other})
        refused = wait(tools)
        assert refused['status'] == 'blocked' and 'preserved' in refused['reason']
        assert len(tools._prepared) == 1
        assert tools._revision_caches[prepared['head_sha']].name == 'tree_' + prepared['source_tree_sha']
        tools.submit('run2', 'watcher_validation_run', {'validation_id': prepared['validation_id'],
            'command': 'test -f /cache/keep'})
        assert wait(tools)['exit_code'] == 0
    finally:
        tools.close()


def test_over_budget_copy_cleanup_preserves_shared_active_cache(tmp_path: Path) -> None:
    tools, job, _workspace = case(tmp_path)
    tools.limits = ValidationLimits(maximum_bytes=1024 * 1024)
    proposal = job['context']['proposals'][0]['proposal_id']
    try:
        tools.submit('prepare1', 'watcher_validation_prepare', {'proposal_id': proposal})
        first = wait(tools)
        tools.submit('prepare2', 'watcher_validation_prepare', {'proposal_id': proposal})
        second = wait(tools)
        tools.submit('run1', 'watcher_validation_run', {'validation_id': first['validation_id'],
            'command': 'printf keep > /cache/keep; dd if=/dev/zero of=oversized bs=1048576 count=2 2>/dev/null'})
        assert wait(tools)['status'] == 'blocked'
        assert first['validation_id'] not in tools._prepared
        assert second['validation_id'] in tools._prepared
        tools.submit('run2', 'watcher_validation_run', {'validation_id': second['validation_id'],
            'command': 'test "$(cat /cache/keep)" = keep'})
        assert wait(tools)['exit_code'] == 0
    finally:
        tools.close()


@pytest.mark.parametrize('combined', [False, True])
def test_nearly_full_retained_cache_leaves_room_for_preparation(tmp_path: Path, combined: bool) -> None:
    tools, job, workspace = case(tmp_path)
    tools.limits = ValidationLimits(maximum_bytes=1024 * 1024)
    sha = job['context']['proposals'][0]['step']['identity']['head_sha']
    cache = tools.cache / ('tree_' + git(workspace, 'rev-parse', sha + '^{tree}'))
    cache.mkdir(mode=0o700)
    (cache / 'near_limit').write_bytes(b'0' * ((200 if combined else 1000) * 1024))
    if combined:
        (tools.root / 'prior_owned_evidence').write_bytes(b'0' * (650 * 1024))
    try:
        tools.submit('prepare1', 'watcher_validation_prepare',
                     {'proposal_id': job['context']['proposals'][0]['proposal_id']})
        result = wait(tools)
        if combined:
            assert result['status'] == 'blocked' and 'headroom' in result['reason']
            assert (tools.root / 'prior_owned_evidence').exists()
            assert not tools._prepared and not tools._unadmitted
        else:
            assert 'validation_id' in result
        assert not (cache / 'near_limit').exists()
        assert tools._revision_caches[sha] == cache and cache.is_dir()
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


def test_merged_action_selects_its_recorded_commit_and_preserves_action_receipts(tmp_path: Path) -> None:
    tools, job, workspace = case(tmp_path)
    action = 'action_' + 'd' * 24
    old_sha = job['context']['proposals'][0]['step']['identity']['head_sha']
    (workspace / 'app.js').write_text('console.log("merged")\n')
    git(workspace, 'add', 'app.js')
    git(workspace, 'commit', '-m', 'Synthetic merged revision')
    merged_sha = git(workspace, 'rev-parse', 'HEAD')
    job['context']['maintenance_completion'] = {'review_id': 'review_' + 'e' * 32,
        'obligation': {'action_id': action, 'repository': job['repository'], 'merge_commit_sha': merged_sha}}
    try:
        tools.submit('prepare_action', 'watcher_validation_prepare', {'action_id': action})
        prepared = wait(tools)
        assert prepared['head_sha'] == merged_sha != old_sha
        assert prepared['action_id'] == action and 'proposal_id' not in prepared
        snapshot_receipt = json.loads((tools.root / (prepared['validation_id'] + '.json')).read_text())
        assert snapshot_receipt['action_id'] == action and snapshot_receipt['head_sha'] == merged_sha
        assert 'proposal_id' not in snapshot_receipt
        tools.submit('run_action', 'watcher_validation_run',
            {'validation_id': prepared['validation_id'], 'command': 'node app.js'})
        result = wait(tools)
        assert result['exit_code'] == 0 and result['output'] == 'merged\n'
        assert result['action_id'] == action and result['head_sha'] == merged_sha
        assert 'proposal_id' not in result and result['tracked_source_changed'] is False
        saved = json.loads((tools.root / (result['operation_id'] + '.json')).read_text())
        assert saved['action_id'] == action and saved['head_sha'] == merged_sha
        assert git(workspace, 'rev-parse', 'HEAD') == merged_sha
    finally:
        tools.close()


def test_merged_action_refusal_retains_admitted_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, job, _workspace = case(tmp_path)
    action = 'action_' + 'd' * 24
    sha = job['context']['proposals'][0]['step']['identity']['head_sha']
    job['context']['maintenance_completion'] = {'obligation': {
        'action_id': action, 'repository': job['repository'], 'merge_commit_sha': sha}}
    try:
        tools.submit('prepare_action', 'watcher_validation_prepare', {'action_id': action})
        prepared = wait(tools)
        def refused(*args: Any, **kwargs: Any) -> Any:
            raise ValidationRuntimeError('Synthetic execution refusal')
        monkeypatch.setattr('dependabot_watcher.validation_tools.run_validation_command', refused)
        tools.submit('run_action', 'watcher_validation_run',
                     {'validation_id': prepared['validation_id'], 'command': 'node app.js'})
        result = wait(tools)
        assert result['status'] == 'blocked'
        for receipt in (result, json.loads((tools.root / 'failure_run_action.json').read_text()),
                        json.loads((tools.root / 'call_run_action.json').read_text())):
            assert receipt['action_id'] == action and receipt['head_sha'] == sha
            assert receipt['validation_id'] == prepared['validation_id'] and 'proposal_id' not in receipt
    finally:
        tools.close()


@pytest.mark.parametrize('fault', ['no_purpose', 'wrong_action', 'wrong_repository', 'missing_sha', 'invalid_sha'])
def test_merged_validation_refuses_unbound_or_ambiguous_actions(tmp_path: Path, fault: str) -> None:
    tools, job, _workspace = case(tmp_path)
    action = 'action_' + 'd' * 24
    obligation = {'action_id': action, 'repository': job['repository'],
                  'merge_commit_sha': job['context']['proposals'][0]['step']['identity']['head_sha']}
    if fault != 'no_purpose':
        job['context']['maintenance_completion'] = {'obligation': obligation}
    if fault == 'wrong_action':
        obligation['action_id'] = 'action_' + 'e' * 24
    elif fault == 'wrong_repository':
        obligation['repository'] = 'other/project'
    elif fault == 'missing_sha':
        obligation.pop('merge_commit_sha')
    elif fault == 'invalid_sha':
        obligation['merge_commit_sha'] = '--host-path'
    try:
        tools.submit('prepare_action', 'watcher_validation_prepare', {'action_id': action})
        assert wait(tools)['status'] == 'blocked'
        assert not tools._prepared
    finally:
        tools.close()


@pytest.mark.parametrize('arguments', [{}, {'action_id': 'x', 'proposal_id': 'y'},
    {'action_id': 'x', 'head_sha': 'a' * 40}, {'action_id': 'x', 'directory': '/host'}])
def test_prepare_requires_one_backend_owned_selection_only(tmp_path: Path, arguments: dict[str, Any]) -> None:
    tools, _job, _workspace = case(tmp_path)
    try:
        with pytest.raises(ValidationRuntimeError, match='argument fields'):
            tools.submit('prepare_action', 'watcher_validation_prepare', arguments)
    finally:
        tools.close()
