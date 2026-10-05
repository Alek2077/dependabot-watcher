from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import cli, native_review
from dependabot_watcher.native_review import NativeReviewError, open_native_review
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json


@pytest.fixture
def native_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = tmp_path / "config.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    config.chmod(0o600)
    component = tmp_path / "Watcher.exe"
    component.write_bytes(b"Owned synthetic executable, never executed.")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(native_review, "_wsl_identity", lambda: ("Fixture-Ubuntu", "fixture-user"))
    monkeypatch.setattr(native_review, "windows_host_path", lambda path: "C:\\Fixture\\" + path.name)
    monkeypatch.setattr(native_review, "trusted_windows_executable", lambda unused: Path("/fixture/powershell.exe"))
    def submit(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": arguments, **kwargs})
        return subprocess.CompletedProcess(arguments, 0, '{"pid":123,"status":"submitted"}', "")
    monkeypatch.setattr(native_review.subprocess, "run", submit)
    return {"config": config, "state": tmp_path / "private state", "component": component, "calls": calls}


def invoke(case: dict[str, Any], *, select: bool = True) -> dict[str, Any]:
    return open_native_review(case["config"], case["state"], component=case["component"] if select else None)


def test_launcher_binds_installed_interpreter_scope_without_model_or_merge(native_case: dict[str, Any]) -> None:
    case = native_case
    receipt = invoke(case)
    assert receipt["assurance"] == "process_submission_only"
    assert receipt["model_calls"] is receipt["github_mutations"] is False
    launch_files = list((case["state"] / "native-review/profiles").glob("*/backend-launch.json"))
    assert len(launch_files) == 1
    launch = load_private_json(launch_files[0].parent, Path("backend-launch.json"))
    assert launch["diagnostic_transport"] is False and launch["schema_version"] == 2
    assert launch_files[0].parent.name == hashlib.sha256(launch_files[0].read_bytes()).hexdigest()
    assert launch["component"] == {"path": "C:\\Fixture\\Watcher.exe",
                                  "sha256": hashlib.sha256(case["component"].read_bytes()).hexdigest()}
    arguments = launch["arguments"]
    assert arguments[:6] == ["--distribution", "Fixture-Ubuntu", "--user", "fixture-user", "--exec", "/usr/bin/env"]
    assert ["-u", "PYTHONPATH"] == arguments[6:8]
    assert arguments[10:13] == ["-I", "-m", "dependabot_watcher.review_bridge"]
    assert arguments[-4:] == ["--config", str(case["config"]), "--state-dir", str(case["state"])]
    assert "-c" not in arguments
    payload = json.loads(case["calls"][0]["input"])
    assert payload["executable"].endswith("Watcher.exe") and "--launch-file" in payload["arguments"]
    assert case["calls"][0].get("shell", False) is False
    assert launch_files[0].stat().st_mode & 0o777 == 0o600


def test_saved_component_and_identical_profile_reopen_without_overwrite(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    profile = next((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))
    before = profile.stat().st_mtime_ns
    invoke(native_case, select=False)
    assert profile.stat().st_mtime_ns == before
    assert len(native_case["calls"]) == 2


def test_replaced_component_is_not_silently_launched(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    native_case["component"].write_bytes(b"Different executable")
    with pytest.raises(NativeReviewError, match="changed"):
        invoke(native_case, select=False)
    assert len(native_case["calls"]) == 1
    invoke(native_case)  # Deliberate update selection is separate local consent.
    profiles = list((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))
    assert len(profiles) == 2, "An updated executable must not activate the old component/profile owner."
    assert len({load_private_json(path.parent, Path("backend-launch.json"))["component"]["sha256"] for path in profiles}) == 2


def test_changed_component_location_gets_distinct_owner(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    replacement = native_case["component"].with_name("OtherWatcher.exe")
    replacement.write_bytes(native_case["component"].read_bytes())
    native_case["component"] = replacement
    invoke(native_case)
    assert len(list((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))) == 2


def test_modified_profile_is_refused_without_child(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    path = next((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))
    launch = json.loads(path.read_bytes())
    launch["arguments"][12] = "unintended_backend"
    path.write_text(json.dumps(launch))
    before = path.read_bytes()
    with pytest.raises(NativeReviewError, match="profile changed"):
        invoke(native_case, select=False)
    assert path.read_bytes() == before and len(native_case["calls"]) == 1


def test_missing_registration_has_no_process_launch(native_case: dict[str, Any]) -> None:
    with pytest.raises(NativeReviewError, match="native-component"):
        invoke(native_case, select=False)
    assert not native_case["calls"]


def test_changed_config_path_gets_separate_immutable_launch_profile(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    original = next((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))
    before = original.read_bytes()
    new_config = native_case["config"].with_name("other.toml")
    new_config.write_bytes(native_case["config"].read_bytes())
    native_case["config"] = new_config
    invoke(native_case, select=False)
    assert len(list(original.parent.parent.glob("*/backend-launch.json"))) == 2
    assert original.read_bytes() == before


@pytest.mark.parametrize("output", ['{}', '{"pid":true,"status":"submitted"}', '{"pid":0,"status":"submitted"}',
                                   '{"pid":123,"status":"ready"}', 'not-json'])
def test_ambiguous_submission_keeps_selection_without_claiming_launch(native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    monkeypatch.setattr(native_review.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""))
    with pytest.raises(NativeReviewError, match="confirm"):
        invoke(native_case)
    registration = load_private_json(native_case["state"] / "native-review", Path("component.json"))
    assert set(registration) == {"schema_version", "path", "sha256"}
    assert registration["path"] == str(native_case["component"])


@pytest.mark.parametrize("failing_file", ["backend-launch.json", "component.json"])
def test_registration_failure_prevents_child_submission(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failing_file: str,
) -> None:
    save = native_review.save_private_json

    def fail(directory: Path, path: Path, value: Any, **kwargs: Any) -> None:
        if path.name == failing_file:
            raise RuntimeStateError("diagnostic registration failure")
        save(directory, path, value, **kwargs)

    monkeypatch.setattr(native_review, "save_private_json", fail)
    with pytest.raises(RuntimeStateError, match="registration failure"):
        invoke(native_case)
    assert native_case["calls"] == []


def test_cli_review_reuses_selected_component_without_browser(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[tuple[Path, Path, Path | None]] = []

    def open_window(config: Path, state: Path, *, component: Path | None = None) -> dict[str, Any]:
        captured.append((config, state, component))
        return {"configuration_path": str(config), "state_directory": str(state)}

    monkeypatch.setattr(cli, "open_native_review", open_window)
    assert cli.main(["review", "--config", str(native_case["config"]),
                     "--state-dir", str(native_case["state"]), "--plain"]) == 0
    assert "Windows accepted" in capsys.readouterr().out
    assert captured == [(native_case["config"], native_case["state"], None)]


def test_non_wsl_refuses_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    with pytest.raises(NativeReviewError, match="WSL"):
        open_native_review(tmp_path / "config.toml", tmp_path / "state")
    assert not (tmp_path / "state").exists()


def test_cli_review_opens_aggregate_without_plan_or_inference(native_case: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["review", "--config", str(native_case["config"]), "--state-dir", str(native_case["state"]),
                     "--native-component", str(native_case["component"]), "--plain"]) == 0
    output = capsys.readouterr().out
    assert "does not confirm" in output and "no model call or GitHub change" in output
