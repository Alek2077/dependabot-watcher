from __future__ import annotations

from pathlib import Path

import pytest

from dependabot_watcher import windows_helpers


def test_trusted_windows_helper_ignores_path_spoof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_system32 = tmp_path / "trusted/System32"
    trusted_helper = trusted_system32 / "wsl.exe"
    trusted_helper.parent.mkdir(parents=True)
    trusted_helper.write_text("trusted", encoding="utf-8")
    fake_helper = tmp_path / "fake/windows/system32/wsl.exe"
    fake_helper.parent.mkdir(parents=True)
    fake_helper.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("PATH", str(fake_helper.parent))
    monkeypatch.setattr(
        windows_helpers,
        "_wsl_system32",
        lambda: trusted_system32.resolve(),
    )

    resolved = windows_helpers.trusted_windows_executable("wsl.exe")

    assert resolved == trusted_helper.resolve()


def test_trusted_windows_helper_rejects_unsupported_or_escaped_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_system32 = tmp_path / "trusted/System32"
    trusted_system32.mkdir(parents=True)
    monkeypatch.setattr(
        windows_helpers,
        "_wsl_system32",
        lambda: trusted_system32.resolve(),
    )

    with pytest.raises(windows_helpers.WindowsHelperError, match="Unsupported"):
        windows_helpers.trusted_windows_executable("cmd.exe")

    outside = tmp_path / "outside-wsl.exe"
    outside.write_text("fake", encoding="utf-8")
    (trusted_system32 / "wsl.exe").symlink_to(outside)
    with pytest.raises(windows_helpers.WindowsHelperError, match="escaped"):
        windows_helpers.trusted_windows_executable("wsl.exe")
