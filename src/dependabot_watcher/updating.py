"""Coordinated, explicit WSL/uv source update with an optional Windows UI.

The caller supplies independently acquired pinned source and a verified matching
component directory. Preparation precedes interruption. No repository action,
model call, task recreation or process kill is an update operation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import load_config
from .native_review import _component, _prepare_native_review, _wsl_identity, verify_native_component
from .runtime_state import RuntimeStateError, delete_private_artifact, ensure_private_directory, load_private_json, run_lock, save_private_json
from .supervisor_workspace import _git, _github_repository
from .update_coordination import UpdateError, UpdateSchedule, close_owner, matching_owners


_ACTIVE = Path("update-active.json")
_SESSION = re.compile(r"update_[a-f0-9]{32}")
_REVISION = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
_JOURNALS = ("run-state.json", "review-queue.json", "supervisor-jobs.json", "review-actions.json", "local-sync.json", "branch-refresh-reviews.json")
_RETAINED_DIRECTORIES = ("plans", "supervisor-observations", "supervisor-runtime", "supervisor-failures", "scheduled-supervisor")


def require_no_update(state: Path) -> None:
    """Read-only startup fence, including an interrupted update awaiting recovery."""
    if os.path.lexists(state / _ACTIVE):
        active = load_private_json(state, _ACTIVE, maximum_bytes=8192, read_only=True)
        if (set(active) != {"schema_version", "session_id"} or active.get("schema_version") != 1
                or not isinstance(active.get("session_id"), str) or not _SESSION.fullmatch(active["session_id"])):
            raise UpdateError("Update ownership state is invalid; preserve it for recovery.")
        raise UpdateError(f"Watcher update {active['session_id']} needs completion or recovery before new work. No action was started.")


def _hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise UpdateError("An update file is unavailable or uses a link.")
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise UpdateError("An update file changed during verification.")
    return digest


def _command(arguments: list[str], *, timeout: int = 600) -> str:
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("An update command could not complete. Check its prerequisites and saved phase; private installer diagnostics were withheld.") from exc
    if result.returncode != 0 or len(result.stdout.encode()) > 1_000_000:
        raise UpdateError("An update command did not succeed. Private installer diagnostics were withheld; inspect the saved update phase.")
    return result.stdout.strip()


def _installed_identity(command: Path, uv: str) -> dict[str, str]:
    executable = command.resolve(strict=True)
    python = executable.parent / "python"
    query = ("import sys,json,importlib.metadata as m; d=m.distribution('dependabot-watcher'); "
             "print(json.dumps({'prefix':sys.prefix,'python':sys.executable,'base_python':sys._base_executable,"
             "'package':str(d.locate_file('dependabot_watcher'))}))")
    identity = json.loads(_command([str(python), "-I", "-c", query], timeout=30))
    tools = Path(_command([uv, "tool", "dir"], timeout=30))
    if (not isinstance(identity, dict) or set(identity) != {"prefix", "python", "base_python", "package"}
            or any(not isinstance(value, str) or not Path(value).is_absolute() for value in identity.values())
            or Path(identity["prefix"]).resolve() != (tools / "dependabot-watcher").resolve()
            or Path(identity["prefix"]).resolve() != Path(identity["prefix"])
            or Path(identity["python"]).absolute() != python.absolute()
            or not Path(identity["package"]).resolve().is_relative_to(Path(identity["prefix"]).resolve())
            or Path(identity["package"]).resolve() != Path(identity["package"])
            or not Path(identity["base_python"]).is_file()):
        raise UpdateError("The selected command is not the verified uv-owned installation. No installer was selected implicitly.")
    return {**identity, "command": str(executable)}


def _verify_source(source: Path, revision: str) -> dict[str, str]:
    if not _REVISION.fullmatch(revision) or source.resolve(strict=True) != source:
        raise UpdateError("Select an exact reviewed commit and its real clean acquired source path.")
    if (_github_repository(_git(source, "remote", "get-url", "origin")).casefold() != "alek2077/dependabot-watcher"
            or _git(source, "rev-parse", "HEAD").lower() != revision):
        raise UpdateError("The acquired source identity does not match the selected Watcher revision.")
    filters = _git(source, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$", empty_ok=True)
    if filters:
        raise UpdateError("Acquire an ordinary clean source checkout without custom Git filters for this update.")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all"):
        raise UpdateError("The update source has local changes; no development checkout substitution was accepted.")
    package = source / "src" / "dependabot_watcher"
    sources = sorted(path for path in package.rglob("*") if path.is_file()
        and (path.suffix == ".py" or path.parent == package / "prompts" and path.suffix in {".md", ".json"}))
    if not sources or len(sources) > 1000:
        raise UpdateError("The source package is missing or exceeds its verification bound.")
    return {path.relative_to(package).as_posix(): _hash(path) for path in sources}


def _verify_component(component: Path, revision: str) -> dict[str, str]:
    executable, launcher, assembly = _component(component)
    root = executable.parent
    manifest_path = root / "component-manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 4_000_000:
        raise UpdateError("A bounded extracted component manifest is required.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("source_commit") != revision
            or manifest.get("platform") != "windows-x64" or not isinstance(manifest.get("files"), list)
            or not 1 <= len(manifest["files"]) <= 20_000):
        raise UpdateError("The Windows package does not match the pinned backend revision.")
    seen: set[str] = set()
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}
                or not isinstance(entry.get("path"), str) or not entry["path"]
                or "\\" in entry["path"] or ":" in entry["path"]
                or Path(entry["path"]).is_absolute() or any(part in {".", ".."} for part in entry["path"].split("/"))
                or type(entry.get("bytes")) is not int or not 0 <= entry["bytes"] <= 1_000_000_000
                or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or entry["path"].casefold() in seen):
            raise UpdateError("The extracted component manifest has an invalid file record.")
        path = root / entry["path"]
        if path.resolve(strict=True) != path or path.stat().st_size != entry["bytes"] or _hash(path) != entry["sha256"]:
            raise UpdateError("An extracted component file differs from its manifest.")
        seen.add(entry["path"].casefold())
    actual = set()
    for count, path in enumerate(root.rglob("*"), 1):
        if count > 25_000:
            raise UpdateError("The component directory exceeds the inspection bound.")
        if path.is_symlink():
            raise UpdateError("The component directory contains a link.")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix().casefold())
    if actual != seen:
        raise UpdateError("The extracted component has unlisted or missing files.")
    return {"path": str(executable), "sha256": launcher, "assembly_sha256": assembly,
            "manifest_sha256": _hash(manifest_path)}


def _verify_installed(python: str, expected: dict[str, str]) -> None:
    query = "import pathlib,dependabot_watcher; print(pathlib.Path(dependabot_watcher.__file__).parent)"
    package = Path(_command([python, "-I", "-c", query], timeout=30))
    if not package.is_absolute():
        raise UpdateError("Installed package identity is unavailable.")
    actual = {path.relative_to(package).as_posix(): _hash(path) for path in package.rglob("*") if path.is_file()
        and (path.suffix == ".py" or path.parent == package / "prompts" and path.suffix in {".md", ".json"})}
    if actual != expected:
        raise UpdateError("Installed package code or runtime prompt data do not match the selected reviewed revision.")


def _verify_runtime(identity: dict[str, str], uv: str, expected: dict[str, str]) -> None:
    if _installed_identity(Path(identity["command"]), uv) != identity:
        raise UpdateError("The updated installed runtime identity changed; recovery is required.")
    _verify_installed(identity["python"], expected)


def _state_snapshot(state: Path) -> dict[str, str | None]:
    names: set[str] = set(_JOURNALS)
    names.add("execution-audit.jsonl")
    # Retained operational metadata only, never caches, worktrees, update
    # wheels or provider transcripts. Refuse an oversized copy, don't truncate.
    if state.exists():
        names.update(path.name for path in state.glob("*.json") if path.name != _ACTIVE.name)
    for name in _RETAINED_DIRECTORIES:
        directory = state / name
        if directory.is_symlink():
            raise UpdateError("Retained update history uses a directory link.")
        if directory.exists() and not directory.is_dir():
            raise UpdateError("A retained history directory has an invalid file type.")
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise UpdateError("Retained update history uses a link.")
                if path.is_file():
                    names.add(path.relative_to(state).as_posix())
                if len(names) > 5000:
                    raise UpdateError("Retained history exceeds the bounded update compatibility check.")
    resources = state / "supervisor-validation"
    if resources.is_symlink():
        raise UpdateError("Retained validation metadata uses a link.")
    if resources.exists() and not resources.is_dir():
        raise UpdateError("A retained validation directory has an invalid file type.")
    for path in resources.glob("*/resource-blocker.json"):
        if path.parent.is_symlink():
            raise UpdateError("Retained validation metadata uses a link.")
        names.add(path.relative_to(state).as_posix())
    if len(names) > 5000:
        raise UpdateError("Retained history exceeds the bounded update compatibility check.")
    snapshot = {name: _hash(state / name) if os.path.lexists(state / name) else None for name in sorted(names)}
    if sum((state / name).stat().st_size for name, digest in snapshot.items() if digest is not None) > 100_000_000:
        raise UpdateError("Retained history exceeds the bounded update compatibility check.")
    return snapshot


def _probe_state(python: str, config_path: Path, state: Path) -> dict[str, str | None]:
    """Candidate readers inspect disposable copies, never migrate user state."""
    before = _state_snapshot(state)
    with tempfile.TemporaryDirectory(prefix="watcher-update-state-") as scratch:
        copy = ensure_private_directory(Path(scratch) / "state")
        for name, digest in before.items():
            if digest is not None:
                if name == "execution-audit.jsonl":
                    from .execution import validate_audit_history
                    validate_audit_history(state)
                    shutil.copyfile(state / name, copy / name)
                    (copy / name).chmod(0o600)
                    continue
                value = load_private_json(state, Path(name), maximum_bytes=10_000_000, read_only=True)
                save_private_json(copy, Path(name), value, maximum_bytes=10_000_000)
        code = (
            "import sys; from pathlib import Path; from datetime import datetime,timezone; "
            "from dependabot_watcher.config import load_config; "
            "from dependabot_watcher.runtime_state import load_run_state,load_private_json; "
            "from dependabot_watcher.review_queue import ReviewQueue; "
            "from dependabot_watcher.supervisor_status import supervision_status; "
            "from dependabot_watcher.supervisor_permissions import SupervisorPermissions; "
            "from dependabot_watcher.execution import validate_audit_history; "
            "from dependabot_watcher.scheduled_triggers import _load as load_trigger,_policy as trigger_policy; "
            "from dependabot_watcher.supervision import SupervisorJobs; "
            "from dependabot_watcher.review_actions import _load_reviews; "
            "from dependabot_watcher.local_sync import _journal; "
            "from dependabot_watcher.branch_refresh import _load; "
            "p=Path(sys.argv[2]); c=load_config(Path(sys.argv[1])); "
            "q=ReviewQueue(c,p,config_path=Path(sys.argv[1])); load_run_state(p); validate_audit_history(p); "
            "q.handle({'schema_version':1,'request_id':'read_update_probe','operation':'snapshot'}); "
            "SupervisorJobs(q)._load(read_only=True); SupervisorPermissions(SupervisorJobs(q))._load(); "
            "_load_reviews(p,datetime.now(timezone.utc)); _journal(p,read_only=True); _load(p); "
            "[supervision_status(q,{'schema_version':1,'request_id':'read_update_status','operation':'supervisor_status',"
            "'repository':r.name}) for r in c.repositories]; "
            "[load_trigger(q,f.stem) for f in (p/'scheduled-supervisor').glob('*.json') "
            "if load_private_json(p,f.relative_to(p))['configuration_digest']==trigger_policy(q)]"
        )
        _command([python, "-I", "-c", code, str(config_path), str(copy)], timeout=45)
    if before != _state_snapshot(state):
        raise UpdateError("Saved state changed during compatibility verification; restage the update without replacing active work.")
    return before


def update(config_path: Path, state: Path, *, source: Path, revision: str,
           component: Path, installed_command: Path, apply: bool = False) -> dict[str, Any]:
    """Verify/stage, then close, replace, rebind and restore one installation."""
    _wsl_identity()
    config_path, state, source = (path.expanduser().absolute() for path in (config_path, state, source))
    load_config(config_path)
    require_no_update(state)
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("This coordinated update path requires the existing uv installer; other installers were not substituted.")
    identity = _installed_identity(installed_command, uv)
    expected = _verify_source(source, revision)
    selected_component = _verify_component(component, revision)
    # Refuse in-place replacement of a selected Windows runtime directory.
    previous_component, _launcher, _assembly = verify_native_component(state)
    if previous_component.parent == Path(selected_component["path"]).parent:
        raise UpdateError("Stage the Windows package in a distinct directory; do not overwrite the running component.")
    schedule = UpdateSchedule(config_path, state, Path(identity["command"]))
    saved_schedule = schedule.snapshot()
    owners = matching_owners(config_path, state, Path(identity["python"]))
    preview = {"schema_version": 1, "status": "preview", "source_commit": revision,
               "owners_to_close": len(owners), "schedule": saved_schedule,
               "component": selected_component, "installed_command": identity["command"],
               "effects": "Stage first; gracefully exit verified owners; update uv backend and select matching Windows component; preserve settings/history and existing task definition/enabled state.",
               "model_calls": False, "github_mutations": False}
    if not apply:
        return preview
    session = "update_" + uuid.uuid4().hex
    updates = ensure_private_directory(state / "updates")
    with run_lock(updates):
        require_no_update(state)
        root = ensure_private_directory(updates / session)
        # Resolve/build/installability before disturbing the app or schedule.
        _command([uv, "build", "--wheel", "--no-build-logs", "--python", identity["base_python"],
                  "--out-dir", str(root / "dist"), str(source)])
        wheels = list((root / "dist").glob("dependabot_watcher-*.whl"))
        if len(wheels) != 1:
            raise UpdateError("Preparation did not produce one Watcher wheel.")
        wheel = wheels[0]
        # Probe dependencies use uv's shared cache, not a retained per-update
        # environment. Only the small wheel and recovery receipt are retained.
        with tempfile.TemporaryDirectory(prefix="watcher-update-probe-") as probe:
            _command([uv, "venv", "--python", identity["base_python"], probe])
            probe_python = Path(probe) / "bin" / "python"
            _command([uv, "pip", "install", "--python", str(probe_python), str(wheel)])
            _verify_installed(str(probe_python), expected)
            with run_lock(state):
                _require_idle(state)
                state_snapshot = _probe_state(str(probe_python), config_path, state)
        if _verify_source(source, revision) != expected or _verify_component(component, revision) != selected_component:
            raise UpdateError("Staged deliverables changed during preparation; the running installation was not touched.")
        if schedule.snapshot() != saved_schedule:
            raise UpdateError("The original schedule changed during preparation; restage without replacing active work.")
        record = {"schema_version": 1, "session_id": session, "phase": "prepared", "source_commit": revision,
                  "config_path": str(config_path.resolve(strict=True)),
                  "configuration_sha256": _hash(config_path), "installed": identity,
                  "source_files": expected, "component": selected_component, "schedule": saved_schedule,
                  "state_snapshot": state_snapshot,
                  "previous_component": {"path": str(previous_component), "sha256": _launcher, "assembly_sha256": _assembly},
                  "wheel": str(wheel), "wheel_sha256": _hash(wheel)}
        save_private_json(root, Path("receipt.json"), record, maximum_bytes=1_000_000)
        return _complete(config_path, state, root, record, schedule, uv)


def _require_idle(state: Path) -> None:
    jobs_path = Path("supervisor-jobs.json")
    if os.path.lexists(state / jobs_path):
        journal = load_private_json(state, jobs_path, maximum_bytes=10_000_000, read_only=True)
        jobs = journal.get("jobs")
        if not isinstance(jobs, dict) or any(not isinstance(job, dict) or job.get("status") == "running" for job in jobs.values()):
            raise UpdateError("An investigation is recorded as running or its status is unavailable. Check saved agent status/recovery; let active work finish before updating. No investigation was stopped.")
    for name, field, pending in (
            ("review-actions.json", "reviews", {"executing", "recovery_required"}),
            ("local-sync.json", "records", {"executing", "recovery_required"}),
            ("branch-refresh-reviews.json", "reviews", {"awaiting_update", "executing", "recovery_required"})):
        if os.path.lexists(state / name):
            records = load_private_json(state, Path(name), maximum_bytes=10_000_000, read_only=True).get(field)
            if not isinstance(records, dict) or any(not isinstance(item, dict) or item.get("status") in pending for item in records.values()):
                raise UpdateError("A saved repository action needs recovery before replacement. Resolve its exact outcome; no action was replayed or stopped.")
    for path in (state / "scheduled-supervisor").glob("*.json"):
        receipt = load_private_json(state, path.relative_to(state), maximum_bytes=65_536, read_only=True)
        slots = receipt.get("slots")
        if not isinstance(slots, list) or any(not isinstance(item, dict) or item.get("phase") in {"started", "recovery_required"} for item in slots):
            raise UpdateError("A scheduled investigation has an unresolved launch receipt; recover it before updating.")
    from .execution import _read_audit_events
    latest = {event["action_id"]: event for event in _read_audit_events(state)}
    if any(event["event_type"] in {"execution_started", "execution_failed"} for event in latest.values()):
        raise UpdateError("An execution audit requires exact-action recovery before updating; no action was replayed.")


@contextmanager
def _update_locks(config_path: Path, state: Path) -> Iterator[None]:
    """Match launch → collection → state order; refuse rather than drain work."""
    with ExitStack() as leases:
        try:
            for repository in load_config(config_path).repositories:
                key = hashlib.sha256(repository.name.casefold().encode()).hexdigest()
                leases.enter_context(run_lock(state / "supervisor-launches" / key))
            leases.enter_context(run_lock(state / "reconciliation"))
            leases.enter_context(run_lock(state / "native-notifications"))
            leases.enter_context(run_lock(state, allow_update=True))
        except RuntimeStateError as exc:
            raise UpdateError("A Watcher launcher, collection or notification is active. Let it finish before updating; no installation was attempted.") from exc
        yield


def _complete(config_path: Path, state: Path, root: Path, record: dict[str, Any], schedule: UpdateSchedule, uv: str) -> dict[str, Any]:
    def save(phase: str) -> None:
        save_private_json(root, Path("receipt.json"), {**record, "phase": phase}, maximum_bytes=1_000_000)
        record["phase"] = phase
    replacement_attempted = record.get("backend_replacement_attempted", False) or record["phase"] not in {
        "prepared", "cancelled_before_installation", "pausing_existing_schedule", "closing_owned_windows"}
    # Monotonic across retries: rewriting a transient phase must never erase
    # the fact that an earlier invocation could have replaced the backend.
    record["backend_replacement_attempted"] = replacement_attempted
    verified = False
    paused = False
    identity = record["installed"]
    with _update_locks(config_path, state):
        if os.path.lexists(state / _ACTIVE):
            active = load_private_json(state, _ACTIVE, maximum_bytes=8192, read_only=True)
            if active != {"schema_version": 1, "session_id": record["session_id"]}:
                raise UpdateError("A different update owns this profile; its state was preserved.")
        save_private_json(state, _ACTIVE, {"schema_version": 1, "session_id": record["session_id"]})
        try:
            _require_idle(state)
            current_snapshot = _state_snapshot(state)
            if record.get("state_snapshot") is not None and record["state_snapshot"] != current_snapshot:
                raise UpdateError("Saved state changed after compatibility verification; no installation was attempted.")
            if _hash(config_path) != record["configuration_sha256"]:
                raise UpdateError("Settings changed before update; they were preserved.")
            if not replacement_attempted and record.get("previous_component") is not None:
                selected, launcher, assembly = verify_native_component(state)
                if {"path": str(selected), "sha256": launcher, "assembly_sha256": assembly} != record["previous_component"]:
                    raise UpdateError("The selected Windows component changed during preparation; the newer selection was preserved.")
            save("pausing_existing_schedule")
            paused = True  # A failed native response may still have disabled it.
            schedule.set_enabled(record["schedule"], False)
            save("closing_owned_windows")
            for owner in matching_owners(config_path, state, Path(identity["python"])):
                close_owner(owner)
            if matching_owners(config_path, state, Path(identity["python"])):
                raise UpdateError("A Watcher owner is still running. No process was killed.")
            _require_idle(state)
            replacement_attempted = True
            record["backend_replacement_attempted"] = True
            save("installing_backend")
            _command([uv, "tool", "install", "--force", "--offline", "--python", identity["base_python"],
                      "--reinstall-package", "dependabot-watcher", record["wheel"]])
            _verify_runtime(identity, uv, record["source_files"])
            _probe_state(identity["python"], config_path, state)
            component = _verify_component(Path(record["component"]["path"]), record["source_commit"])
            if component != record["component"] or _hash(config_path) != record["configuration_sha256"]:
                raise UpdateError("Components or settings changed during update; recovery is required.")
            save("registering_component")
            _prepare_native_review(config_path, state, component=Path(component["path"]),
                                   backend_python=Path(identity["python"]), updating=True)
            selected, launcher, assembly = verify_native_component(state)
            if str(selected) != component["path"] or launcher != component["sha256"] or assembly != component["assembly_sha256"]:
                raise UpdateError("Updated native registration did not match the verified package.")
            verified = True
            save("restoring_existing_schedule")
            schedule.set_enabled(record["schedule"], record["schedule"]["enabled"] if record["schedule"] else False)
            save("complete")
            delete_private_artifact(state, _ACTIVE, maximum_bytes=8192)
        except Exception as exc:
            if record["phase"] == "complete":
                raise UpdateError(f"Update {record['session_id']} is verified complete but needs fence cleanup. Recover this saved update; replacement will not be repeated.") from exc
            # Never restart a schedule against an unverified partial backend.
            if paused and (not replacement_attempted or verified):
                try:
                    schedule.set_enabled(record["schedule"], record["schedule"]["enabled"] if record["schedule"] else False)
                except Exception:
                    save("schedule_recovery_required")
                    raise UpdateError(f"Update {record['session_id']} needs schedule recovery; the task was not recreated.") from exc
            if not replacement_attempted:
                save("cancelled_before_installation")
                delete_private_artifact(state, _ACTIVE, maximum_bytes=8192)
                raise UpdateError("Update stopped before installation. The original schedule state and installed components were preserved; no process was killed.") from exc
            save("recovery_required")
            raise UpdateError(f"Update {record['session_id']} needs recovery. Saved settings/history and task definition were retained; no action was replayed.") from exc
    # Open through the updated installed entry, never this possibly bootstrapped
    # source interpreter. Scheduling/normal activation now use the same binding.
    return _updated_result(config_path, state, record)


def _updated_result(config_path: Path, state: Path, record: dict[str, Any]) -> dict[str, Any]:
    identity = record["installed"]
    window_launch_submitted = True
    try:
        _command([identity["command"], "review", "--config", str(config_path), "--state-dir", str(state)], timeout=45)
    except (UpdateError, OSError, subprocess.TimeoutExpired):
        # Replacement and schedule restoration are already complete. Do not
        # describe a window-launch problem as a reason to reinstall/recover.
        window_launch_submitted = False
    return {"schema_version": 1, "status": "updated", "session_id": record["session_id"],
            "source_commit": record["source_commit"], "schedule_preserved": record["schedule"] is not None,
            "schedule_status": "restored" if record["schedule"] is not None else "not_present",
            "settings_preserved": True, "history_preserved": True,
            "window_launch_submitted": window_launch_submitted, "window_visibility": "unverified",
            "model_calls": False, "github_mutations": False}


def recover_update(config_path: Path, state: Path, session: str) -> dict[str, Any]:
    """Explicitly finish the same saved install; never replay a repository action."""
    if not _SESSION.fullmatch(session):
        raise UpdateError("Select the exact saved update session.")
    config_path, state = (path.expanduser().absolute() for path in (config_path, state))
    active = load_private_json(state, _ACTIVE, maximum_bytes=8192, read_only=True)
    if active != {"schema_version": 1, "session_id": session}:
        raise UpdateError("The update recovery identity does not match the active session.")
    root = state / "updates" / session
    record = load_private_json(root, Path("receipt.json"), maximum_bytes=1_000_000, read_only=True)
    observed_record = dict(record)
    if not {"config_path", "state_snapshot", "previous_component", "schedule"} <= set(record):
        raise UpdateError("This development update receipt lacks required safety baselines. Preserve its recovery fence for explicit installation repair; no replacement or cleanup was attempted.")
    uv = shutil.which("uv")
    installed = record.get("installed")
    sources = record.get("source_files")
    component = record.get("component")
    saved_schedule = record.get("schedule")
    previous_component = record.get("previous_component")
    if (uv is None or type(record.get("schema_version")) is not int or record["schema_version"] != 1
            or record.get("session_id") != session
            or record.get("config_path") != str(config_path.resolve(strict=True))
            or ("backend_replacement_attempted" in record and type(record["backend_replacement_attempted"]) is not bool)
            or record.get("phase") not in {"prepared", "complete", "cancelled_before_installation", "pausing_existing_schedule", "closing_owned_windows", "installing_backend",
                "registering_component", "restoring_existing_schedule", "schedule_recovery_required", "recovery_required"}
            or not isinstance(record.get("source_commit"), str) or not _REVISION.fullmatch(record["source_commit"])
            or not isinstance(installed, dict)
            or set(installed) != {"prefix", "python", "base_python", "package", "command"}
            or any(not isinstance(value, str) or not Path(value).is_absolute() for value in installed.values())
            or not isinstance(sources, dict) or not 1 <= len(sources) <= 1000
            or any(not isinstance(name, str) or Path(name).suffix not in {".py", ".md", ".json"} or "\\" in name
                or Path(name).is_absolute() or any(part in {"", ".", ".."} for part in name.split("/"))
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                for name, digest in sources.items())
            or not isinstance(component, dict)
            or set(component) != {"path", "sha256", "assembly_sha256", "manifest_sha256"}
            or not isinstance(component.get("path"), str) or not Path(component["path"]).is_absolute()
            or any(not isinstance(component.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", component[key])
                   for key in ("sha256", "assembly_sha256", "manifest_sha256"))
            or not isinstance(previous_component, dict)
            or set(previous_component) != {"path", "sha256", "assembly_sha256"}
            or not isinstance(previous_component.get("path"), str) or not Path(previous_component["path"]).is_absolute()
            or any(not isinstance(previous_component.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", previous_component[key])
                   for key in ("sha256", "assembly_sha256"))
            or (saved_schedule is not None and (not isinstance(saved_schedule, dict)
                or set(saved_schedule) != {"job_id", "authority", "definition_sha256", "enabled"}
                or type(saved_schedule.get("enabled")) is not bool
                or any(not isinstance(saved_schedule.get(key), str) for key in ("job_id", "authority", "definition_sha256"))))
            or not isinstance(record.get("wheel"), str) or Path(record["wheel"]).parent != root / "dist"
            or _hash(Path(record["wheel"])) != record.get("wheel_sha256")
            ):
        raise UpdateError("Saved update artifacts changed. Restore the exact verified wheel/package for this update before retrying recovery; no fence was removed.")
    prefix = Path(_command([uv, "tool", "dir"], timeout=30)) / "dependabot-watcher"
    if (Path(installed["prefix"]) != prefix or Path(installed["command"]) != prefix / "bin" / "dependabot-watcher"
            or Path(installed["python"]) != prefix / "bin" / "python"
            or not Path(installed["package"]).is_relative_to(prefix)
            or _verify_component(Path(component["path"]), record["source_commit"]) != component):
        raise UpdateError("Saved installation ownership or component changed. Restore the exact verified supplied component before retrying recovery; no fence was removed.")
    load_config(config_path)
    # Explicit recovery may adopt valid settings edited since interruption.
    # Preserve them, without granting any model, Git or repository operation.
    record["configuration_sha256"] = _hash(config_path)
    # Preserve the original pre-install state identity through recovery. A
    # partial install does not authorize adopting incompatible changed journals.
    snapshot = record.get("state_snapshot")
    if (not isinstance(snapshot, dict) or not set(_JOURNALS) <= set(snapshot)
            or len(snapshot) > 5000 or any(not isinstance(name, str) or Path(name).is_absolute()
                or any(part in {'', '.', '..'} for part in name.split('/')) or '\\' in name for name in snapshot)
            or any(value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value))
                   for value in snapshot.values())):
        raise UpdateError("Saved state-compatibility evidence is malformed; no recovery was attempted.")
    with run_lock(state / "updates"):
        if not os.path.lexists(state / _ACTIVE):
            raise UpdateError("This update no longer has an active recovery fence. Recheck its saved completion; no installation was repeated.")
        if (load_private_json(state, _ACTIVE, maximum_bytes=8192, read_only=True) != active
                or load_private_json(root, Path("receipt.json"), maximum_bytes=1_000_000, read_only=True) != observed_record):
            raise UpdateError("Update recovery state changed before admission. Recheck the saved outcome; no installation was repeated.")
        save_private_json(root, Path("receipt.json"), record, maximum_bytes=1_000_000)
        schedule = UpdateSchedule(config_path, state, Path(record["installed"]["command"]))
        if record["phase"] == "complete":
            # A crash between completion receipt and fence deletion must not
            # reinstall or close a now-current owner. Verify and clean up only.
            with _update_locks(config_path, state):
                _require_idle(state)
                _verify_runtime(record["installed"], uv, record["source_files"])
                _probe_state(record["installed"]["python"], config_path, state)
                selected, launcher, assembly = verify_native_component(state)
                if (str(selected), launcher, assembly) != (component["path"], component["sha256"], component["assembly_sha256"]):
                    raise UpdateError("Completed update registration changed; its recovery fence was preserved.")
                schedule.set_enabled(saved_schedule, saved_schedule["enabled"] if saved_schedule else False)
                delete_private_artifact(state, _ACTIVE, maximum_bytes=8192)
            return _updated_result(config_path, state, record)
        return _complete(config_path, state, root, record, schedule, uv)
