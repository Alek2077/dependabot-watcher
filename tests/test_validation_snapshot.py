from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from dependabot_watcher.validation_runtime import ValidationLimits, ValidationRuntimeError
from dependabot_watcher.validation_snapshot import prepare_validation_snapshot
from test_supervisor_workspace import git

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX private exact-revision validation snapshots')


def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Synthetic')
    git(root, 'config', 'user.email', 'synthetic@example.invalid')
    (root / 'app.js').write_text('console.log("old")\n')
    (root / '.env.synthetic').write_text('SYNTHETIC_ONLY=excluded')
    (root / 'secrets').mkdir()
    (root / 'secrets' / 'synthetic.txt').write_text('excluded synthetic fixture')
    (root / 'AGENTS.md').symlink_to('/synthetic/missing/instructions')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Synthetic revision')
    return root, git(root, 'rev-parse', 'HEAD')


def test_exact_commit_preparation_preserves_working_files_and_excludes_secrets(tmp_path: Path) -> None:
    root, sha = repository(tmp_path)
    (root / 'app.js').write_text('unfinished developer work\n')
    before = git(root, 'status', '--porcelain'), git(root, 'show-ref')
    destination = tmp_path / 'state' / 'snapshot'
    receipt = prepare_validation_snapshot(root, destination, sha=sha,
        limits=ValidationLimits(), ensure_current=lambda: None)
    assert (destination / 'app.js').read_text() == 'console.log("old")\n'
    assert not (destination / '.env.synthetic').exists()
    assert not (destination / 'secrets').exists()
    assert not (destination / 'AGENTS.md').exists()
    assert not (destination / '.git').exists()
    assert (root / 'app.js').read_text() == 'unfinished developer work\n'
    assert (git(root, 'status', '--porcelain'), git(root, 'show-ref')) == before
    assert receipt['head_sha'] == sha and receipt['tracked_files'] == 1
    assert receipt['omitted_entries'] == 3
    assert receipt['snapshot_complete'] is False
    assert {item['path'] for item in cast(list[dict[str, str]], receipt['omissions'])} == {
        '.env.synthetic', 'secrets/synthetic.txt', 'AGENTS.md'}
    assert (destination.parent / 'snapshot.json').exists()


def test_export_attributes_cannot_hide_or_transform_validation_source(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)
    (root / '.gitattributes').write_text('app.js export-ignore\nversion.txt export-subst\n')
    (root / 'version.txt').write_text('$Format:%H$\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Synthetic export attributes')
    sha = git(root, 'rev-parse', 'HEAD')
    destination = tmp_path / 'state' / 'snapshot'
    prepare_validation_snapshot(root, destination, sha=sha,
        limits=ValidationLimits(), ensure_current=lambda: None)
    assert (destination / 'app.js').read_text() == 'console.log("old")\n'
    assert (destination / 'version.txt').read_text() == '$Format:%H$\n'


@pytest.mark.parametrize('name', ['.netrc', '.git-credentials', '.docker/config.json',
                                'gcloud/application_default_credentials.json', 'synthetic.pem', 'synthetic.p12',
                                '.config/gh/hosts.yml', '.config/pip/pip.conf', '.kube/config', '.azure/auth.json'])
def test_common_credential_paths_are_omitted_and_reported(tmp_path: Path, name: str) -> None:
    root, _ = repository(tmp_path)
    credential = root / name
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text('synthetic fixture only')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Synthetic credential path')
    destination = tmp_path / 'state' / 'snapshot'
    receipt = prepare_validation_snapshot(root, destination, sha=git(root, 'rev-parse', 'HEAD'),
        limits=ValidationLimits(), ensure_current=lambda: None)
    assert not (destination / name).exists()
    assert {'path': name, 'reason': 'credential_path'} in cast(list[dict[str, str]], receipt['omissions'])


def test_existing_destination_is_not_adopted_or_removed(tmp_path: Path) -> None:
    root, sha = repository(tmp_path)
    destination = tmp_path / 'saved'
    destination.mkdir(mode=0o700)
    (destination / 'keep.txt').write_text('keep')
    with pytest.raises(ValidationRuntimeError, match='new canonical'):
        prepare_validation_snapshot(root, destination, sha=sha,
            limits=ValidationLimits(), ensure_current=lambda: None)
    assert (destination / 'keep.txt').read_text() == 'keep'


def test_insufficient_space_refuses_before_archive_or_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, sha = repository(tmp_path)
    destination = tmp_path / 'state' / 'snapshot'
    monkeypatch.setattr('dependabot_watcher.validation_snapshot.shutil.disk_usage', lambda _path: SimpleNamespace(free=1))
    with pytest.raises(ValidationRuntimeError, match='reserve'):
        prepare_validation_snapshot(root, destination, sha=sha,
            limits=ValidationLimits(minimum_free_bytes=64 * 1024**3, maximum_bytes=16 * 1024**3),
            ensure_current=lambda: None)
    assert not destination.exists()
