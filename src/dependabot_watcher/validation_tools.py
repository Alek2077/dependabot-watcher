"""Provider-neutral agent-selected validation, bound to one backend-owned job.

No caller-chosen host paths, revisions, credentials or action authority. The
provider translates dynamic calls; this controller owns preparation, execution
receipts and cancellation. Test selection remains the agent's responsibility.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from .config import SupervisorValidationConfig
from .runtime_state import RuntimeStateError, ensure_private_directory, run_lock, save_private_json
from .supervisor_workspace import _git
from .review_queue import ReviewQueueError
from .validation_runtime import ValidationLimits, ValidationRuntimeError, allocated_bytes, run_validation_command
from .validation_snapshot import prepare_validation_snapshot
from .validation_evidence import ValidationEvidence


def validation_tool_specs() -> list[dict[str, Any]]:
    return [
        {'type': 'function', 'name': 'watcher_validation_prepare',
         'description': 'Prepare an isolated exact revision for validation: select a collected PR by proposal_id or the saved merged maintenance action by action_id. The backend selects the commit. No host paths or remote changes. Returns a validation_id; omitted links/secret paths are reported.',
         'inputSchema': {'type': 'object', 'properties': {'proposal_id': {'type': 'string'}, 'action_id': {'type': 'string'}},
                         'oneOf': [{'required': ['proposal_id']}, {'required': ['action_id']}],
                         'additionalProperties': False}},
        {'type': 'function', 'name': 'watcher_validation_run',
         'description': 'Run a shell command inside a prepared validation sandbox at /work, with persistent private cache at /cache. Install dependencies, create disposable probes and run relevant tests within saved network/storage/time limits. No host HOME, Git metadata or credentials. Results are actual execution evidence, not proof of compatibility.',
         'inputSchema': {'type': 'object', 'properties': {'validation_id': {'type': 'string'}, 'command': {'type': 'string'}},
                         'required': ['validation_id', 'command'], 'additionalProperties': False}},
    ]


class ValidationTools:
    """One asynchronous operation at a time; transport polling stays responsive."""

    def __init__(self, job: Mapping[str, Any], state_directory: Path,
                 ensure_current: Callable[[], None], *, storage_reserve_paths: tuple[Path, ...] = ()) -> None:
        policy = job['context']['supervisor'].get('validation')
        if (not isinstance(policy, dict) or policy.get('preparation_authorized') is not True
                or job['context']['supervisor'].get('validation_authorized') is not True):
            raise ValidationRuntimeError('Validation preparation is not authorized in this job.')
        options = SupervisorValidationConfig(**policy)
        self.limits = ValidationLimits(timeout_seconds=options.command_timeout_seconds,
            maximum_bytes=options.maximum_bytes, minimum_free_bytes=options.minimum_free_bytes,
            network_enabled=options.network_authorized)
        self.job = job
        self.workspace = Path(job['context']['workspace_evidence']['directory'])
        self.root = ensure_private_directory(state_directory / 'supervisor-validation' / job['job_id'])
        scope = hashlib.sha256(job['repository'].casefold().encode()).hexdigest()
        self.cache = ensure_private_directory(state_directory / 'validation-cache' / scope)
        self._cache_lease = ensure_private_directory(state_directory / 'validation-cache-leases' / scope)
        self._evidence = ValidationEvidence(state_directory / 'validation-evidence' / scope, policy)
        self._ensure_current = ensure_current
        self._reserve_paths = storage_reserve_paths + tuple(Path(path) for path in options.storage_reserve_paths)
        self._cancelled = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='watcher-validation')
        self._pending: tuple[str, Future[dict[str, Any]]] | None = None
        self._prepared: dict[str, tuple[Path, str, dict[str, str]]] = {}
        self._unadmitted: set[Path] = set()
        self._revision_caches: dict[str, Path] = {}
        self._source_trees: dict[str, str] = {}
        self._fingerprints: dict[str, dict[str, str]] = {}
        self._source_metadata: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        self._call_bindings: dict[str, dict[str, str]] = {}
        self._calls = 0
        self._seen_calls: set[str] = set()

    def ensure_authority(self) -> None:
        if self._cancelled.is_set():
            raise ValidationRuntimeError('Validation consumer stopped; owned work was cancelled.')
        self._ensure_current()

    def _storage_roots(self) -> tuple[Path, ...]:
        # Different commits can name the same source tree/cache.
        return tuple(dict.fromkeys((self.root, *self._revision_caches.values())))

    def ensure_current(self, *, ensure_operation: Callable[[], None] | None = None) -> None:
        self.ensure_authority()
        current = ensure_operation or self.ensure_authority
        current()
        for path in self._reserve_paths:
            current()
            if shutil.disk_usage(path).free < self.limits.minimum_free_bytes:
                raise ValidationRuntimeError('Validation host-volume reserve is insufficient.')
        if sum(allocated_bytes(path, ensure_current=current)
               for path in self._storage_roots()) > self.limits.maximum_bytes:
            raise ValidationRuntimeError('Validation job reached its aggregate storage budget.')
        current()

    def submit(self, call_id: str, tool: str, arguments: object) -> None:
        self._ensure_current()
        if (self._pending is not None or not isinstance(call_id, str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call_id) is None or self._calls >= 40
                or call_id in self._seen_calls):
            raise ValidationRuntimeError('Validation call is invalid, concurrent or exceeds the job bound.')
        if not isinstance(arguments, dict):
            raise ValidationRuntimeError('Validation arguments must be a scoped object.')
        valid = (set(arguments) in ({'proposal_id'}, {'action_id'}) if tool == 'watcher_validation_prepare'
                 else set(arguments) == {'validation_id', 'command'} if tool == 'watcher_validation_run' else False)
        if not valid:
            raise ValidationRuntimeError('Unknown validation tool or argument fields.')
        self._calls += 1
        self._seen_calls.add(call_id)
        identifier = arguments.get('validation_id')
        if isinstance(identifier, str) and identifier in self._prepared:
            _path, sha, selection = self._prepared[identifier]
            self._call_bindings[call_id] = {**selection, 'head_sha': sha, 'validation_id': identifier}
            save_private_json(self.root, Path('call_' + call_id + '.json'),
                {'job_id': self.job['job_id'], 'context_digest': self.job['context_digest'],
                 'status': 'admitted', **self._call_bindings[call_id]})
        self._pending = call_id, self._executor.submit(self._execute, tool, dict(arguments))

    def poll(self) -> tuple[str, dict[str, Any]] | None:
        if self._pending is None or not self._pending[1].done():
            return None
        call_id, future = self._pending
        self._pending = None
        self._ensure_current()
        try:
            result = future.result()
        except ValidationRuntimeError as exc:
            result = {'status': 'blocked', 'reason': str(exc), 'job_id': self.job['job_id'],
                      'context_digest': self.job['context_digest'], **self._call_bindings.get(call_id, {})}
            if call_id in self._call_bindings:
                save_private_json(self.root, Path('failure_' + call_id + '.json'),
                                  {key: value for key, value in result.items() if key != 'reason'})
        self._call_bindings.pop(call_id, None)
        return call_id, result

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def _execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.limits.timeout_seconds

        def cleanup_current() -> None:
            self.ensure_authority()
            if time.monotonic() >= deadline:
                raise ValidationRuntimeError('Validation operation exceeded its lifetime.')

        try:
            return self._execute_operation(tool, arguments)
        except ValidationRuntimeError:
            cleanup_current()
            # Resource refusal is a tool result, not loss of job authority.
            # After the runner killed/drained its process, discard only this
            # registered disposable copy if its allocation exceeded admission.
            identifier = arguments.get('validation_id')
            if isinstance(identifier, str) and identifier in self._prepared:
                path, sha, _proposal = self._prepared[identifier]
                try:
                    with run_lock(self._cache_lease):
                        if sum(allocated_bytes(root, ensure_current=cleanup_current)
                               for root in self._storage_roots()) > self.limits.maximum_bytes:
                            cache = self._revision_caches[sha]
                            if (path.is_symlink() or path.parent != self.root
                                    or cache.is_symlink() or cache.parent != self.cache
                                    or cache.name != 'tree_' + self._source_trees[sha]):
                                raise ValidationRuntimeError('Over-budget storage identity changed; files were preserved.')
                            shutil.rmtree(path)
                            del self._prepared[identifier]
                            self._fingerprints.pop(identifier, None)
                            self._source_metadata.pop(identifier, None)
                            # Failed, over-budget validation cache contents are
                            # disposable, unlike host development/tool caches.
                            # Reset the offending revision cache even if copy
                            # removal alone briefly brings usage under budget:
                            # a nearly-full cache can prevent a fresh copy.
                            if not any(self._revision_caches[other_sha] == cache
                                       for _other_path, other_sha, _other_selection in self._prepared.values()):
                                shutil.rmtree(cache)
                                self._revision_caches[sha] = ensure_private_directory(cache)
                except (OSError, RuntimeStateError) as exc:
                    self._ensure_current()
                    raise ValidationRuntimeError('Over-budget cleanup could not complete; remaining owned files were preserved.') from exc
            raise
        except (OSError, RuntimeStateError) as exc:
            # Recheck authority separately; revoked/unreadable job state must
            # not be disguised as an ordinary operational validation refusal.
            self._ensure_current()
            raise ValidationRuntimeError('Validation operation is unavailable within its owned storage boundary.') from exc

    def _execute_operation(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.ensure_current()
        # Reusable caches can be shared across jobs, but only one owner writes
        # a repository's cache at once. Host caches are never mounted implicitly.
        with run_lock(self._cache_lease):
            if tool == 'watcher_validation_prepare':
                return self._prepare(arguments)
            identifier = arguments['validation_id']
            if not isinstance(identifier, str) or identifier not in self._prepared:
                raise ValidationRuntimeError('Validation selection does not belong to this job.')
            path, sha, selection = self._prepared[identifier]
            deadline = time.monotonic() + self.limits.timeout_seconds

            def current() -> None:
                self.ensure_authority()
                if time.monotonic() >= deadline:
                    raise ValidationRuntimeError('Validation operation exceeded its lifetime.')

            result = run_validation_command(path, self._revision_caches[sha], arguments['command'], self.limits,
                ensure_current=current, storage_reserve_paths=self._reserve_paths,
                storage_roots=self._storage_roots())
            receipt = {**asdict(result), 'job_id': self.job['job_id'],
                'context_digest': self.job['context_digest'], 'validation_id': identifier,
                **selection, 'head_sha': sha, 'source_tree_sha': self._source_trees[sha],
                'network_enabled': self.limits.network_enabled}
            receipt['tracked_source_changed'] = self._source_changed(
                path, self._fingerprints[identifier], self._source_metadata[identifier], ensure_current=current)
            transient_output = receipt.pop('output')
            receipt['operation_id'] = 'validation_' + uuid.uuid4().hex
            save_private_json(self.root, Path(receipt['operation_id'] + '.json'), receipt)
            try:
                self._evidence.record(receipt)
            except (OSError, RuntimeStateError):
                # The exact execution receipt above remains authoritative.
                # Losing an optional reuse index must not erase real results.
                pass
            self.ensure_current(ensure_operation=current)
            current()
            return {**receipt, 'output': transient_output}

    def _prepare(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.limits.timeout_seconds

        def current() -> None:
            self.ensure_authority()
            if time.monotonic() >= deadline:
                raise ValidationRuntimeError('Validation preparation exceeded its lifetime.')

        if 'action_id' in arguments:
            action_id = arguments['action_id']
            purpose = self.job['context'].get('maintenance_completion')
            obligation = purpose.get('obligation') if isinstance(purpose, dict) else None
            if (not isinstance(action_id, str) or re.fullmatch(r'action_[a-f0-9]{24}', action_id) is None
                    or not isinstance(obligation, dict) or obligation.get('action_id') != action_id
                    or not isinstance(obligation.get('repository'), str)
                    or obligation['repository'].casefold() != self.job['repository'].casefold()):
                raise ValidationRuntimeError('Validation action is not the exact merged action assigned to this job.')
            sha = obligation.get('merge_commit_sha')
            selection = {'action_id': action_id}
        else:
            proposal_id = arguments['proposal_id']
            proposals = [p for p in self.job['context']['proposals'] if p['proposal_id'] == proposal_id
                         and p['step']['kind'] == 'pull_request']
            if len(proposals) != 1:
                raise ValidationRuntimeError('Validation requires one exact collected PR proposal.')
            sha = proposals[0]['step']['identity'].get('head_sha')
            selection = {'proposal_id': str(proposal_id)}
        if not isinstance(sha, str) or re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha) is None:
            raise ValidationRuntimeError('Validation selection has no exact commit.')
        self.ensure_current(ensure_operation=current)
        try:
            _git(self.workspace, 'cat-file', '-e', sha + '^{commit}', ensure_current=current,
                 timeout_seconds=self.limits.timeout_seconds)
        except ReviewQueueError:
            # Caller is the configured launch under its existing workspace
            # lease/origin receipt, not the model's unrestricted Git command.
            self.ensure_current(ensure_operation=current)
            try:
                _git(self.workspace, 'fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
                     'origin', sha, overrides=('remote.origin.uploadpack=git-upload-pack',),
                     timeout_seconds=self.limits.timeout_seconds, ensure_current=current)
            except ReviewQueueError as exc:
                self.ensure_current(ensure_operation=current)
                raise ValidationRuntimeError('Exact validation revision could not be acquired; no checks ran.') from exc
        self.ensure_current(ensure_operation=current)
        # A squash merge can change commit identity without changing any source.
        # Share only when Git confirms the entire raw tree, including modes and
        # lockfiles, is identical. Different source trees never share writable
        # caches. This is cache reuse, not permission to reuse test conclusions.
        try:
            tree = _git(self.workspace, 'rev-parse', '--verify', sha + '^{tree}',
                        ensure_current=current, timeout_seconds=self.limits.timeout_seconds).strip()
        except ReviewQueueError as exc:
            raise ValidationRuntimeError('Validation source tree identity is unavailable.') from exc
        if re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', tree) is None:
            raise ValidationRuntimeError('Validation source tree identity is unavailable.')
        self._source_trees[sha] = tree
        self._revision_caches[sha] = ensure_private_directory(self.cache / ('tree_' + tree))
        source_headroom = min(32 * 1024**2, self.limits.maximum_bytes // 2)
        occupied = sum(allocated_bytes(root, ensure_current=current)
                       for root in self._storage_roots())
        if occupied > self.limits.maximum_bytes - source_headroom:
            # A retained cache is optional. Leave bounded room for source
            # preparation instead of admitting an inevitably unusable copy.
            cache = self._revision_caches[sha]
            if any(self._revision_caches[prepared_sha] == cache
                   for _path, prepared_sha, _selection in self._prepared.values()):
                raise ValidationRuntimeError('Preparation needs storage headroom; an active validation cache was preserved.')
            shutil.rmtree(cache)
            self._revision_caches[sha] = ensure_private_directory(cache)
            occupied = sum(allocated_bytes(root, ensure_current=current)
                           for root in self._storage_roots())
            if occupied > self.limits.maximum_bytes - source_headroom:
                raise ValidationRuntimeError('Preparation needs storage headroom; existing validation storage was preserved.')
        self.ensure_current(ensure_operation=current)
        identifier = 'validation_' + uuid.uuid4().hex
        path = self.root / identifier
        # Track before creation: even a failed admission must remain reachable
        # by shutdown if its immediate disposal cannot complete.
        self._unadmitted.add(path)
        try:
            receipt = prepare_validation_snapshot(self.workspace, path, sha=sha,
                limits=self.limits, ensure_current=current)
            # Bind the private snapshot receipt before stripping fingerprints
            # from the model-visible result. Selection comes only from this job.
            save_private_json(self.root, Path(identifier + '.json'),
                {**receipt, **selection, 'source_tree_sha': tree, 'validation_id': identifier,
                 'job_id': self.job['job_id'], 'context_digest': self.job['context_digest']})
            self.ensure_current(ensure_operation=current)
            fingerprints = cast(dict[str, str], receipt.pop('source_fingerprints'))
            source_metadata: dict[str, tuple[int, int, int, int]] = {}
            for name in fingerprints:
                current()
                metadata = (path / name).stat(follow_symlinks=False)
                current()
                source_metadata[name] = (
                    metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_ctime_ns)
            current()
            self.ensure_current(ensure_operation=current)
            current()
        except BaseException:
            # A refused preparation has no model-visible handle. Dispose only
            # this newly allocated copy; keep its independent failure record.
            failure = {'job_id': self.job['job_id'], 'context_digest': self.job['context_digest'],
                **selection, 'head_sha': sha, 'validation_id': identifier,
                'status': 'preparation_failed', 'disposal': 'pending'}
            audit = Path('preparation_' + identifier + '.json')
            self._record_preparation_failure(audit, failure)
            self._dispose_snapshot(path)
            self._unadmitted.discard(path)
            self._record_preparation_failure(audit, {**failure, 'disposal': 'completed'})
            raise
        self._prepared[identifier] = path, sha, selection
        self._fingerprints[identifier] = fingerprints
        self._source_metadata[identifier] = source_metadata
        self._unadmitted.remove(path)
        return {**receipt, 'validation_id': identifier, **selection, 'source_tree_sha': tree,
                'cache_scope': 'repository_and_identical_source_tree',
                'historical_validation': self._evidence.historical(tree),
                'job_id': self.job['job_id'], 'context_digest': self.job['context_digest'],
                'network_enabled': self.limits.network_enabled,
                'maximum_bytes': self.limits.maximum_bytes, 'minimum_free_bytes': self.limits.minimum_free_bytes,
                'source_integrity_check': 'content_identity_mode_and_kernel_change_time',
                'isolation_scope': 'watcher_validation_tools_only_not_all_agent_tools',
                'secret_filter': 'recognized_paths_only_not_content_scan',
                'working_directory': '/work', 'cache_directory': '/cache'}

    def _record_preparation_failure(self, audit: Path, failure: dict[str, Any]) -> None:
        # Audit availability must never determine whether disposable files can
        # be removed, nor mask the original preparation failure after removal.
        try:
            save_private_json(self.root, audit, failure)
        except (OSError, RuntimeStateError):
            pass

    def _dispose_snapshot(self, path: Path) -> None:
        if path.is_symlink() or path.parent != self.root:
            raise ValidationRuntimeError('Validation disposal identity changed; copy was preserved.')
        if path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _source_changed(root: Path, fingerprints: Mapping[str, str],
                        original_metadata: Mapping[str, tuple[int, int, int, int]], *,
                        ensure_current: Callable[[], None] = lambda: None) -> bool:
        """Detect content, replacement, mode and write/restore changes without following links.

        Kernel ctime cannot be restored through ordinary sandbox file APIs.
        This is a source-integrity signal, not proof that an agent chose valid tests.
        """
        scanned = 0
        for name, expected in fingerprints.items():
            ensure_current()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = Path(name).parts
                for component in parts[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                try:
                    metadata = os.fstat(file)
                    if (not stat.S_ISREG(metadata.st_mode)
                            or (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_ctime_ns)
                            != original_metadata.get(name)):
                        return True
                    digest = hashlib.sha256()
                    remaining = 32 * 1024**2
                    while block := os.read(file, min(65_536, remaining + 1)):
                        ensure_current()
                        scanned += len(block)
                        if scanned > 32 * 1024**2:
                            raise ValidationRuntimeError('Validation source-integrity scan exceeds its aggregate bound.')
                        remaining -= len(block)
                        if remaining < 0:
                            return True
                        digest.update(block)
                    if digest.hexdigest() != expected:
                        return True
                finally:
                    os.close(file)
            except OSError:
                return True
            finally:
                os.close(descriptor)
        return False

    def close(self) -> None:
        self._cancelled.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        # Retain cache and receipts. Clean only tracked, backend-created private
        # snapshots, never registered worktrees or user-selected directories.
        paths = {path for path, _sha, _proposal in self._prepared.values()} | self._unadmitted
        for path in paths:
            self._dispose_snapshot(path)
            self._unadmitted.discard(path)
