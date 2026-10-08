from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.review_queue import ReviewQueue, ReviewQueueError, _load_queue, _QUEUE_PATH
from dependabot_watcher.runtime_state import RuntimeLockBusyError, load_private_json, save_private_json
from dependabot_watcher.supervisor_workspace_refresh import refresh_supervisor_workspace, supervisor_workspace_lease
from dependabot_watcher.supervisor_workspace import verify_supervisor_workspace
from test_supervisor_workspace import git, workspace_case


@pytest.fixture
def managed_case(workspace_case: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    case = workspace_case
    registration = replace(case["config"].supervisor.workspaces[0], refresh_authorized=True)
    config = replace(case["config"], supervisor=replace(case["config"].supervisor, workspaces=(registration,)))
    queue = ReviewQueue(config, tmp_path / "state")
    return {**case, "queue": queue}


def refresh(case: dict[str, Any], plan: dict[str, Any] | None = None):
    with supervisor_workspace_lease(case["queue"], case["plan"]["repository"]):
        return refresh_supervisor_workspace(case["queue"], case["plan"]["repository"], plan or case["plan"])


def next_base(case: dict[str, Any]) -> dict[str, Any]:
    (case["primary"] / "app.py").write_text("VALUE = 2\n")
    git(case["primary"], "add", "app.py")
    git(case["primary"], "commit", "-m", "Synthetic remote maintenance")
    return {"repository": case["plan"]["repository"],
            "evidence_snapshot": {"base_sha": git(case["primary"], "rev-parse", "HEAD")}}


def receipt_path(case: dict[str, Any]) -> Path:
    return next((case["queue"].directory / "supervisor-workspaces").glob("*/ownership.json"))


def test_owned_detached_copy_advances_without_changing_developer_branch(managed_case: dict[str, Any]) -> None:
    case = managed_case
    assert refresh(case).head_sha == case["head"]
    plan = next_base(case)
    (case["primary"] / "developer.txt").write_text("Keep unfinished developer work\n")
    before = git(case["primary"], "status", "--porcelain"), git(case["primary"], "show-ref")
    proof = refresh(case, plan)
    assert proof.head_sha == plan["evidence_snapshot"]["base_sha"]
    assert (case["worktree"] / "app.py").read_text() == "VALUE = 2\n"
    assert (git(case["primary"], "status", "--porcelain"), git(case["primary"], "show-ref")) == before
    assert (case["primary"] / "developer.txt").read_text() == "Keep unfinished developer work\n"
    assert git(case["worktree"], "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert refresh(case, plan) == proof


@pytest.mark.parametrize("change", ["tracked", "untracked", "commit", "attached"])
def test_changed_maintenance_work_is_preserved(managed_case: dict[str, Any], change: str) -> None:
    case = managed_case
    refresh(case)
    plan = next_base(case)
    if change == "tracked":
        (case["worktree"] / "app.py").write_text("Unfinished maintenance change\n")
    elif change == "untracked":
        (case["worktree"] / "unsaved.txt").write_text("Keep this\n")
    elif change == "commit":
        (case["worktree"] / "app.py").write_text("VALUE = 3\n")
        git(case["worktree"], "add", "app.py")
        git(case["worktree"], "commit", "-m", "Unrelated maintenance work")
    else:
        git(case["worktree"], "switch", "-c", "developer-work")
    before = git(case["worktree"], "rev-parse", "HEAD"), git(case["worktree"], "status", "--porcelain")
    with pytest.raises(ReviewQueueError):
        refresh(case, plan)
    assert (git(case["worktree"], "rev-parse", "HEAD"), git(case["worktree"], "status", "--porcelain")) == before


def test_first_registration_cannot_adopt_stale_worktree(managed_case: dict[str, Any]) -> None:
    case = managed_case
    plan = next_base(case)
    with pytest.raises(ReviewQueueError, match="collected base"):
        refresh(case, plan)
    assert git(case["worktree"], "rev-parse", "HEAD") == case["head"]
    assert not list(case["queue"].directory.rglob("ownership.json"))


@pytest.mark.parametrize("change", ["none", "commit", "instructions", "fingerprint", "dirty"])
def test_enabling_refresh_after_base_move_requires_unchanged_prior_verified_copy(
    managed_case: dict[str, Any], change: str,
) -> None:
    case = managed_case
    trusted = case["primary"].parent / "trusted.md"
    if change == "fingerprint":
        trusted.write_text("Original trusted instructions\n")
        registration = replace(case["queue"].config.supervisor.workspaces[0], instruction_sources=(str(trusted),))
        case["queue"].config = replace(case["queue"].config,
            supervisor=replace(case["queue"].config.supervisor, workspaces=(registration,)))
    proof = verify_supervisor_workspace(case["queue"].config, case["plan"]["repository"], case["plan"])
    state = _load_queue(case["queue"].directory)
    state["workspace_evidence"][case["plan"]["repository"].casefold()] = json.loads(json.dumps(asdict(proof)))
    save_private_json(case["queue"].directory, _QUEUE_PATH, state)
    plan = next_base(case)
    if change == "commit":
        git(case["worktree"], "commit", "--allow-empty", "-m", "Unexpected local commit")
    elif change == "instructions":
        (case["worktree"] / "AGENTS.md").write_text("Changed instructions\n")
        git(case["worktree"], "add", "AGENTS.md")
        git(case["worktree"], "commit", "-m", "Changed harness")
    elif change == "dirty":
        (case["worktree"] / "app.py").write_text("Unfinished work\n")
    elif change == "fingerprint":
        trusted.write_text("Different trusted instructions\n")
    before = git(case["worktree"], "rev-parse", "HEAD"), git(case["worktree"], "status", "--porcelain")
    if change == "none":
        assert refresh(case, plan).head_sha == plan["evidence_snapshot"]["base_sha"]
        assert git(case["primary"], "rev-parse", "HEAD") == plan["evidence_snapshot"]["base_sha"]
    else:
        with pytest.raises(ReviewQueueError):
            refresh(case, plan)
        assert (git(case["worktree"], "rev-parse", "HEAD"), git(case["worktree"], "status", "--porcelain")) == before
        assert not list(case["queue"].directory.rglob("ownership.json"))


def test_foreign_plan_cannot_trigger_checkout(managed_case: dict[str, Any]) -> None:
    case = managed_case
    refresh(case)
    plan = next_base(case)
    plan["repository"] = "other/repository"
    with pytest.raises(ReviewQueueError, match="exact collected base"):
        refresh(case, plan)
    assert git(case["worktree"], "rev-parse", "HEAD") == case["head"]


@pytest.mark.parametrize("completed", [False, True])
def test_interrupted_local_checkout_recovers_from_verified_prior_or_completed_head(
    managed_case: dict[str, Any], completed: bool,
) -> None:
    case = managed_case
    refresh(case)
    plan = next_base(case)
    path = receipt_path(case)
    receipt = load_private_json(path.parent, Path(path.name))
    receipt["target_sha"] = plan["evidence_snapshot"]["base_sha"]
    save_private_json(path.parent, Path(path.name), receipt)
    if completed:
        git(case["worktree"], "checkout", "--detach", receipt["target_sha"])
        assert refresh(case, plan).head_sha == receipt["target_sha"]
        assert load_private_json(path.parent, Path(path.name))["target_sha"] is None
    else:
        assert git(case["worktree"], "rev-parse", "HEAD") == case["head"]
        assert refresh(case, plan).head_sha == receipt["target_sha"]
        assert load_private_json(path.parent, Path(path.name))["target_sha"] is None
        assert refresh(case, plan).head_sha == receipt["target_sha"]


def test_workspace_lease_serializes_different_profiles(managed_case: dict[str, Any], tmp_path: Path) -> None:
    case = managed_case
    other = ReviewQueue(case["queue"].config, tmp_path / "other-state")
    with supervisor_workspace_lease(case["queue"], case["plan"]["repository"]):
        with pytest.raises(RuntimeLockBusyError):
            with supervisor_workspace_lease(other, case["plan"]["repository"]):
                pytest.fail("Two Watcher profiles acquired the same worktree")


def test_malformed_ownership_receipt_cannot_grant_refresh(managed_case: dict[str, Any]) -> None:
    case = managed_case
    refresh(case)
    path = receipt_path(case)
    original = load_private_json(path.parent, Path(path.name))
    malformed = copy.deepcopy(original)
    malformed["evidence"]["directory"] = str(case["primary"])
    save_private_json(path.parent, Path(path.name), malformed)
    with pytest.raises(ReviewQueueError, match="identity changed"):
        refresh(case, next_base(case))
    assert git(case["worktree"], "rev-parse", "HEAD") == case["head"]
