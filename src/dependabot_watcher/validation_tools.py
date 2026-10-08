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


def validation_tool_specs() -> list[dict[str, Any]]:
    return [
        {'type': 'function', 'name': 'watcher_validation_prepare',
         'description': 'Prepare an isolated exact-PR revision for local validation. No host paths or remote changes. Returns a validation_id; omitted links/secret paths are reported.',
         'inputSchema': {'type': 'object', 'properties': {'proposal_id': {'type': 'string'}},
                         'required': ['proposal_id'], 'additionalProperties': False}},
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
        self._ensure_current = ensure_current
        self._reserve_paths = storage_reserve_paths + tuple(Path(path) for path in options.storage_reserve_paths)
        self._cancelled = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='watcher-validation')
        self._pending: tuple[str, Future[dict[str, Any]]] | None = None
        self._prepared: dict[str, tuple[Path, str, str]] = {}
        self._revision_caches: dict[str, Path] = {}
        self._fingerprints: dict[str, dict[str, str]] = {}
        self._source_metadata: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        self._calls = 0
        self._seen_calls: set[str] = set()

    def ensure_current(self) -> None:
        if self._cancelled.is_set():
            raise ValidationRuntimeError('Validation consumer stopped; owned work was cancelled.')
        self._ensure_current()
        for path in self._reserve_paths:
            if shutil.disk_usage(path).free < self.limits.minimum_free_bytes:
                raise ValidationRuntimeError('Validation host-volume reserve is insufficient.')
        if sum(allocated_bytes(path) for path in (self.root, *self._revision_caches.values())) > self.limits.maximum_bytes:
            raise ValidationRuntimeError('Validation job reached its aggregate storage budget.')

    def submit(self, call_id: str, tool: str, arguments: object) -> None:
        self._ensure_current()
        if (self._pending is not None or not isinstance(call_id, str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call_id) is None or self._calls >= 40
                or call_id in self._seen_calls):
            raise ValidationRuntimeError('Validation call is invalid, concurrent or exceeds the job bound.')
        if not isinstance(arguments, dict):
            raise ValidationRuntimeError('Validation arguments must be a scoped object.')
        expected = {'proposal_id'} if tool == 'watcher_validation_prepare' else (
            {'validation_id', 'command'} if tool == 'watcher_validation_run' else None)
        if expected is None or set(arguments) != expected:
            raise ValidationRuntimeError('Unknown validation tool or argument fields.')
        self._calls += 1
        self._seen_calls.add(call_id)
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
                      'context_digest': self.job['context_digest']}
        return call_id, result

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def _execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._execute_operation(tool, arguments)
        except ValidationRuntimeError:
            self._ensure_current()
            # Resource refusal is a tool result, not loss of job authority.
            # After the runner killed/drained its process, discard only this
            # registered disposable copy if its allocation exceeded admission.
            identifier = arguments.get('validation_id')
            if isinstance(identifier, str) and identifier in self._prepared:
                path, sha, _proposal = self._prepared[identifier]
                try:
                    with run_lock(self._cache_lease):
                        if sum(allocated_bytes(root) for root in (self.root, *self._revision_caches.values())) > self.limits.maximum_bytes:
                            cache = self._revision_caches[sha]
                            if (path.is_symlink() or path.parent != self.root
                                    or cache.is_symlink() or cache.parent != self.cache or cache.name != sha):
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
                return self._prepare(arguments['proposal_id'])
            identifier = arguments['validation_id']
            if not isinstance(identifier, str) or identifier not in self._prepared:
                raise ValidationRuntimeError('Validation selection does not belong to this job.')
            path, sha, proposal = self._prepared[identifier]
            result = run_validation_command(path, self._revision_caches[sha], arguments['command'], self.limits,
                ensure_current=self.ensure_current, storage_reserve_paths=self._reserve_paths)
            receipt = {**asdict(result), 'job_id': self.job['job_id'],
                'context_digest': self.job['context_digest'], 'validation_id': identifier,
                'proposal_id': proposal, 'head_sha': sha, 'network_enabled': self.limits.network_enabled}
            receipt['tracked_source_changed'] = self._source_changed(
                path, self._fingerprints[identifier], self._source_metadata[identifier])
            transient_output = receipt.pop('output')
            receipt['operation_id'] = 'validation_' + uuid.uuid4().hex
            save_private_json(self.root, Path(receipt['operation_id'] + '.json'), receipt)
            self.ensure_current()
            return {**receipt, 'output': transient_output}

    def _prepare(self, proposal_id: object) -> dict[str, Any]:
        proposals = [p for p in self.job['context']['proposals'] if p['proposal_id'] == proposal_id
                     and p['step']['kind'] == 'pull_request']
        if len(proposals) != 1:
            raise ValidationRuntimeError('Validation requires one exact collected PR proposal.')
        identity = proposals[0]['step']['identity']
        sha = identity.get('head_sha')
        if not isinstance(sha, str) or re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha) is None:
            raise ValidationRuntimeError('Validation proposal has no exact commit.')
        self.ensure_current()
        try:
            _git(self.workspace, 'cat-file', '-e', sha + '^{commit}')
        except ReviewQueueError:
            # Caller is the configured launch under its existing workspace
            # lease/origin receipt, not the model's unrestricted Git command.
            self.ensure_current()
            try:
                _git(self.workspace, 'fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
                     'origin', sha, overrides=('remote.origin.uploadpack=git-upload-pack',), timeout_seconds=120)
            except ReviewQueueError as exc:
                self.ensure_current()
                raise ValidationRuntimeError('Exact validation revision could not be acquired; no checks ran.') from exc
        self.ensure_current()
        # Untrusted validation writes never flow from one PR revision's cache
        # into another. Repeated jobs for the same exact revision can reuse it.
        self._revision_caches[sha] = ensure_private_directory(self.cache / sha)
        source_headroom = min(32 * 1024**2, self.limits.maximum_bytes // 2)
        occupied = sum(allocated_bytes(root) for root in (self.root, *self._revision_caches.values()))
        if occupied > self.limits.maximum_bytes - source_headroom:
            # A retained cache is optional. Leave bounded room for source
            # preparation instead of admitting an inevitably unusable copy.
            shutil.rmtree(self._revision_caches[sha])
            self._revision_caches[sha] = ensure_private_directory(self.cache / sha)
        self.ensure_current()
        identifier = 'validation_' + uuid.uuid4().hex
        path = self.root / identifier
        try:
            receipt = prepare_validation_snapshot(self.workspace, path, sha=sha,
                limits=self.limits, ensure_current=self.ensure_current)
        except (OSError, RuntimeStateError) as exc:
            self.ensure_current()
            raise ValidationRuntimeError('Validation preparation failed; owned partial files were preserved.') from exc
        self._prepared[identifier] = path, sha, str(proposal_id)
        self._fingerprints[identifier] = cast(dict[str, str], receipt.pop('source_fingerprints'))
        self._source_metadata[identifier] = {}
        for name in self._fingerprints[identifier]:
            metadata = (path / name).stat(follow_symlinks=False)
            self._source_metadata[identifier][name] = (
                metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_ctime_ns)
        return {**receipt, 'validation_id': identifier, 'proposal_id': proposal_id,
                'job_id': self.job['job_id'], 'context_digest': self.job['context_digest'],
                'network_enabled': self.limits.network_enabled,
                'maximum_bytes': self.limits.maximum_bytes, 'minimum_free_bytes': self.limits.minimum_free_bytes,
                'source_integrity_check': 'content_identity_mode_and_kernel_change_time',
                'isolation_scope': 'watcher_validation_tools_only_not_all_agent_tools',
                'secret_filter': 'recognized_paths_only_not_content_scan',
                'working_directory': '/work', 'cache_directory': '/cache'}

    @staticmethod
    def _source_changed(root: Path, fingerprints: Mapping[str, str],
                        original_metadata: Mapping[str, tuple[int, int, int, int]]) -> bool:
        """Detect content, replacement, mode and write/restore changes without following links.

        Kernel ctime cannot be restored through ordinary sandbox file APIs.
        This is a source-integrity signal, not proof that an agent chose valid tests.
        """
        for name, expected in fingerprints.items():
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
        for path, _sha, _proposal in self._prepared.values():
            if path.is_symlink() or path.parent != self.root:
                raise ValidationRuntimeError('Validation disposal identity changed; copy was preserved.')
            if path.exists():
                shutil.rmtree(path)
