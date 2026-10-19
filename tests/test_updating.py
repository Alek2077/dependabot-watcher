"""Deterministic updater orchestration; native ownership has separate checks."""
from __future__ import annotations

import json
import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from dependabot_watcher import cli, execution, scheduling, update_coordination, updating
from dependabot_watcher.execution import ExecutionError
from dependabot_watcher.review_queue import supervisor_context_digest
from dependabot_watcher.schedulers.common import ScheduleError
from dependabot_watcher.runtime_state import load_private_json, run_lock, save_private_json
from dependabot_watcher.update_coordination import UpdateError, task_definition_identity


def test_task_identity_preserves_triggers_but_ignores_temporary_enabled_bit():
    template = ('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                '<Triggers><TimeTrigger><StartBoundary>{time}</StartBoundary></TimeTrigger></Triggers>'
                '<Settings><Enabled>{enabled}</Enabled><Hidden>true</Hidden></Settings></Task>')
    original = task_definition_identity(template.format(time="2026-10-18T10:00:00", enabled="true"))
    assert original == task_definition_identity(template.format(time="2026-10-18T10:00:00", enabled="false"))
    assert original != task_definition_identity(template.format(time="2026-10-18T11:00:00", enabled="true"))


def test_update_guard_is_read_only_and_retains_interrupted_identity(tmp_path):
    state = tmp_path / "not-created"
    updating.require_no_update(state)
    assert not state.exists()
    session = "update_" + "a" * 32
    save_private_json(state, Path("update-active.json"), {"schema_version": 1, "session_id": session})
    before = (state / "update-active.json").read_bytes()
    with pytest.raises(UpdateError, match=session):
        updating.require_no_update(state)
    assert (state / "update-active.json").read_bytes() == before


@pytest.fixture
def installation(tmp_path, monkeypatch):
    config = tmp_path / "profile.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    state = tmp_path / "state"
    session = "update_" + "b" * 32
    root = state / "updates" / session
    component = {"path": str(tmp_path / "new" / "Watcher.exe"), "sha256": "c" * 64,
                 "assembly_sha256": "d" * 64, "manifest_sha256": "e" * 64}
    record = {"schema_version": 1, "session_id": session, "phase": "prepared", "source_commit": "f" * 40,
              "config_path": str(config.resolve()), "configuration_sha256": updating._hash(config),
              "installed": {"python": "/fixture/bin/python", "base_python": "/fixture/base/python",
                            "command": "/fixture/bin/dependabot-watcher"},
              "component": component, "source_files": {"__init__.py": "a" * 64},
              "previous_component": {key: component[key] for key in ("path", "sha256", "assembly_sha256")},
              "state_snapshot": updating._state_snapshot(state),
              "wheel": str(root / "dist" / "dependabot_watcher-0.3.0.whl"),
              "schedule": {"enabled": True}}
    events = []

    class Schedule:
        def set_enabled(self, saved, enabled):
            events.append(("schedule", enabled))

    owners = [{"pid": 123, "fixture": True}]
    monkeypatch.setattr(updating, "matching_owners", lambda *args: list(owners))

    def close(owner):
        events.append(("close", owner["pid"]))
        owners.remove(owner)

    monkeypatch.setattr(updating, "close_owner", close)
    monkeypatch.setattr(updating, "_command", lambda argv, **kwargs: events.append(("command", tuple(argv))) or "")
    monkeypatch.setattr(updating, "_verify_installed", lambda *args: events.append(("verify",)))
    monkeypatch.setattr(updating, "_installed_identity", lambda *args: record["installed"])
    monkeypatch.setattr(updating, "_probe_state", lambda *args: {})
    monkeypatch.setattr(updating, "_verify_component", lambda *args: component)
    monkeypatch.setattr(updating, "_prepare_native_review", lambda *args, **kwargs: events.append(("register",)))
    monkeypatch.setattr(updating, "verify_native_component", lambda *args:
                        (Path(component["path"]), component["sha256"], component["assembly_sha256"]))
    return config, state, root, record, Schedule(), events


