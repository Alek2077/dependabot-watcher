from __future__ import annotations

import json
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import cli, native_review
from dependabot_watcher.native_review import NativeReviewError, native_notification, open_native_review, submit_scheduled_supervisor
from dependabot_watcher.runtime_state import RuntimeStateError, load_private_json, load_run_state, save_run_state
from dependabot_watcher.windows_helpers import WindowsHelperError


@pytest.fixture
def native_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = tmp_path / "config.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    config.chmod(0o600)
    component = tmp_path / "Watcher.exe"
    component.write_bytes(b"Owned synthetic executable, never executed.")
    component.with_suffix(".dll").write_bytes(b"Owned synthetic application code, never loaded.")
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
    assert launch["diagnostic_transport"] is False and launch["schema_version"] == 3
    assert launch_files[0].parent.name == hashlib.sha256(launch_files[0].read_bytes()).hexdigest()
    assert launch["component"] == {"path": "C:\\Fixture\\Watcher.exe",
                                  "sha256": hashlib.sha256(case["component"].read_bytes()).hexdigest(),
                                  "assembly_sha256": hashlib.sha256(case["component"].with_suffix(".dll").read_bytes()).hexdigest()}
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


def configure_scheduled(case: dict[str, Any]) -> str:
    invoke(case)
    case["calls"].clear()
    case["config"].write_text(case["config"].read_text() + '\n[supervisor]\nmode = "approval_first"\n'
        'provider = "codex_app_server"\nmodel = "gpt-5.6-luna"\nreasoning_effort = "xhigh"\n'
        'expected_account_type = "chatgpt"\naccess = "inherit"\nmodel_use_acknowledged = true\n'
        'workspaces = [{repository = "example-org/example-app", directory = '
        + json.dumps(str(case["config"].parent / "maintenance")) + ', instruction_sources = []}]\n')
    run_id = "run_" + "a" * 20
    state = load_run_state(case["state"])
    state["last_attempt"] = {"run_id": run_id}
    save_run_state(case["state"], state)
    return run_id


@pytest.mark.parametrize("mode", ["off", "desktop"])
def test_scheduled_submission_is_bound_durable_and_not_agent_completion(native_case: dict[str, Any], mode: str) -> None:
    case = native_case
    run_id = configure_scheduled(case)
    result = submit_scheduled_supervisor(case["config"], case["state"], run_id, notification_mode=mode)
    payload = json.loads(case["calls"][0]["input"])
    assert payload["arguments"].endswith(f"--scheduled-run {run_id} --notify-mode {mode}")
    assert result["assurance"] == "process_submission_only"
    assert result["model_launch_requested"] is True and result["model_calls"] == "not_observed"
    assert result["github_mutations"] is False
    saved = list((case["state"] / "native-review/profiles").glob(f"*/scheduled-submissions/{run_id}.json"))
    assert len(saved) == 1 and load_private_json(saved[0].parent, Path(saved[0].name)) == result


@pytest.mark.parametrize("fault", ["stale", "disabled", "invalid_run", "invalid_notification"])
def test_scheduled_submission_refuses_wrong_run_or_policy_before_process(native_case: dict[str, Any], fault: str) -> None:
    case = native_case
    run_id = configure_scheduled(case)
    mode = "off"
    if fault == "stale": run_id = "run_" + "b" * 20
    elif fault == "disabled": case["config"].write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    elif fault == "invalid_run": run_id = "../other"
    else: mode = "unexpected"
    with pytest.raises(NativeReviewError):
        submit_scheduled_supervisor(case["config"], case["state"], run_id, notification_mode=mode)
    assert case["calls"] == []


