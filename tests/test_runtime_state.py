from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dependabot_watcher import runtime_state


@pytest.mark.parametrize("payload", [
    '{"workspace_evidence":{"PRIVATE_MARKER":1},"workspace_evidence":{}}',
    '{"history":[{"handling":"rejected","handling":"pending"}]}',
    '{"key":1,"\\u006bey":2}',
])
def test_private_json_rejects_duplicate_keys_at_every_level_without_rewriting(
    tmp_path: Path, payload: str,
) -> None:
    directory = runtime_state.ensure_private_directory(tmp_path / "state")
    path = directory / "journal.json"
    path.write_text(payload, encoding="utf-8")
    runtime_state.secure_private_file(path)
    before = path.read_bytes()
    with pytest.raises(runtime_state.RuntimeStateError, match="duplicate") as failure:
        runtime_state.load_private_json(directory, Path("journal.json"))
    assert "PRIVATE_MARKER" not in str(failure.value)
    assert path.read_bytes() == before


def test_repeated_key_names_in_distinct_json_objects_are_not_ambiguous(tmp_path: Path) -> None:
    value = {"first": {"status": "pending"}, "second": {"status": "rejected"}}
    runtime_state.save_private_json(tmp_path / "state", Path("journal.json"), value)
    assert runtime_state.load_private_json(tmp_path / "state", Path("journal.json")) == value


def test_opt_in_lock_wait_acquires_after_short_transaction_and_default_still_refuses(tmp_path: Path) -> None:
    ready = threading.Event()
    release = threading.Event()
    def holder() -> None:
        with runtime_state.run_lock(tmp_path / "state"):
            ready.set()
            release.wait(2)
    owner = threading.Thread(target=holder)
    owner.start()
    assert ready.wait(2)
    try:
        with pytest.raises(runtime_state.RuntimeStateError, match="already active"):
            with runtime_state.run_lock(tmp_path / "state"):
                pytest.fail("Default locking must remain fail-fast")
        timer = threading.Timer(.05, release.set)
        timer.start()
        with runtime_state.run_lock(tmp_path / "state", wait_seconds=1):
            assert release.is_set()
        timer.join()
    finally:
        release.set()
        owner.join(timeout=3)
    assert not owner.is_alive()


def test_opt_in_lock_wait_times_out_without_taking_other_owner_lock(tmp_path: Path) -> None:
    with runtime_state.run_lock(tmp_path / "state"):
        started = time.monotonic()
        with pytest.raises(runtime_state.RuntimeStateError, match="already active"):
            with runtime_state.run_lock(tmp_path / "state", wait_seconds=.05):
                pytest.fail("A held lock cannot be taken")
        assert time.monotonic() - started < 1


@pytest.mark.parametrize("wait", [-1, 6, True, float("nan"), float("inf")])
def test_lock_wait_must_be_explicitly_bounded(tmp_path: Path, wait: float) -> None:
    with pytest.raises(runtime_state.RuntimeStateError, match="bounded"):
        with runtime_state.run_lock(tmp_path / "state", wait_seconds=wait):
            pytest.fail("Invalid wait must not acquire the lock")


def test_windows_private_state_applies_and_rechecks_current_user_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def completed(*args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_state, "_windows_platform", lambda: True)
    monkeypatch.setattr(runtime_state, "_trusted_windows_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(runtime_state.subprocess, "run", completed)
    directory = runtime_state.ensure_private_directory(tmp_path / "state")
    artifact = directory / "artifact.json"
    artifact.write_text("{}\n", encoding="utf-8")

    runtime_state.secure_private_file(artifact)
    runtime_state.validate_private_file(artifact)

    assert calls == [
        {"path": str(directory), "directory": True, "apply": True},
        {"path": str(artifact), "directory": False, "apply": True},
        {"path": str(artifact), "directory": False, "apply": False},
        {"path": str(artifact), "directory": False, "apply": False},
    ]


def test_windows_acl_verification_failure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_state, "_windows_platform", lambda: True)
    monkeypatch.setattr(runtime_state, "_trusted_windows_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(
        runtime_state.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=12, stdout="", stderr=""),
    )

    with pytest.raises(runtime_state.RuntimeStateError, match="not restricted"):
        runtime_state.ensure_private_directory(tmp_path / "state")


