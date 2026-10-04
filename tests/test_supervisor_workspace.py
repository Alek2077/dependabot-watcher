from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import supervisor_workspace
from dependabot_watcher.config import SupervisorConfig, SupervisorWorkspaceConfig, parse_config
from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError, supervisor_context_digest
from dependabot_watcher.supervision import SupervisorJobs
from dependabot_watcher.supervisor_workspace import (
    SupervisorAuthorityChanged, SupervisorInstructionGuard,
    _github_repository, _git, _instruction_fingerprint, verify_supervisor_workspace,
)
from test_execution import refresh_plan_identities, saved_plan
from test_review_queue import persist_plan, snapshot


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX/WSL isolated supervisor workspace")


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def workspace_case(tmp_path: Path) -> dict[str, Any]:
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "--initial-branch=main")
    git(primary, "config", "user.name", "Workspace test")
    git(primary, "config", "user.email", "workspace@example.invalid")
    git(primary, "remote", "add", "origin", "https://github.com/example-org/example-app.git")
    (primary / "AGENTS.md").write_text("Investigate dependency compatibility; preserve user work.\n")
    (primary / "app.py").write_text("VALUE = 1\n")
    git(primary, "add", "AGENTS.md", "app.py")
    git(primary, "commit", "-m", "Synthetic baseline")
    head = git(primary, "rev-parse", "HEAD")
    worktree = tmp_path / "maintenance"
    git(primary, "worktree", "add", "--detach", str(worktree), head)
    config = parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]})
    config = replace(config, supervisor=SupervisorConfig(workspaces=(
        SupervisorWorkspaceConfig("example-org/example-app", str(worktree), (str(worktree / "AGENTS.md"),)),)))
    plan = {"repository": "example-org/example-app", "evidence_snapshot": {"base_sha": head}}
    return {"primary": primary, "worktree": worktree, "config": config, "plan": plan, "head": head}


def test_real_worktree_verifies_exact_repository_base_and_instruction_fingerprint_without_writes(
    workspace_case: dict[str, Any],
) -> None:
    case = workspace_case
    before = git(case["primary"], "show-ref")
    evidence = verify_supervisor_workspace(case["config"], "EXAMPLE-ORG/Example-App", case["plan"])
    assert evidence.repository == case["plan"]["repository"]
    assert evidence.head_sha == case["head"]
    assert evidence.directory == str(case["worktree"])
    assert evidence.git_directory != evidence.common_directory
    assert evidence.instruction_fingerprints == ((str(case["worktree"] / "AGENTS.md"),
        _instruction_fingerprint(case["worktree"] / "AGENTS.md")),)
    assert git(case["primary"], "show-ref") == before
    assert git(case["primary"], "rev-parse", "HEAD") == case["head"]
    assert git(case["worktree"], "status", "--porcelain") == ""


def test_real_worktree_proof_flows_into_durable_job_and_native_context_revision(
    workspace_case: dict[str, Any], tmp_path: Path,
) -> None:
    case = workspace_case
    trusted = tmp_path / "trusted-instructions.md"
    trusted.write_text("First trusted investigation policy.\n")
    registration = replace(case["config"].supervisor.workspaces[0], instruction_sources=(str(trusted),))
    policy = replace(case["config"].supervisor, mode="approval_first", provider="codex_app_server",
        model="fixture-model", reasoning_effort="low", expected_account_type="chatgpt",
        model_use_acknowledged=True, workspaces=(registration,))
    config = replace(case["config"], supervisor=policy)
    report = saved_plan()
    report["repositories"][0]["evidence_snapshot"]["base_sha"] = case["head"]
    for step in report["repositories"][0]["recommended_order"]:
        step["freshness"]["base_sha"] = case["head"]
    report = refresh_plan_identities(report)
    queue = ReviewQueue(config, tmp_path / "state")
    persist_plan(queue.directory, report)
    proof = verify_supervisor_workspace(config, case["plan"]["repository"], report["repositories"][0])
    queue.record_workspace_evidence(asdict(proof))
    jobs = SupervisorJobs(queue)
    job = jobs.prepare(case["plan"]["repository"])
    context = job["context"]
    assert context["workspace_evidence"]["head_sha"] == case["head"]
    assert context["workspace_evidence"]["instruction_fingerprints"] == [[str(trusted), _instruction_fingerprint(trusted)]]
    assert snapshot(queue)["supervisor_context_digests"][case["plan"]["repository"]] == job["context_digest"]
    assert job["context_digest"] == supervisor_context_digest(context)
    trusted.write_text("Changed trusted investigation policy.\n")
    updated = verify_supervisor_workspace(config, case["plan"]["repository"], report["repositories"][0])
    queue.record_workspace_evidence(asdict(updated))
    with pytest.raises(ReviewQueueError, match="context changed"):
        jobs.start(job["job_id"])
    assert jobs.prepare(case["plan"]["repository"])["context_digest"] != job["context_digest"]
    assert git(case["primary"], "rev-parse", "HEAD") == case["head"]
    assert git(case["worktree"], "status", "--porcelain") == ""