def test_uncertain_scheduled_submission_records_uncertainty_not_no_agent_claim(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = native_case
    run_id = configure_scheduled(case)
    monkeypatch.setattr(native_review.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Fixture failure")))
    with pytest.raises(NativeReviewError, match="no automatic retry"):
        submit_scheduled_supervisor(case["config"], case["state"], run_id)
    saved = next((case["state"] / "native-review/profiles").glob(f"*/scheduled-submissions/{run_id}.json"))
    assert load_private_json(saved.parent, Path(saved.name))["status"] == "submission_unconfirmed"


@pytest.mark.parametrize("persist_fault", [OSError, RuntimeStateError])
def test_post_launch_receipt_failure_blocks_repeat_without_false_no_agent_claim(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, persist_fault: type[Exception],
) -> None:
    case = native_case
    run_id = configure_scheduled(case)
    original = native_review.save_private_json
    def write(directory: Path, relative: Path, value: dict[str, Any], **kwargs: Any) -> Path:
        if relative.parts[0] == "scheduled-submissions" and value["status"] != "submission_pending":
            raise persist_fault("Private disk diagnostic must not escape")
        return original(directory, relative, value, **kwargs)
    monkeypatch.setattr(native_review, "save_private_json", write)
    with pytest.raises(NativeReviewError, match="accepted background submission") as failure:
        submit_scheduled_supervisor(case["config"], case["state"], run_id)
    assert "Private disk diagnostic" not in str(failure.value)
    saved = next((case["state"] / "native-review/profiles").glob(f"*/scheduled-submissions/{run_id}.json"))
    assert load_private_json(saved.parent, Path(saved.name))["status"] == "submission_pending"
    with pytest.raises(NativeReviewError, match="already has a submission"):
        submit_scheduled_supervisor(case["config"], case["state"], run_id)
    assert len(case["calls"]) == 1


def test_same_run_concurrent_submission_creates_only_one_process(native_case: dict[str, Any]) -> None:
    case = native_case
    run_id = configure_scheduled(case)
    def submit(unused: int) -> str:
        try:
            return submit_scheduled_supervisor(case["config"], case["state"], run_id)["status"]
        except NativeReviewError as error:
            assert "already has a submission" in str(error)
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == ["refused", "submitted"]
    assert len(case["calls"]) == 1


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
    replacement.with_suffix(".dll").write_bytes(native_case["component"].with_suffix(".dll").read_bytes())
    native_case["component"] = replacement
    invoke(native_case)
    assert len(list((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))) == 2


def test_application_update_with_unchanged_apphost_requires_new_selection(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    original = next((native_case["state"] / "native-review/profiles").glob("*/backend-launch.json"))
    before = original.read_bytes()
    executable_bytes = native_case["component"].read_bytes()
    native_case["component"].with_suffix(".dll").write_bytes(b"New managed application; identical apphost.")
    with pytest.raises(NativeReviewError, match="changed"):
        invoke(native_case, select=False)
    assert len(native_case["calls"]) == 1 and native_case["component"].read_bytes() == executable_bytes
    assert original.read_bytes() == before
    invoke(native_case)
    profiles = list(original.parent.parent.glob("*/backend-launch.json"))
    components = [load_private_json(path.parent, Path("backend-launch.json"))["component"] for path in profiles]
    assert len(profiles) == 2
    assert len({item["sha256"] for item in components}) == 1
    assert len({item["assembly_sha256"] for item in components}) == 2


def test_incomplete_component_cannot_register_or_submit(native_case: dict[str, Any]) -> None:
    native_case["component"].with_suffix(".dll").unlink()
    with pytest.raises(NativeReviewError, match="complete"):
        invoke(native_case)
    assert not native_case["calls"]
    assert not (native_case["state"] / "native-review/component.json").exists()
    assert not (native_case["state"] / "native-review/profiles").exists()


def test_linked_application_dll_is_not_selected(native_case: dict[str, Any]) -> None:
    assembly = native_case["component"].with_suffix(".dll")
    target = assembly.with_name("other-application.dll")
    assembly.rename(target)
    try:
        assembly.symlink_to(target)
    except OSError:
        pytest.skip("This host cannot create a symlink for the negative control.")
    with pytest.raises(NativeReviewError, match="complete"):
        invoke(native_case)
    assert not native_case["calls"]


def test_legacy_executable_only_registration_requires_deliberate_reselection(native_case: dict[str, Any]) -> None:
    invoke(native_case)
    registration = native_case["state"] / "native-review/component.json"
    legacy = json.loads(registration.read_bytes())
    legacy["schema_version"] = 1
    legacy.pop("assembly_sha256")
    registration.write_text(json.dumps(legacy))
    before = registration.read_bytes()
    with pytest.raises(NativeReviewError, match="native-component"):
        invoke(native_case, select=False)
    assert len(native_case["calls"]) == 1 and registration.read_bytes() == before
    invoke(native_case)
    assert load_private_json(registration.parent, Path(registration.name))["schema_version"] == 2


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
    assert set(registration) == {"schema_version", "path", "sha256", "assembly_sha256"}
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


def notification_submit(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    def submit(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        case["calls"].append({"argv": arguments, **kwargs})
        profile = next((case["state"] / "native-review/profiles").iterdir()).name
        receipt = {"schema_version": 1, "status": "accepted", "profile_id": profile,
                   "notification_id": 42, "assurance": "adapter_accepted_only", **overrides}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(receipt), "")
    monkeypatch.setattr(native_review.subprocess, "run", submit)


def test_notification_test_uses_hidden_bound_component_without_run_state(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    notification_submit(native_case, monkeypatch)
    receipt = native_notification(native_case["config"], native_case["state"],
                                  operation="test", component=native_case["component"])
    assert receipt["status"] == "accepted"
    payload = json.loads(native_case["calls"][0]["input"])
    assert "--notification-test" in payload["arguments"] and "--launch-file" in payload["arguments"]
    assert "$start.CreateNoWindow = $true" in native_case["calls"][0]["argv"][-1]
    assert not (native_case["state"] / "run-state.json").exists()
    assert not (native_case["state"] / "review-queue.json").exists()


@pytest.mark.parametrize("overrides", [{"notification_id": True}, {"notification_id": 0},
    {"profile_id": "a" * 64}, {"status": "registered"}, {"assurance": "user_saw_it"}, {"unexpected": "field"}])
def test_notification_receipt_is_bound_and_does_not_claim_visibility(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any],
) -> None:
    notification_submit(native_case, monkeypatch, **overrides)
    with pytest.raises(NativeReviewError, match="receipt|acceptance"):
        native_notification(native_case["config"], native_case["state"], operation="test", component=native_case["component"])


@pytest.mark.parametrize("operation", ["status", "remove"])
def test_notification_status_and_removal_do_not_invent_registration(native_case: dict[str, Any], operation: str) -> None:
    with pytest.raises(NativeReviewError, match="existing"):
        native_notification(native_case["config"], native_case["state"], operation=operation)
    assert native_case["calls"] == [] and not native_case["state"].exists()


def test_notification_test_cli_clearly_requests_user_observation(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    notification_submit(native_case, monkeypatch)
    assert cli.main(["notification", "test", "--config", str(native_case["config"]),
        "--state-dir", str(native_case["state"]), "--native-component", str(native_case["component"]), "--plain"]) == 0
    text = capsys.readouterr().out
    assert "Please check its visibility" in text and "does not prove display" in text


@pytest.mark.parametrize("helper", ["windows_host_path", "trusted_windows_executable"])
def test_notification_helper_failure_is_expected_and_sanitized(
    native_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, helper: str,
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> str:
        raise WindowsHelperError("private mount diagnostic")
    monkeypatch.setattr(native_review, helper, unavailable)
    with pytest.raises(NativeReviewError) as failure:
        native_notification(native_case["config"], native_case["state"], operation="test", component=native_case["component"])
    assert "private mount diagnostic" not in str(failure.value)
    assert native_case["calls"] == []
