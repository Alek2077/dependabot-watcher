from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from dependabot_watcher.validation_runtime import (
    ValidationLimits, ValidationRuntimeError, allocated_bytes,
    run_validation_command, sandbox_command,
)

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or shutil.which('bwrap') is None,
    reason='Linux/WSL Bubblewrap validation runtime; no host-execution fallback')


@pytest.fixture
def directories(tmp_path: Path) -> tuple[Path, Path]:
    paths = tmp_path / 'snapshot', tmp_path / 'cache'
    for path in paths:
        path.mkdir(mode=0o700)
    return paths


@pytest.mark.parametrize('kwargs', [
    {'timeout_seconds': True}, {'timeout_seconds': 0}, {'maximum_bytes': -1},
    {'minimum_free_bytes': 0}, {'maximum_output_bytes': 2_000_001}, {'network_enabled': 1},
])
def test_limits_are_explicit_and_bounded(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationRuntimeError):
        ValidationLimits(**kwargs)  # type: ignore[arg-type]


def test_storage_does_not_follow_links_and_counts_hardlinks_once(directories: tuple[Path, Path]) -> None:
    snapshot, cache = directories
    file = snapshot / 'data'
    file.write_bytes(b'x' * 8192)
    before = allocated_bytes(snapshot)
    (snapshot / 'alias').hardlink_to(file)
    (snapshot / 'outside').symlink_to(cache, target_is_directory=True)
    (cache / 'unrelated').write_bytes(b'x' * 65536)
    assert allocated_bytes(snapshot) < before + 8192


def test_sandbox_refuses_linked_and_overlapping_directories(directories: tuple[Path, Path]) -> None:
    snapshot, cache = directories
    link = snapshot.parent / 'linked'
    link.symlink_to(snapshot, target_is_directory=True)
    with pytest.raises(ValidationRuntimeError, match='non-linked'):
        sandbox_command(link, cache, 'true', ValidationLimits())
    with pytest.raises(ValidationRuntimeError, match='overlap'):
        sandbox_command(snapshot, snapshot, 'true', ValidationLimits())


def test_no_host_fallback_when_sandbox_missing(monkeypatch: pytest.MonkeyPatch,
                                              directories: tuple[Path, Path]) -> None:
    monkeypatch.setattr(shutil, 'which', lambda _name: None)
    with pytest.raises(ValidationRuntimeError, match='not a fallback'):
        sandbox_command(*directories, 'true', ValidationLimits())


def test_authority_revocation_prevents_process_start(directories: tuple[Path, Path]) -> None:
    def revoked() -> None:
        raise RuntimeError('revoked')
    with pytest.raises(RuntimeError, match='revoked'):
        run_validation_command(*directories, 'touch should-not-exist', ValidationLimits(),
                               ensure_current=revoked)
    assert not (directories[0] / 'should-not-exist').exists()


@pytest.mark.skipif(shutil.which('bwrap') is None, reason='Actual Linux Bubblewrap required')
def test_actual_sandbox_runs_code_without_host_files_or_environment(
    directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, cache = directories
    private = snapshot.parent / 'private-host-file'
    private.write_text('DO NOT EXPOSE')
    monkeypatch.setenv('WATCHER_SYNTHETIC_SECRET', 'DO NOT INHERIT')
    result = run_validation_command(snapshot, cache,
        f'test ! -e "{private}" && test -z "$WATCHER_SYNTHETIC_SECRET" '
        '&& test ! -e /var/run/docker.sock && test ! -e /work/.git '
        '&& printf verified > result.txt && cat result.txt',
        ValidationLimits(), ensure_current=lambda: None)
    assert result.exit_code == 0 and result.output == 'verified'
    assert (snapshot / 'result.txt').read_text() == 'verified'
    assert private.read_text() == 'DO NOT EXPOSE'
    assert len(result.command_sha256) == len(result.output_sha256) == 64


@pytest.mark.skipif(shutil.which('bwrap') is None, reason='Actual Linux Bubblewrap required')
@pytest.mark.parametrize('command,limits,match', [
    ('sleep 5', ValidationLimits(timeout_seconds=1), 'lifetime'),
    ('yes output', ValidationLimits(maximum_output_bytes=1024), 'output bound'),
    ('dd if=/dev/zero of=large bs=1M count=2 2>/dev/null; sleep 2',
     ValidationLimits(maximum_bytes=1024 * 1024), 'storage budget'),
])
def test_actual_command_limits_stop_owned_processes(
    directories: tuple[Path, Path], command: str, limits: ValidationLimits, match: str,
) -> None:
    with pytest.raises(ValidationRuntimeError, match=match):
        run_validation_command(*directories, command, limits, ensure_current=lambda: None)