def test_plan_pruning_revalidates_windows_directory_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, bool, bool]] = []
    state_directory = tmp_path / "state"
    plans_directory = state_directory / "plans"
    plans_directory.mkdir(parents=True)
    retained = plans_directory / f"plan_{'a' * 16}.json"
    retained.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(runtime_state, "_windows_platform", lambda: True)
    monkeypatch.setattr(
        runtime_state,
        "_windows_acl",
        lambda path, *, directory, apply: calls.append((path, directory, apply)),
    )

    runtime_state.prune_plan_artifacts(
        state_directory,
        [f"plan_{'a' * 16}"],
    )

    assert (plans_directory, True, False) in calls
    assert retained.exists()


def test_private_artifact_deletion_validates_and_removes_only_target(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    relative_path = Path("schedules/launchd-job.json")
    artifact = runtime_state.save_private_json(
        state_directory,
        relative_path,
        {"job": "owned"},
    )

    assert runtime_state.delete_private_artifact(state_directory, relative_path) is True
    assert not artifact.exists()
    assert runtime_state.delete_private_artifact(state_directory, relative_path) is False

    with pytest.raises(runtime_state.RuntimeStateError, match="remain inside"):
        runtime_state.delete_private_artifact(state_directory, Path("../outside.json"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link boundary")
def test_private_artifacts_reject_symlinked_intermediate_directory(
    tmp_path: Path,
) -> None:
    state_directory = runtime_state.ensure_private_directory(tmp_path / "state")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    artifact = outside / "marker.json"
    artifact.write_text('{"owned":false}\n', encoding="utf-8")
    artifact.chmod(0o600)
    (state_directory / "schedules").symlink_to(outside, target_is_directory=True)
    relative_path = Path("schedules/marker.json")

    with pytest.raises(runtime_state.RuntimeStateError, match="trusted directory|symbolic link"):
        runtime_state.load_private_json(state_directory, relative_path)
    with pytest.raises(runtime_state.RuntimeStateError, match="trusted directory|symbolic link"):
        runtime_state.save_private_json(state_directory, relative_path, {"owned": True})
    with pytest.raises(runtime_state.RuntimeStateError, match="trusted directory|symbolic link"):
        runtime_state.delete_private_artifact(state_directory, relative_path)

    assert artifact.read_text(encoding="utf-8") == '{"owned":false}\n'


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_native_windows_private_json_round_trip_and_atomic_replace(
    tmp_path: Path,
) -> None:
    state_directory = runtime_state.ensure_private_directory(tmp_path / "state")
    relative_path = Path("reports/latest.json")

    first_path = runtime_state.save_private_json(
        state_directory,
        relative_path,
        {"revision": 1},
    )
    second_path = runtime_state.save_private_json(
        state_directory,
        relative_path,
        {"revision": 2},
    )

    assert first_path == second_path
    assert runtime_state.load_private_json(state_directory, relative_path) == {
        "revision": 2
    }
    assert list(second_path.parent.glob(".run-state-*.tmp")) == []
    runtime_state.validate_private_file(second_path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows file locking")
def test_native_windows_run_lock_rejects_a_second_process(tmp_path: Path) -> None:
    state_directory = runtime_state.ensure_private_directory(tmp_path / "state")
    child = (
        "from pathlib import Path; "
        "from dependabot_watcher.runtime_state import run_lock; "
        "import sys; "
        "lock = run_lock(Path(sys.argv[1])); "
        "lock.__enter__(); "
        "lock.__exit__(None, None, None)"
    )

    with runtime_state.run_lock(state_directory):
        blocked = subprocess.run(
            [sys.executable, "-c", child, str(state_directory)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    released = subprocess.run(
        [sys.executable, "-c", child, str(state_directory)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert blocked.returncode != 0
    assert "Another scheduled reconciliation is already active" in blocked.stderr
    assert released.returncode == 0, released.stderr
