from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from git_environment import GitEnvironment, activate_git_environment, create_git_environment


@pytest.fixture(scope="session")
def real_git_executable() -> str | None:
    executable = shutil.which("git")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True,
        env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
    )
    version = re.match(r"git version (\d+)\.(\d+)", result.stdout)
    if version is None or tuple(map(int, version.groups())) < (2, 32):
        pytest.fail("Synthetic Git tests require Git 2.32+ to isolate global/system configuration safely.")
    return executable


@pytest.fixture(autouse=True)
def git_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, real_git_executable: str | None,
) -> GitEnvironment | None:
    """Leave host configuration alone; each test owns its global/system files."""
    if real_git_executable is None:
        return None
    directory = tmp_path_factory.mktemp("git-environment") / "configuration"
    environment = create_git_environment(directory, real_git_executable)
    activate_git_environment(environment, monkeypatch)
    return environment
