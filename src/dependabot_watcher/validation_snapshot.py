"""Exact-commit validation copies without linked worktrees or developer changes.

Snapshots contain tracked ordinary files, excluding Git metadata, recognized
credential paths and trusted harness symlink targets. Unknown embedded secrets
are not detected by path filtering. The supervisor keeps its separately verified
instruction context. Preparation is not authorization to execute repository code.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Callable

from .runtime_state import ensure_private_directory, save_private_json
from .review_queue import ReviewQueueError
from .supervisor_workspace import _git
from .validation_runtime import ValidationLimits, ValidationRuntimeError, allocated_bytes


def _excluded(name: str) -> bool:
    parts = PurePosixPath(name).parts
    credential_config = any(parts[index:index + 2] == ('.config', folder)
        for index in range(len(parts) - 1) for folder in ('gh', 'pip', 'gcloud', 'containers'))
    if credential_config:
        return True
    return any(part in {'.git', 'secrets', '.ssh', '.aws', '.codex', '.npmrc', '.pypirc',
                       '.git-credentials', '.netrc', '_netrc', '.docker', 'gcloud',
                       '.kube', '.azure', 'application_default_credentials.json'}
               or part.startswith('.env') or any(fnmatch.fnmatch(part, pattern)
                   for pattern in ('*.key', '*.pem', '*.p12', '*.pfx')) for part in parts)


def _git_bytes(workspace: Path, arguments: list[str], maximum_bytes: int,
               ensure_current: Callable[[], None]) -> bytes:
    executable = shutil.which('git')
    if executable is None:
        raise ValidationRuntimeError('Git is unavailable for validation preparation.')
    environment = {'PATH': '/usr/bin:/bin', 'GIT_TERMINAL_PROMPT': '0',
                   'GIT_CONFIG_NOSYSTEM': '1', 'HOME': '/nonexistent'}
    # Read raw tree/blob objects; archive attributes and checkout filters must
    # not transform the revision that the validation receipt identifies.
    command = [executable, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-C', str(workspace), *arguments]
    process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    assert process.stdout is not None and process.stderr is not None
    contents = bytearray()
    diagnostics = 0
    started = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                ensure_current()
                if time.monotonic() - started > 30:
                    raise ValidationRuntimeError('Validation archive exceeded its lifetime.')
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stdout:
                        contents.extend(chunk)
                    else:
                        diagnostics += len(chunk)
                    if len(contents) > maximum_bytes or diagnostics > 65_536:
                        raise ValidationRuntimeError('Validation archive exceeds its size bound.')
        if process.wait(timeout=1) != 0:
            raise ValidationRuntimeError('Validation archive failed; private diagnostics omitted.')
        return bytes(contents)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def _archive(workspace: Path, sha: str, maximum_bytes: int,
             ensure_current: Callable[[], None]) -> tuple[bytes, list[dict[str, str]]]:
    """Build an internal tar from raw objects, never Git export attributes."""
    tree = _git_bytes(workspace, ['ls-tree', '-r', '-z', sha], maximum_bytes, ensure_current)
    output = io.BytesIO()
    omitted: list[dict[str, str]] = []
    entries = tree.split(b'\0')
    deadline = time.monotonic() + 30
    if len(entries) > 50_001:
        raise ValidationRuntimeError('Validation tree exceeds its entry bound.')
    with tarfile.open(fileobj=output, mode='w:') as archive:
        for entry in entries:
            if not entry:
                continue
            ensure_current()
            if time.monotonic() > deadline:
                raise ValidationRuntimeError('Validation raw-tree preparation exceeded its lifetime.')
            try:
                header, raw_name = entry.split(b'\t', 1)
                mode, kind, oid = header.decode('ascii').split(' ')
                name = raw_name.decode('utf-8', errors='strict')
            except (ValueError, UnicodeError) as exc:
                raise ValidationRuntimeError('Validation tree has an unsupported path or entry.') from exc
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or not path.parts:
                raise ValidationRuntimeError('Validation tree contains an unsafe path.')
            if _excluded(name) or mode not in {'100644', '100755'} or kind != 'blob':
                omitted.append({'path': name, 'reason': 'credential_path' if _excluded(name)
                                else 'link_or_nonordinary_entry'})
                continue
            if re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', oid) is None:
                raise ValidationRuntimeError('Validation tree has an invalid object identity.')
            blob = _git_bytes(workspace, ['cat-file', 'blob', oid],
                              max(0, maximum_bytes - output.tell()), ensure_current)
            member = tarfile.TarInfo(name)
            member.size = len(blob)
            member.mode = 0o755 if mode == '100755' else 0o644
            archive.addfile(member, io.BytesIO(blob))
            if output.tell() > maximum_bytes:
                raise ValidationRuntimeError('Validation snapshot exceeds its archive bound.')
    if output.tell() > maximum_bytes:
        raise ValidationRuntimeError('Validation snapshot exceeds its archive bound.')
    return output.getvalue(), omitted


def prepare_validation_snapshot(
    workspace: Path, destination: Path, *, sha: str, limits: ValidationLimits,
    ensure_current: Callable[[], None],
) -> dict[str, object]:
    """Prepare one new backend-selected directory; never adopt an existing copy.

    Caller must verify origin, bind SHA to fresh collected evidence and hold its
    workspace lease. Exact-object fetching belongs to that trusted caller, not
    arbitrary user/model paths. Initial maximum archive is 32MiB to bound memory.
    """
    ensure_current()
    if not isinstance(sha, str) or re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', sha) is None:
        raise ValidationRuntimeError('Validation requires an exact collected commit.')
    if (not destination.is_absolute() or destination.resolve() != destination
            or os.path.lexists(destination)):
        raise ValidationRuntimeError('Validation destination must be a new canonical owned path.')
    parent = ensure_private_directory(destination.parent)
    if shutil.disk_usage(parent).free < limits.minimum_free_bytes + limits.maximum_bytes:
        raise ValidationRuntimeError('Validation preparation lacks its requested storage reserve.')
    try:
        verified = _git(workspace, 'rev-parse', '--verify', sha + '^{commit}')
    except ReviewQueueError as exc:
        ensure_current()
        raise ValidationRuntimeError('Validation revision is unavailable; no checks ran.') from exc
    if verified.casefold() != sha.casefold():
        raise ValidationRuntimeError('Validation commit identity could not be verified.')
    archive, omissions = _archive(workspace, sha, min(limits.maximum_bytes, 32 * 1024**2), ensure_current)
    destination.mkdir(mode=0o700)
    files = 0
    omitted = len(omissions)
    total = 0
    fingerprints: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
            for member in source:
                ensure_current()
                name = PurePosixPath(member.name)
                if name.is_absolute() or '..' in name.parts or not name.parts or '\0' in member.name:
                    raise ValidationRuntimeError('Validation archive contains an unsafe path.')
                if _excluded(member.name) or member.issym() or member.islnk():
                    omitted += 1
                    continue
                path = destination.joinpath(*name.parts)
                if member.isdir():
                    path.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if not member.isfile() or member.size < 0:
                    raise ValidationRuntimeError('Validation archive contains an unsupported file type.')
                total += member.size
                files += 1
                if total > limits.maximum_bytes or files > 50_000:
                    raise ValidationRuntimeError('Validation snapshot exceeds its bound.')
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                blob = source.extractfile(member)
                if blob is None:
                    raise ValidationRuntimeError('Validation archive file is unavailable.')
                with blob, path.open('xb') as output:
                    shutil.copyfileobj(blob, output, length=65_536)
                path.chmod(0o700 if member.mode & stat.S_IXUSR else 0o600)
                fingerprints[str(name)] = hashlib.sha256(path.read_bytes()).hexdigest()
        ensure_current()
        receipt: dict[str, object] = {'schema_version': 1, 'head_sha': sha,
            'tracked_files': files, 'omitted_entries': omitted, 'source_bytes': total,
            'allocated_bytes': allocated_bytes(destination), 'git_metadata_present': False,
            'omissions': omissions, 'snapshot_complete': omitted == 0,
            'source_fingerprints': fingerprints}
        save_private_json(parent, Path(destination.name + '.json'), receipt)
        return receipt
    except BaseException:
        # Preserve the owned partial copy for explicit recovery; do not silently
        # delete source or generated work on failure, and never claim readiness.
        raise
