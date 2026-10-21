from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dependabot_watcher.local_sync import LocalSyncError
from dependabot_watcher.supervisor_workspace import _git as workspace_git
from dependabot_watcher.validation_snapshot import _git_bytes
from git_environment import GitEnvironment, activate_git_environment, create_git_environment
from test_local_sync import prepare, sync_case
from test_supervisor_workspace import git


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL Git subprocess boundary")


@posix_only
def test_real_git_keeps_fixture_config_after_production_environment_sanitization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_environment: GitEnvironment,
) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    git(workspace, "init", "-b", "main")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.injected.process")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    assert workspace_git(workspace, "config", "--get-regexp", r"^filter\.", empty_ok=True) == ""
    observed = subprocess.run(
        ["git", "-C", str(workspace), "config", "--get-regexp", r"^filter\."],
        capture_output=True, text=True, check=False,
    )
    assert observed.returncode == 0 and observed.stdout.strip() == "filter.injected.process false"


@pytest.mark.parametrize("scope", ["global", "system", "local"])
@posix_only
def test_deliberately_configured_filters_still_block_real_sync(sync_case, scope: str) -> None:
    git(sync_case["path"], "config", "--" + scope, "filter.deliberate.process", "false")
    before = git(sync_case["path"], "rev-parse", "HEAD"), (sync_case["path"] / "app.txt").read_bytes()
    with pytest.raises(LocalSyncError, match="Git filters"):
        prepare(sync_case)
    assert (git(sync_case["path"], "rev-parse", "HEAD"), (sync_case["path"] / "app.txt").read_bytes()) == before


@posix_only
def test_git_launcher_quotes_paths_with_spaces(tmp_path: Path, real_git_executable: str, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = create_git_environment(tmp_path / "configuration with spaces", real_git_executable)
    activate_git_environment(environment, monkeypatch)
    result = subprocess.run(
        [str(environment.executable), "config", "--global", "--get", "user.email"],
        capture_output=True, text=True, check=True,
        env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
    )
    assert result.stdout.strip() == "operator@example.invalid"


def test_fixture_removes_inherited_git_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_environment: GitEnvironment) -> None:
    poisoned = {
        "GIT_DIR": str(tmp_path / "foreign.git"),
        "GIT_WORK_TREE": str(tmp_path / "foreign"),
        "GIT_INDEX_FILE": str(tmp_path / "foreign.index"),
        "GIT_TRACE": str(tmp_path / "foreign.trace"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.email",
        "GIT_CONFIG_VALUE_0": "foreign@example.invalid",
        "git_trace": str(tmp_path / "lowercase.trace"),
        "Git_Dir": str(tmp_path / "mixed-case.git"),
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    activate_git_environment(git_environment, monkeypatch)
    assert not any(key.upper().startswith("GIT_") and key.upper() not in {"GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"} for key in os.environ)
    result = subprocess.run(["git", "config", "--global", "--get", "user.email"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "operator@example.invalid"
    assert list(tmp_path.iterdir()) == []


@posix_only
def test_launcher_preserves_explicit_config_free_child_environment(tmp_path: Path, git_environment: GitEnvironment) -> None:
    git_environment.system_config.write_text('[filter "system"]\n\tprocess = false\n', encoding="utf-8")
    git_environment.global_config.write_text('[filter "global"]\n\tprocess = false\n', encoding="utf-8")
    child = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    child.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(["git", "config", "--get-regexp", r"^filter\."], cwd=tmp_path, env=child, capture_output=True, text=True, check=False)
    assert result.returncode == 1 and result.stdout == ""


@posix_only
def test_snapshot_git_retains_config_free_environment(tmp_path: Path, git_environment: GitEnvironment) -> None:
    git(tmp_path, "init", "-b", "main")
    git_environment.global_config.write_text('[filter "sentinel"]\n\tprocess = false\n', encoding="utf-8")
    observed = _git_bytes(tmp_path, ["config", "--list"], 65_536, lambda: None)
    assert b"filter.sentinel" not in observed