@pytest.mark.parametrize("enabled", [True, False])
def test_update_closes_exact_owner_then_restores_prior_schedule(installation, enabled):
    config, state, root, record, schedule, events = installation
    record["schedule"]["enabled"] = enabled
    result = updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert result["status"] == "updated" and result["window_launch_submitted"]
    assert result["window_visibility"] == "unverified"
    assert events[0:2] == [("schedule", False), ("close", 123)]
    assert events[-2][0:2] == ("schedule", enabled)
    assert events[2][1][1:3] == ("tool", "install")
    assert events[3:5] == [("verify",), ("register",)]
    assert not (state / "update-active.json").exists()
    assert load_private_json(root, Path("receipt.json"))["phase"] == "complete"


def test_stuck_owner_does_not_install_and_restores_schedule(installation, monkeypatch):
    config, state, root, record, schedule, events = installation
    def blocked(owner):
        raise UpdateError("Owner is draining; no kill.")
    monkeypatch.setattr(updating, "close_owner", blocked)
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == [("schedule", False), ("schedule", True)]
    assert not (state / "update-active.json").exists()
    assert load_private_json(root, Path("receipt.json"))["phase"] == "cancelled_before_installation"


def test_partial_install_keeps_recovery_fence_and_schedule_paused(installation, monkeypatch):
    config, state, root, record, schedule, events = installation
    def failed(*args, **kwargs):
        raise UpdateError("Installer failed.")
    monkeypatch.setattr(updating, "_command", failed)
    with pytest.raises(UpdateError, match="needs recovery"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == [("schedule", False), ("close", 123)]
    with pytest.raises(UpdateError, match=record["session_id"]):
        updating.require_no_update(state)
    assert load_private_json(root, Path("receipt.json"))["phase"] == "recovery_required"


def test_reopen_failure_does_not_turn_completed_update_into_reinstall(installation, monkeypatch):
    config, state, root, record, schedule, events = installation
    def command(argv, **kwargs):
        if "review" in argv:
            raise UpdateError("Window launch failed.")
        return ""
    monkeypatch.setattr(updating, "_command", command)
    result = updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert result["status"] == "updated" and result["window_launch_submitted"] is False
    assert not (state / "update-active.json").exists()
    assert events[-1] == ("schedule", True)


def test_active_investigation_is_not_stopped_or_updated(installation):
    config, state, root, record, schedule, events = installation
    save_private_json(state, Path("supervisor-jobs.json"), {"jobs": {"fixture": {"status": "running"}}})
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []
    assert load_private_json(state, Path("supervisor-jobs.json"))["jobs"]["fixture"]["status"] == "running"
    assert not (state / "update-active.json").exists()


def test_component_manifest_verifies_revision_and_every_file(tmp_path, monkeypatch):
    executable = tmp_path / "Watcher.exe"
    assembly = executable.with_suffix(".dll")
    executable.write_bytes(b"fixture executable, never run")
    assembly.write_bytes(b"fixture assembly, never loaded")
    monkeypatch.setattr(updating, "_component", lambda path:
        (executable, updating._hash(executable), updating._hash(assembly)))
    manifest = {"source_commit": "a" * 40, "platform": "windows-x64", "files": [
        {"path": path.name, "bytes": path.stat().st_size, "sha256": updating._hash(path)}
        for path in (executable, assembly)]}
    (tmp_path / "component-manifest.json").write_text(json.dumps(manifest))
    assert updating._verify_component(executable, "a" * 40)["path"] == str(executable)
    with pytest.raises(UpdateError, match="does not match"):
        updating._verify_component(executable, "b" * 40)
    assembly.write_bytes(b"changed")
    with pytest.raises(UpdateError, match="differs"):
        updating._verify_component(executable, "a" * 40)


def test_preview_does_not_stage_pause_close_or_install(tmp_path, monkeypatch):
    config = tmp_path / "profile.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    state = tmp_path / "state-not-created"
    identity = {"python": "/fixture/python", "command": "/fixture/command"}
    component = {"path": str(tmp_path / "new" / "Watcher.exe")}
    monkeypatch.setattr(updating, "_wsl_identity", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(updating.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(updating, "_installed_identity", lambda *args: identity)
    monkeypatch.setattr(updating, "_verify_source", lambda *args: {"__init__.py": "a" * 64})
    monkeypatch.setattr(updating, "_verify_component", lambda *args: component)
    monkeypatch.setattr(updating, "verify_native_component", lambda *args:
                        (tmp_path / "old" / "Watcher.exe", "b" * 64, "c" * 64))
    monkeypatch.setattr(updating, "matching_owners", lambda *args: [{"pid": 123}])
    class Schedule:
        def __init__(self, *args):
            pass
        def snapshot(self):
            return {"enabled": False}
        def set_enabled(self, *args):
            pytest.fail("Preview changed the schedule")
    monkeypatch.setattr(updating, "UpdateSchedule", Schedule)
    monkeypatch.setattr(updating, "_command", lambda *args, **kwargs: pytest.fail("Preview ran installer"))
    monkeypatch.setattr(updating, "close_owner", lambda *args: pytest.fail("Preview closed owner"))
    result = updating.update(config, state, source=tmp_path, revision="a" * 40,
        component=Path(component["path"]), installed_command=Path(identity["command"]))
    assert result["status"] == "preview" and result["owners_to_close"] == 1
    assert result["schedule"]["enabled"] is False
    assert not state.exists()


@pytest.mark.parametrize("phase", ["prepared", "recovery_required", "complete"])
@pytest.mark.parametrize("completed_before_admission", [False, True])
def test_saved_partial_install_can_finish_only_its_exact_session(installation, monkeypatch, phase, completed_before_admission):
    config, state, root, record, schedule, events = installation
    prefix = Path("/fixture/tools/dependabot-watcher")
    record["installed"] = {"prefix": str(prefix), "python": str(prefix / "bin/python"),
        "base_python": "/fixture/base/python", "command": str(prefix / "bin/dependabot-watcher"),
        "package": str(prefix / "lib/dependabot_watcher")}
    wheel = Path(record["wheel"])
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"controlled wheel boundary; never executed")
    record.update(phase=phase, wheel_sha256=updating._hash(wheel))
    record["schedule"] = None
    save_private_json(root, Path("receipt.json"), record)
    save_private_json(state, Path("update-active.json"), {"schema_version": 1, "session_id": record["session_id"]})
    config.write_text(config.read_text() + "# Operator edited valid settings while interrupted.\n")
    edited_settings = config.read_bytes()
    monkeypatch.setattr(updating.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(updating, "UpdateSchedule", lambda *args: schedule)
    def command(argv, **kwargs):
        if argv[1:] == ["tool", "dir"]:
            return str(prefix.parent)
        events.append(("command", tuple(argv)))
        return ""
    monkeypatch.setattr(updating, "_command", command)
    with pytest.raises(UpdateError, match="does not match"):
        updating.recover_update(config, state, "update_" + "c" * 32)
    assert events == []
    other = config.with_name("other-profile.toml")
    other.write_bytes(config.read_bytes())
    with pytest.raises(UpdateError, match="artifacts changed"):
        updating.recover_update(other, state, record["session_id"])
    assert events == []
    if completed_before_admission:
        original_lock = updating.run_lock
        @contextmanager
        def intervening_completion(path, **kwargs):
            with original_lock(path, **kwargs) as held:
                if path == state / "updates":
                    # Another invocation completed between this reader's
                    # optimistic observation and lock admission.
                    (state / "update-active.json").unlink()
                    save_private_json(root, Path("receipt.json"), {**record, "phase": "complete"})
                yield held
        monkeypatch.setattr(updating, "run_lock", intervening_completion)
        with pytest.raises(UpdateError, match="no longer has an active recovery fence"):
            updating.recover_update(config, state, record["session_id"])
        assert events == []
        assert load_private_json(root, Path("receipt.json"))["phase"] == "complete"
        return
    result = updating.recover_update(config, state, record["session_id"])
    assert result["status"] == "updated"
    assert not (state / "update-active.json").exists()
    assert config.read_bytes() == edited_settings
    assert sum(event[0] == "command" and "install" in event[1] for event in events) == (0 if phase == "complete" else 1)


@pytest.mark.parametrize("tail", [[], ["--scheduled-run", "run_" + "a" * 20],
    ["--scheduled-run", "run_" + "a" * 20, "--notify-mode", "off"]])
def test_owner_discovery_includes_background_started_window(tmp_path, monkeypatch, tail):
    config = tmp_path / "profile.toml"
    state = tmp_path / "state"
    python = Path("/fixture/bin/python")
    component = {"path": "C:\\Fixture\\WatcherNativeProof.exe", "sha256": "a" * 64, "assembly_sha256": "b" * 64}
    launch = {"schema_version": 3, "diagnostic_transport": False, "component": component,
              "arguments": [str(python), "-I", "-m", "dependabot_watcher.review_bridge", "--config", str(config), "--state-dir", str(state)]}
    identity = hashlib.sha256((json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    profile = state / "native-review" / "profiles" / identity
    save_private_json(profile, Path("backend-launch.json"), launch)
    monkeypatch.setattr(update_coordination, "_wsl_identity", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(update_coordination, "_unc_path", lambda *args: "fixture-launch-file")
    argv = [component["path"], "--launch-file", "fixture-launch-file", *tail]
    monkeypatch.setattr(update_coordination, "_split_windows_command_line", lambda text: argv)
    owner = {"pid": 123, "path": component["path"], "command_line": "fixture-command", "started": "123"}
    def windows(script, payload):
        return {"owners": [owner]} if "Win32_Process" in script else {key: component[key] for key in ("sha256", "assembly_sha256")}
    monkeypatch.setattr(update_coordination, "_windows", windows)
    assert update_coordination.matching_owners(config, state, python) == [{**owner,
        **{key: component[key] for key in ("sha256", "assembly_sha256")},
        "profile": str(profile), "profile_sha256": identity, "launch_file": "fixture-launch-file"}]


def test_runtime_prompt_data_is_verified_not_only_python(tmp_path, monkeypatch):
    package = tmp_path / "dependabot_watcher"
    prompts = package / "prompts"
    prompts.mkdir(parents=True)
    (package / "__init__.py").write_text("# fixture\n")
    prompt = prompts / "analysis.md"
    prompt.write_text("Original reviewed prompt.\n")
    expected = {path.relative_to(package).as_posix(): updating._hash(path) for path in package.rglob("*") if path.is_file()}
    monkeypatch.setattr(updating, "_command", lambda *args, **kwargs: str(package))
    updating._verify_installed("/fixture/python", expected)
    prompt.write_text("Altered runtime instructions.\n")
    with pytest.raises(UpdateError, match="runtime prompt data"):
        updating._verify_installed("/fixture/python", expected)


def test_matching_sources_do_not_override_changed_runtime_identity(installation, monkeypatch):
    config, state, root, record, schedule, events = installation
    monkeypatch.setattr(updating, "_installed_identity", lambda *args: {**record["installed"], "prefix": "/foreign/runtime"})
    with pytest.raises(UpdateError, match="runtime identity changed"):
        updating._verify_runtime(record["installed"], "/fixture/uv", record["source_files"])
    assert events == []


def test_launcher_not_yet_published_as_running_also_blocks_replacement(installation):
    config, state, root, record, schedule, events = installation
    key = hashlib.sha256(b"example-org/example-app").hexdigest()
    with run_lock(state / "supervisor-launches" / key):
        with pytest.raises(UpdateError, match="launcher, collection or notification is active"):
            updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []
    assert not (state / "update-active.json").exists()


def test_bad_update_path_is_a_clear_refusal_not_an_internal_error(monkeypatch, capsys):
    def missing(*args, **kwargs):
        raise FileNotFoundError("Selected source is missing")
    monkeypatch.setattr(cli, "update", missing)
    result = cli.main(["update", "--source", "/missing/source", "--source-commit", "a" * 40,
                       "--native-component", "/missing/Watcher.exe", "--installed-command", "/missing/command", "--plain"])
    assert result == cli.ExitCode.EXECUTION_ERROR
    assert "Update" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["install", "disable", "remove"])
def test_schedule_writes_cannot_bypass_active_update(tmp_path, monkeypatch, action):
    state = tmp_path / "state"
    save_private_json(state, Path("update-active.json"), {"schema_version": 1, "session_id": "update_" + "a" * 32})
    class Adapter:
        def __getattr__(self, name):
            return lambda spec: pytest.fail("Native schedule mutation bypassed update fence")
    monkeypatch.setattr(scheduling, "scheduler_adapter", lambda *args, **kwargs: Adapter())
    monkeypatch.setattr(scheduling, "detect_schedule_platform", lambda requested: "systemd")
    with pytest.raises(ScheduleError, match="Schedule change was not applied"):
        scheduling.manage_schedule(action, config_path=tmp_path / "profile.toml", state_directory=state,
            interval_minutes=60, requested_platform="systemd", executable="/usr/bin/true")


def test_candidate_state_probe_rejects_incompatible_history_without_rewriting_it(tmp_path):
    config = tmp_path / "profile.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    state = tmp_path / "state"
    save_private_json(state, Path("run-state.json"), {"schema_version": 999, "retained_history": ["keep"]})
    before = (state / "run-state.json").read_bytes()
    with pytest.raises(UpdateError, match="did not succeed"):
        updating._probe_state(sys.executable, config, state)
    assert (state / "run-state.json").read_bytes() == before


def test_state_lock_rechecks_update_fence_after_entry_precheck(tmp_path):
    state = tmp_path / "state"
    updating.require_no_update(state)  # A request was admitted before replacement.
    save_private_json(state, Path("update-active.json"), {"schema_version": 1, "session_id": "update_" + "a" * 32})
    with pytest.raises(updating.RuntimeStateError, match="recovery"):
        with run_lock(state):
            pytest.fail("Previously admitted request committed across update fence")
    with run_lock(state, allow_update=True):
        assert (state / "update-active.json").exists()


def test_snapshot_includes_referenced_plan_and_runtime_metadata(tmp_path):
    state = tmp_path / "state"
    for relative in ("plans/plan_fixture.json", "supervisor-runtime/job_fixture.json", "latest-run.json"):
        save_private_json(state, Path(relative), {"retained": True})
    before = updating._state_snapshot(state)
    assert all(name in before for name in ("plans/plan_fixture.json", "supervisor-runtime/job_fixture.json", "latest-run.json"))
    save_private_json(state, Path("plans/plan_fixture.json"), {"retained": "changed"})
    assert before != updating._state_snapshot(state)


def test_hidden_owner_reactivation_requires_unchanged_private_profile(tmp_path, monkeypatch):
    launch = {"component": {"path": "C:\\Fixture\\Watcher.exe", "sha256": "a" * 64, "assembly_sha256": "b" * 64}}
    profile = tmp_path / ("c" * 64)
    save_private_json(profile, Path("backend-launch.json"), launch)
    monkeypatch.setattr(update_coordination, "_wsl_identity", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(update_coordination, "_windows", lambda *args: pytest.fail("Changed profile activated"))
    with pytest.raises(UpdateError, match="profile changed"):
        update_coordination.close_owner({"profile": str(profile), "profile_sha256": profile.name,
            "launch_file": "changed", **launch["component"]})


@pytest.mark.parametrize("change", ["new_task", "command", "definition"])
def test_schedule_revalidates_inventory_and_original_definition(tmp_path, monkeypatch, change):
    config, state, command = tmp_path / "profile.toml", tmp_path / "state", tmp_path / "bin/watcher"
    job_id = update_coordination.schedule_job_id(config)
    observed = {"job_id": job_id, "config_path": str(config), "state_directory": str(state),
        "distro": "fixture", "drifted": False, "interval_minutes": 60, "notification_mode": "off",
        "command": ["fixture"] * 8 + [str(command)]}
    xml = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><Enabled>true</Enabled></Settings><Actions><Exec><Command>original</Command></Exec></Actions></Task>'
    items = [] if change == "new_task" else [observed]
    class Adapter:
        def __init__(self, **kwargs): pass
        def inventory(self): return {"untrusted_candidates": [], "registrations": items}
        def _native_status(self, spec): return {"installed": True, "enabled": True, "state": "Ready", "xml": xml}
        def _management_definition(self, *args): pass
        def _user(self): return "fixture-sid"
        def _run_script(self, *args): pytest.fail("Drifted schedule was mutated")
    monkeypatch.setattr(update_coordination, "_wsl_identity", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(update_coordination, "WindowsTaskSchedulerAdapter", Adapter)
    # A real spec is immutable/value-comparable; keep that contract in the fixture.
    spec = type("Spec", (), {"job_id": job_id})()
    monkeypatch.setattr(update_coordination, "schedule_spec", lambda **kwargs: spec)
    schedule = update_coordination.UpdateSchedule(config, state, command)
    saved = schedule.snapshot()
    if change == "new_task": items.append(observed)
    elif change == "command": observed["command"][8] = str(tmp_path / "foreign")
    else: xml = xml.replace("original", "foreign")
    with pytest.raises(UpdateError, match="changed|differs"):
        schedule.set_enabled(saved, False)


def test_inflight_collection_lease_blocks_replacement_before_any_interruption(installation):
    config, state, root, record, schedule, events = installation
    with run_lock(state / "reconciliation"):
        with pytest.raises(UpdateError, match="collection or notification is active"):
            updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []
    assert not (state / "update-active.json").exists()


@pytest.mark.parametrize("journal,field,status", [
    ("review-actions.json", "reviews", "executing"),
    ("local-sync.json", "records", "recovery_required"),
    ("branch-refresh-reviews.json", "reviews", "awaiting_update")])
def test_uncertain_repository_action_refuses_update_without_replaying_it(installation, journal, field, status):
    config, state, root, record, schedule, events = installation
    save_private_json(state, Path(journal), {field: {"fixture": {"status": status}}})
    before = (state / journal).read_bytes()
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == [] and (state / journal).read_bytes() == before


def _saved_trigger(state, *, phase="recovery_required"):
    run_id = "run_" + "a" * 20
    selected = {"repository": "example-org/example-app", "context_digest": "a" * 64}
    selection = {"schema_version": 1, "mode": "scheduled_supervisor_plan", "run_id": run_id,
                 "status": "ready", "max_jobs_per_run": 1, "repositories": [selected], "skipped": [],
                 "capabilities": {"model_calls": False, "github_mutations": False}}
    path = Path("scheduled-supervisor") / f"{run_id}.json"
    value = {"schema_version": 1, "run_id": run_id, "configuration_digest": "d" * 64,
             "selection": selection, "selection_digest": supervisor_context_digest(selection),
             "slots": [{**selected, "phase": phase, "job_id": None}]}
    save_private_json(state, path, value)
    return path


def test_unresolved_scheduled_slot_blocks_replacement(installation):
    config, state, root, record, schedule, events = installation
    _saved_trigger(state, phase="started")
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []


@pytest.mark.parametrize("job_status", [None, "failed", "interrupted", "completed", "superseded"])
def test_consumed_failed_schedule_history_does_not_block_update_or_change_receipts(installation, job_status):
    config, state, root, record, schedule, events = installation
    # No exact job/digest match is required: launch can fail before job creation,
    # and fresh collection can change context before a job is prepared.
    path = _saved_trigger(state)
    if job_status is not None:
        save_private_json(state, Path("supervisor-jobs.json"), {"jobs": {
            "job_" + "b" * 32: {"status": job_status, "context_digest": "c" * 64}}})
    record["state_snapshot"] = updating._state_snapshot(state)
    before = (state / path).read_bytes()
    result = updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert result["status"] == "updated"
    assert (state / path).read_bytes() == before
    assert events[0:2] == [("schedule", False), ("close", 123)]
    assert events[-2] == ("schedule", True)


@pytest.mark.parametrize("status", ["running", None, "unknown", {}, []])
def test_failed_schedule_receipt_does_not_hide_active_or_unknown_job(installation, status):
    config, state, root, record, schedule, events = installation
    _saved_trigger(state)
    save_private_json(state, Path("supervisor-jobs.json"), {"jobs": {"fixture": {"status": status}}})
    record["state_snapshot"] = updating._state_snapshot(state)
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []


@pytest.mark.parametrize("phase", ["started", None, "unknown", {}, []])
def test_malformed_or_incomplete_schedule_slot_still_blocks_update(installation, phase):
    config, state, root, record, schedule, events = installation
    _saved_trigger(state, phase=phase)
    record["state_snapshot"] = updating._state_snapshot(state)
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []


def test_failed_schedule_receipt_does_not_bypass_active_launch_lease(installation):
    config, state, root, record, schedule, events = installation
    _saved_trigger(state)
    record["state_snapshot"] = updating._state_snapshot(state)
    key = hashlib.sha256(b"example-org/example-app").hexdigest()
    with run_lock(state / "supervisor-launches" / key):
        with pytest.raises(UpdateError, match="launcher, collection or notification is active"):
            updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []


def test_candidate_reader_rejects_malformed_old_configuration_trigger(tmp_path):
    config = tmp_path / "profile.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    state = tmp_path / "state"
    path = Path("scheduled-supervisor/run_" + "a" * 20 + ".json")
    save_private_json(state, path, {"configuration_digest": "d" * 64,
                                  "slots": [{"phase": "recovery_required"}]})
    before = (state / path).read_bytes()
    with pytest.raises(UpdateError, match="command did not succeed"):
        updating._probe_state(sys.executable, config, state)
    with pytest.raises(UpdateError, match="history is malformed"):
        updating._require_idle(state)
    assert (state / path).read_bytes() == before


def test_candidate_reader_retains_valid_old_configuration_trigger(tmp_path):
    config = tmp_path / "profile.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    state = tmp_path / "state"
    path = _saved_trigger(state)
    before = (state / path).read_bytes()
    updating._require_idle(state)
    snapshot = updating._probe_state(sys.executable, config, state)
    assert snapshot[path.as_posix()] == updating._hash(state / path)
    assert (state / path).read_bytes() == before


def test_interrupted_execution_audit_refuses_replacement(installation, monkeypatch):
    config, state, root, record, schedule, events = installation
    monkeypatch.setattr(execution, "_read_audit_events", lambda unused: [
        {"action_id": "fixture", "event_type": "execution_started"}])
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []


def test_compatibility_probe_refuses_malformed_execution_audit(tmp_path):
    config = tmp_path / "profile.toml"
    config.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n')
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    audit = state / "execution-audit.jsonl"
    audit.write_text('not JSON\n')
    audit.chmod(0o600)
    with pytest.raises(ExecutionError, match="malformed JSON"):
        updating._probe_state(sys.executable, config, state)
    assert audit.read_text() == 'not JSON\n'


@pytest.mark.parametrize("phase,attempted", [("recovery_required", False), ("installing_backend", False), ("closing_owned_windows", True)])
def test_recovery_preinstall_refusal_cannot_clear_prior_replacement_fence(installation, phase, attempted):
    config, state, root, record, schedule, events = installation
    record["phase"] = phase
    if attempted:
        record["backend_replacement_attempted"] = True
    record["configuration_sha256"] = "0" * 64  # Refuse before this retry installs.
    save_private_json(state, Path("update-active.json"), {"schema_version": 1, "session_id": record["session_id"]})
    with pytest.raises(UpdateError, match="needs recovery"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []
    assert (state / "update-active.json").exists()
    receipt = load_private_json(root, Path("receipt.json"))
    assert receipt["phase"] == "recovery_required" and receipt["backend_replacement_attempted"] is True


def test_running_owner_with_missing_selected_profile_is_not_silently_ignored(tmp_path, monkeypatch):
    state = tmp_path / "state"
    launch_file = update_coordination._unc_path(state / "native-review/profiles" / ("a" * 64) / "backend-launch.json", "fixture")
    owner = {"pid": 123, "path": "C:\\Fixture\\WatcherNativeProof.exe", "command_line": "fixture", "started": "123"}
    monkeypatch.setattr(update_coordination, "_wsl_identity", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(update_coordination, "_windows", lambda *args: {"owners": [owner]})
    monkeypatch.setattr(update_coordination, "_split_windows_command_line", lambda unused: [owner["path"], "--launch-file", launch_file])
    with pytest.raises(UpdateError, match="unverified profile"):
        update_coordination.matching_owners(tmp_path / "profile.toml", state, Path("/fixture/python"))


@pytest.mark.parametrize("root", ["scheduled-supervisor", "supervisor-validation"])
def test_retained_directory_replaced_by_file_blocks_compatibility(installation, root):
    config, state, receipt_root, record, schedule, events = installation
    state.mkdir(mode=0o700)
    (state / root).write_text("not a directory")
    with pytest.raises(UpdateError, match="invalid file type"):
        updating._state_snapshot(state)
    record.pop("state_snapshot")  # Even this internal path still checks root types.
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, receipt_root, record, schedule, "/fixture/uv")
    assert events == []


def test_complete_receipt_survives_fence_cleanup_failure_without_reinstall_claim(installation, monkeypatch):
    config, state, root, record, schedule, events = installation
    def failed_cleanup(*args, **kwargs):
        raise updating.RuntimeStateError("Injected marker cleanup failure")
    monkeypatch.setattr(updating, "delete_private_artifact", failed_cleanup)
    with pytest.raises(UpdateError, match="verified complete.*fence cleanup"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert load_private_json(root, Path("receipt.json"))["phase"] == "complete"
    assert (state / "update-active.json").exists()
    assert sum(event[0] == "command" and "install" in event[1] for event in events) == 1


def test_notification_delivery_lease_blocks_update_but_not_backend_reads(installation):
    config, state, root, record, schedule, events = installation
    with run_lock(state / "native-notifications"):
        with run_lock(state):
            pass  # The child may acquire the root journal lock for attention.
        with pytest.raises(UpdateError, match="notification is active"):
            updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []


def test_new_component_selection_during_staging_is_not_overwritten(installation):
    config, state, root, record, schedule, events = installation
    record["previous_component"] = {"path": "/fixture/previous/Watcher.exe", "sha256": "1" * 64, "assembly_sha256": "2" * 64}
    with pytest.raises(UpdateError, match="stopped before installation"):
        updating._complete(config, state, root, record, schedule, "/fixture/uv")
    assert events == []  # No pause, owner closure, installation or registration.
    assert not (state / "update-active.json").exists()


@pytest.mark.parametrize("missing", ["state_snapshot", "previous_component", "schedule"])
def test_missing_required_recovery_baselines_refuse_before_any_action(installation, missing):
    config, state, root, record, schedule, events = installation
    record.pop(missing)
    save_private_json(root, Path("receipt.json"), record)
    save_private_json(state, Path("update-active.json"), {"schema_version": 1, "session_id": record["session_id"]})
    with pytest.raises(UpdateError, match="lacks required safety baselines"):
        updating.recover_update(config, state, record["session_id"])
    assert events == []
    assert (state / "update-active.json").exists()