@pytest.mark.parametrize("remote", ["https://github.com/EXAMPLE-ORG/Example-App.git",
    "git@github.com:example-org/example-app.git", "ssh://git@github.com/example-org/example-app.git"])
def test_normal_github_remote_forms_resolve_same_repository(workspace_case: dict[str, Any], remote: str) -> None:
    case = workspace_case
    git(case["primary"], "remote", "set-url", "origin", remote)
    assert verify_supervisor_workspace(case["config"], case["plan"]["repository"], case["plan"]).head_sha == case["head"]


@pytest.mark.parametrize("remote", ["https://github.com/other/repository.git", "https://other.invalid/example-org/example-app.git",
    "https://PRIVATE_MARKER@github.com/example-org/example-app.git", "ssh://user@github.com/example-org/example-app.git",
    "https://github.com/example-org/example-app.git?PRIVATE_MARKER", "file:///tmp/private-repository",
    "https://github.com:notaport/example-org/example-app.git"])
def test_other_ambiguous_or_credential_bearing_origins_are_refused_without_leaking(
    workspace_case: dict[str, Any], remote: str,
) -> None:
    case = workspace_case
    git(case["primary"], "remote", "set-url", "origin", remote)
    with pytest.raises(ReviewQueueError) as failure:
        verify_supervisor_workspace(case["config"], case["plan"]["repository"], case["plan"])
    assert "PRIVATE_MARKER" not in str(failure.value)
    assert remote not in str(failure.value)


@pytest.mark.parametrize("change", ["primary", "nested", "dirty", "untracked", "head", "scope", "unbound", "symlink", "base"])
def test_unbound_stale_dirty_or_nonisolated_workspaces_are_refused(workspace_case: dict[str, Any], change: str) -> None:
    case = workspace_case
    config, plan = case["config"], case["plan"]
    worktree = case["worktree"]
    registration = config.supervisor.workspaces[0]
    if change in {"primary", "nested", "symlink"}:
        if change == "primary":
            directory = case["primary"]
        elif change == "nested":
            directory = worktree / "nested"
            directory.mkdir()
        else:
            directory = worktree.parent / "alias"
            directory.symlink_to(worktree, target_is_directory=True)
        config = replace(config, supervisor=replace(config.supervisor,
            workspaces=(replace(registration, directory=str(directory)),)))
    elif change in {"dirty", "untracked"}:
        (worktree / ("app.py" if change == "dirty" else "new.txt")).write_text("Preserve my local work")
    elif change == "head":
        plan = {**plan, "evidence_snapshot": {"base_sha": "a" * 40}}
    elif change == "base":
        plan = {**plan, "evidence_snapshot": []}
    elif change == "scope":
        plan = {**plan, "repository": "other/repository"}
    else:
        config = replace(config, supervisor=replace(config.supervisor, workspaces=()))
    with pytest.raises(ReviewQueueError):
        verify_supervisor_workspace(config, "example-org/example-app", plan)
    assert git(case["primary"], "rev-parse", "HEAD") == case["head"]
    if change in {"dirty", "untracked"}:
        assert "Preserve my local work" in (worktree / ("app.py" if change == "dirty" else "new.txt")).read_text()


