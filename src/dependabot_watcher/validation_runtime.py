"""POSIX validation runner without mounted host credentials or user HOME.

Agent-selected commands run inside an existing Bubblewrap installation, never
on the host as a fallback. This module does not grant action authority, choose
tests, fetch revisions, provision dependencies or trust model-written receipts.
"""
from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .runtime_state import validate_user_owned_directory


class ValidationRuntimeError(ValueError):
    """Validation could not run within its declared boundary."""


@dataclass(frozen=True)
class ValidationLimits:
    timeout_seconds: int = 120
    maximum_bytes: int = 256 * 1024 * 1024
    minimum_free_bytes: int = 512 * 1024 * 1024
    maximum_output_bytes: int = 256 * 1024
    network_enabled: bool = False

    def __post_init__(self) -> None:
        for name, lower, upper in (
            ("timeout_seconds", 1, 3600),
            ("maximum_bytes", 1, 16 * 1024**3),
            ("minimum_free_bytes", 1, 64 * 1024**3),
            ("maximum_output_bytes", 1, 2_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValidationRuntimeError(f"Invalid validation limit: {name}.")
        if type(self.network_enabled) is not bool:
            raise ValidationRuntimeError("Validation network selection must be explicit.")


@dataclass(frozen=True)
class ValidationCommandResult:
    exit_code: int
    duration_seconds: float
    output: str
    output_sha256: str
    command_sha256: str
    allocated_bytes: int


def allocated_bytes(root: Path, *, maximum_entries: int = 250_000,
                    ensure_current: Callable[[], None] = lambda: None) -> int:
    """Count owned allocation without following links or crossing filesystems.

    Hard links count once. Polling is an operational budget, not a kernel quota;
    hostile code can grow between polls. Admission keeps a free-space reserve.
    """
    seen: set[tuple[int, int]] = set()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    device = os.fstat(descriptor).st_dev
    last_check = 0.0

    def checkpoint() -> None:
        nonlocal last_check
        if time.monotonic() - last_check >= .05:
            ensure_current()
            last_check = time.monotonic()

    def count(metadata: os.stat_result) -> int:
        identity = metadata.st_dev, metadata.st_ino
        if identity in seen:
            return 0
        seen.add(identity)
        if len(seen) > maximum_entries or metadata.st_dev != device:
            raise ValidationRuntimeError("Validation storage is too large or crosses a filesystem.")
        return metadata.st_blocks * 512

    def descend(current: int, depth: int = 0) -> int:
        checkpoint()
        if depth > 256:
            raise ValidationRuntimeError('Validation storage nesting exceeds its bound.')
        total = count(os.fstat(current))
        with os.scandir(current) as entries:
            for entry in entries:
                checkpoint()
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        child = os.open(entry.name, flags, dir_fd=current)
                        try:
                            total += descend(child, depth + 1)
                        finally:
                            os.close(child)
                    else:
                        total += count(metadata)
                except FileNotFoundError:
                    continue  # Commands can remove their own generated files.
        return total

    try:
        result = descend(descriptor)
        ensure_current()
        return result
    except OSError as exc:
        raise ValidationRuntimeError('Validation storage changed during its bounded scan.') from exc
    finally:
        os.close(descriptor)


def sandbox_command(snapshot: Path, cache: Path, command: str, limits: ValidationLimits) -> list[str]:
    """Construct an explicit minimal filesystem; host HOME/Git/config stay out.

    Network can be enabled for dependency acquisition only by saved capability.
    It is not a registry allowlist or a remote-action credential guarantee for
    secrets embedded in the supplied snapshot; preparation must exclude secrets.
    """
    if os.name != "posix" or not Path('/proc').is_dir():
        raise ValidationRuntimeError("Isolated validation currently requires Linux/WSL.")
    executable = shutil.which('bwrap')
    if executable is None:
        raise ValidationRuntimeError("Bubblewrap is unavailable; host execution is not a fallback.")
    for directory in (snapshot, cache):
        if not directory.is_absolute() or directory.is_symlink():
            raise ValidationRuntimeError("Validation directories must be explicit owned non-linked paths.")
        validate_user_owned_directory(directory)
        if directory.resolve() != directory or directory.stat().st_mode & 0o077:
            raise ValidationRuntimeError("Validation directories must be canonical and private.")
    if snapshot == cache or snapshot in cache.parents or cache in snapshot.parents:
        raise ValidationRuntimeError("Validation snapshot and cache must not overlap.")
    if not isinstance(command, str) or not command.strip() or '\0' in command or len(command.encode()) > 32_768:
        raise ValidationRuntimeError("Validation command is empty or exceeds its bound.")
    argv = [executable, '--unshare-all', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--clearenv']
    if limits.network_enabled:
        argv.append('--share-net')
    for directory in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(directory).exists():
            argv.extend(['--ro-bind', directory, directory])
    argv.extend(['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                 '--dir', '/etc', '--dir', '/home', '--dir', '/home/validator',
                 '--bind', str(snapshot), '/work', '--bind', str(cache), '/cache'])
    for source in ('/etc/ssl/certs', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf'):
        if Path(source).exists():
            argv.extend(['--ro-bind', source, source])
    for name, value in (
        ('PATH', '/usr/bin:/bin'), ('HOME', '/home/validator'), ('TMPDIR', '/work/.tmp'),
        ('CI', 'true'), ('npm_config_cache', '/cache/npm'), ('npm_config_userconfig', '/dev/null'),
        ('PIP_CACHE_DIR', '/cache/pip'), ('UV_CACHE_DIR', '/cache/uv'),
        ('GIT_CONFIG_NOSYSTEM', '1'), ('GIT_TERMINAL_PROMPT', '0'),
    ):
        argv.extend(['--setenv', name, value])
    argv.extend(['--chdir', '/work', '/bin/sh', '-c', command])
    return argv


def run_validation_command(
    snapshot: Path, cache: Path, command: str, limits: ValidationLimits,
    *, ensure_current: Callable[[], None], storage_reserve_paths: tuple[Path, ...] = (),
    storage_roots: tuple[Path, ...] = (),
) -> ValidationCommandResult:
    """Run with bounded pipes/lifetime and authority/storage checks throughout.

    The owning job supplies revocation/client checks. Output is transient private
    evidence; callers must not persist it as unrestricted logs or public reports.
    Same-user hostile writers and hard disk quotas require stronger OS controls.
    """
    argv = sandbox_command(snapshot, cache, command, limits)
    roots = (snapshot, cache)
    # Backend-selected job roots include other admitted copies and revision
    # caches. They are budget inputs only, not extra sandbox mounts.
    budget_roots = storage_roots or roots

    def check() -> int:
        ensure_current()
        usage = sum(allocated_bytes(path, ensure_current=ensure_current) for path in budget_roots)
        if usage > limits.maximum_bytes:
            raise ValidationRuntimeError("Validation reached its storage budget.")
        for path in (*roots, *storage_reserve_paths):
            if shutil.disk_usage(path).free < limits.minimum_free_bytes:
                raise ValidationRuntimeError("Validation storage reserve is insufficient.")
        return usage

    check()
    temporary = snapshot / '.tmp'
    if temporary.is_symlink() or temporary.exists() and not temporary.is_dir():
        raise ValidationRuntimeError("Validation temporary directory is unsafe.")
    temporary.mkdir(mode=0o700, exist_ok=True)
    process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    output = bytearray()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            last_budget_check = 0.0
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                ensure_current()
                if now - started >= limits.timeout_seconds:
                    raise ValidationRuntimeError("Validation command exceeded its lifetime.")
                if now - last_budget_check >= .25:
                    check()
                    last_budget_check = time.monotonic()
                for key, _ in selector.select(.05):
                    block = os.read(key.fd, 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(block)
                    if len(output) > limits.maximum_output_bytes:
                        raise ValidationRuntimeError("Validation command exceeded its output bound.")
        code = process.wait(timeout=1)
        usage = check()
        return ValidationCommandResult(code, round(time.monotonic() - started, 3),
            output.decode('utf-8', errors='replace'), hashlib.sha256(output).hexdigest(),
            hashlib.sha256(command.encode()).hexdigest(), usage)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationRuntimeError("Isolated validation could not complete; no host fallback was used.") from exc
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
