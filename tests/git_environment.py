"""Controlled configuration for real Git subprocesses in synthetic tests."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class GitEnvironment:
    executable: Path
    global_config: Path
    system_config: Path


def activate_git_environment(environment: GitEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discard inherited Git redirects; tests may explicitly add their own afterward."""
    for key in tuple(os.environ):
        if key.upper().startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(environment.system_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(environment.global_config))
    if os.name == "posix":
        monkeypatch.setenv("PATH", str(environment.executable.parent) + os.pathsep + os.environ.get("PATH", ""))


def create_git_environment(directory: Path, real_git: str) -> GitEnvironment:
    directory.mkdir(parents=True)
    global_config = directory / "global.gitconfig"
    system_config = directory / "system.gitconfig"
    global_config.write_text(
        '[user]\n\tname = Synthetic Git Operator\n\temail = operator@example.invalid\n'
        '[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n'
        '[core]\n\tautocrlf = false\n', encoding="utf-8",
    )
    system_config.write_text("", encoding="utf-8")
    executable = Path(real_git)
    if os.name == "posix":
        # Production deliberately strips GIT_* overrides. Apply test config at
        # the executable boundary so the real Git still sees it in child runs.
        executable = directory / "git"
        executable.write_text(
            '#!/bin/sh\n'
            f'launcher_directory={shlex.quote(str(directory))}\n'
            'case ":${PATH-}:" in\n'
            '*":$launcher_directory:"*)\n'
            f'if [ "${{GIT_CONFIG_SYSTEM+x}}" != x ]; then export GIT_CONFIG_SYSTEM={shlex.quote(str(system_config))}; fi\n'
            f'if [ "${{GIT_CONFIG_GLOBAL+x}}" != x ]; then export GIT_CONFIG_GLOBAL={shlex.quote(str(global_config))}; fi\n'
            ';;\n'
            'esac\n'
            f'exec {shlex.quote(real_git)} "$@"\n', encoding="utf-8",
        )
        executable.chmod(0o700)
    return GitEnvironment(executable, global_config, system_config)