def test_ambient_git_redirection_cannot_change_verified_repository(
    workspace_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = workspace_case
    monkeypatch.setenv("GIT_DIR", str(case["primary"] / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(case["primary"]))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/other/repository.git")
    evidence = verify_supervisor_workspace(case["config"], case["plan"]["repository"], case["plan"])
    assert evidence.directory == str(case["worktree"])


def test_git_status_never_executes_a_repository_clean_filter(workspace_case: dict[str, Any]) -> None:
    case = workspace_case
    primary, worktree = case["primary"], case["worktree"]
    (primary / ".gitattributes").write_text("app.py filter=unsafe\n")
    git(primary, "add", ".gitattributes")
    git(primary, "commit", "-m", "Synthetic filter attribute")
    head = git(primary, "rev-parse", "HEAD")
    git(worktree, "checkout", "--detach", head)
    marker = primary.parent / "FILTER_EXECUTED"
    git(primary, "config", "filter.unsafe.clean", "touch " + str(marker))
    # Force status to consider conversion instead of trusting an unchanged stat.
    (worktree / "app.py").write_text("VALUE = 1\n")
    evidence = verify_supervisor_workspace(case["config"], case["plan"]["repository"],
        {**case["plan"], "evidence_snapshot": {"base_sha": head}})
    assert evidence.head_sha == head and not marker.exists()


def test_submodules_are_refused_without_recursing_into_their_git_configuration(workspace_case: dict[str, Any]) -> None:
    case = workspace_case
    git(case["worktree"], "update-index", "--add", "--cacheinfo", "160000," + case["head"] + ",module")
    with pytest.raises(ReviewQueueError, match="submodules"):
        verify_supervisor_workspace(case["config"], case["plan"]["repository"], case["plan"])


@pytest.mark.parametrize("fault", ["missing", "unsafe", "oversized", "binary", "secret", "secret_symlink"])
def test_instruction_sources_are_bounded_owned_text_not_secret_targets(tmp_path: Path, fault: str) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("Trusted instructions\n")
    if fault == "missing":
        source.unlink()
    elif fault == "unsafe":
        source.chmod(0o666)
    elif fault == "oversized":
        source.write_bytes(b"a" * 1_000_001)
    elif fault == "binary":
        source.write_bytes(b"\xff")
    elif fault == "secret":
        source = tmp_path / ".env"
        source.write_text("DO_NOT_READ")
    else:
        target = tmp_path / ".env"
        target.write_text("DO_NOT_READ")
        source.unlink()
        source.symlink_to(target)
    with pytest.raises(ReviewQueueError):
        _instruction_fingerprint(source)


def test_explicit_owned_instruction_symlink_and_changed_content_are_detected(tmp_path: Path) -> None:
    target = tmp_path / "trusted.md"
    target.write_text("First instruction\n")
    source = tmp_path / "AGENTS.md"
    source.symlink_to(target)
    first = _instruction_fingerprint(source)
    target.write_text("Changed instruction\n")
    assert _instruction_fingerprint(source) != first


@pytest.mark.parametrize("change", ["content", "missing", "unsafe", "symlink"])
def test_active_instruction_guard_detects_changed_missing_or_untrusted_sources(tmp_path: Path, change: str) -> None:
    target = tmp_path / "trusted.md"
    target.write_text("First policy.\n")
    source = tmp_path / "AGENTS.md"
    source.symlink_to(target)
    proof = {"repository": "example-org/example-app", "directory": str(tmp_path),
             "git_directory": str(tmp_path / "git" / "worktrees" / "fixture"),
             "common_directory": str(tmp_path / "git"), "head_sha": "b" * 40,
             "instruction_fingerprints": [[str(source), _instruction_fingerprint(source)]]}
    guard = SupervisorInstructionGuard(proof)
    guard.verify(force=True)
    before = target.stat()
    if change == "content":
        target.write_text("Other policy.\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "missing":
        target.unlink()
    elif change == "unsafe":
        target.chmod(0o666)
    else:
        another = tmp_path / "another.md"
        another.write_text("Other policy.\n")
        source.unlink()
        source.symlink_to(another)
    with pytest.raises(SupervisorAuthorityChanged) as failure:
        guard.verify()
    assert str(source) not in str(failure.value)


def test_instruction_guard_polling_uses_metadata_but_force_check_reads_unchanged_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("Trusted policy.\n")
    proof = {"repository": "example-org/example-app", "directory": str(tmp_path),
             "git_directory": str(tmp_path / "git" / "worktrees" / "fixture"),
             "common_directory": str(tmp_path / "git"), "head_sha": "b" * 40,
             "instruction_fingerprints": [[str(source), _instruction_fingerprint(source)]]}
    calls: list[Path] = []
    original = supervisor_workspace._instruction_fingerprint
    def fingerprint(path: Path) -> str:
        calls.append(path)
        return original(path)
    monkeypatch.setattr(supervisor_workspace, "_instruction_fingerprint", fingerprint)
    guard = SupervisorInstructionGuard(proof)
    guard.verify(force=True)
    for _ in range(8):
        guard.verify()
    assert calls == [source]
    guard.verify(force=True)
    assert calls == [source, source]


def test_git_failures_hide_raw_output_and_keep_read_only_command_bounds(tmp_path: Path) -> None:
    with pytest.raises(ReviewQueueError) as failure:
        _git(tmp_path, "rev-parse", "--show-toplevel")
    assert str(tmp_path) not in str(failure.value)
    with pytest.raises(ValueError):
        _github_repository("https://github.com:notaport/example/repo")
