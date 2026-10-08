"""Opt-in refresh of a registered detached maintenance worktree only.

No reset, developer-branch update, push or merge. A durable ownership receipt
must first be established at the collected base. Changed/untracked files or an
unexpected local commit remain untouched. This is not a freeze against arbitrary
same-user editors or Git processes.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Mapping

from .review_queue import ReviewQueue, ReviewQueueError, _validate_workspace_evidence, _load_queue
from .runtime_state import ensure_private_directory, load_private_json, run_lock, save_private_json, validate_user_owned_directory
from .supervisor_workspace import SupervisorWorkspaceEvidence, _git, verify_supervisor_workspace, same_supervisor_session_workspace


@contextmanager
def supervisor_workspace_lease(queue: ReviewQueue, repository: str) -> Iterator[None]:
    """Serialize Watcher use of one worktree even across different state profiles."""
    registrations = [item for item in queue.config.supervisor.workspaces
                     if item.repository.casefold() == repository.casefold()]
    if len(registrations) != 1:
        raise ReviewQueueError("Supervisor requires a unique registered worktree.")
    workspace = Path(registrations[0].directory)
    validate_user_owned_directory(workspace)
    head = _git(workspace, "rev-parse", "--verify", "HEAD^{commit}")
    proof = verify_supervisor_workspace(queue.config, repository,
        {"repository": repository, "evidence_snapshot": {"base_sha": head}})
    root = ensure_private_directory(Path(proof.git_directory) / "dependabot-watcher-owner")
    with run_lock(root):
        yield


def refresh_supervisor_workspace(
    queue: ReviewQueue, repository: str, repository_plan: Mapping[str, Any],
) -> SupervisorWorkspaceEvidence:
    """Return fresh evidence, advancing only an explicitly authorized owned copy.

The caller must hold its configured launch lease and recheck policy afterwards.
    First registration never adopts a stale/dirty copy. Pending receipts allow
    observed recovery of a completed or not-applied local checkout, never force
    or adoption of an unexpected commit.
"""
    registration = next((item for item in queue.config.supervisor.workspaces
                         if item.repository.casefold() == repository.casefold()), None)
    if registration is None or not registration.refresh_authorized:
        return verify_supervisor_workspace(queue.config, repository, repository_plan)
    if os.name != "posix":
        raise ReviewQueueError("Managed workspace refresh currently requires POSIX/WSL.")
    queue.require_current_configuration()
    workspace = Path(registration.directory)
    snapshot = repository_plan.get("evidence_snapshot")
    target = snapshot.get("base_sha") if isinstance(snapshot, Mapping) else None
    if (not isinstance(repository_plan.get("repository"), str)
            or repository_plan["repository"].casefold() != repository.casefold()
            or not isinstance(target, str) or re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", target) is None):
        raise ReviewQueueError("Managed workspace refresh requires an exact collected base.")
    root = ensure_private_directory(queue.directory / "supervisor-workspaces")
    identity = hashlib.sha256((repository.casefold() + "\n" + registration.directory).encode()).hexdigest()
    with run_lock(root / identity):
        location = Path(identity) / "ownership.json"
        if not os.path.lexists(root / location):
            # Enabling refresh after base movement may adopt only a previously
            # verified copy, not any clean checkout at an arbitrary older head.
            with run_lock(queue.directory, wait_seconds=2):
                previous = _load_queue(queue.directory)["workspace_evidence"].get(repository.casefold())
            initial_plan = repository_plan
            if previous is not None:
                _validate_workspace_evidence(previous)
                initial_plan = {"repository": repository, "evidence_snapshot": {"base_sha": previous["head_sha"]}}
            proof = verify_supervisor_workspace(queue.config, repository, initial_plan)
            if previous is not None and not same_supervisor_session_workspace(previous,
                    {**asdict(proof), "instruction_fingerprints": [list(item) for item in proof.instruction_fingerprints]}):
                raise ReviewQueueError("Previously verified workspace identity or instructions changed; no checkout was performed.")
            if _git(workspace, "symbolic-ref", "-q", "HEAD", empty_ok=True):
                raise ReviewQueueError("Managed refresh requires a detached maintenance worktree, not a working branch.")
            save_private_json(root, location, {"schema_version": 1, "evidence": asdict(proof), "target_sha": None})
            if proof.head_sha.casefold() == target.casefold():
                return proof
        receipt = load_private_json(root, location)
        if (not isinstance(receipt, dict) or set(receipt) != {"schema_version", "evidence", "target_sha"}
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or not isinstance(receipt["evidence"], dict)):
            raise ReviewQueueError("Managed workspace ownership receipt is invalid.")
        previous = receipt["evidence"]
        _validate_workspace_evidence(previous)
        # Reuse the normal identity, ownership, instructions and clean-tree
        # verifier; the receipt is not permission to adopt arbitrary Git state.
        prior_head = previous.get("head_sha")
        pending = receipt["target_sha"]
        if (not isinstance(prior_head, str) or re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", prior_head) is None
                or pending is not None and (not isinstance(pending, str)
                    or re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", pending) is None)):
            raise ReviewQueueError("Managed workspace ownership commit is invalid.")
        actual = _git(workspace, "rev-parse", "--verify", "HEAD^{commit}")
        expected = pending if pending is not None and actual.casefold() == pending.casefold() else prior_head
        proof = verify_supervisor_workspace(queue.config, repository,
            {"repository": repository_plan["repository"], "evidence_snapshot": {"base_sha": expected}})
        if (previous["repository"].casefold() != proof.repository.casefold()
                or any(previous.get(key) != getattr(proof, key) for key in
                       ("directory", "git_directory", "common_directory"))):
            raise ReviewQueueError("Managed workspace identity changed; no checkout was performed.")
        if _git(workspace, "symbolic-ref", "-q", "HEAD", empty_ok=True):
            raise ReviewQueueError("Managed refresh refuses an attached working branch.")
        if pending is not None:
            # Verification above permits only the exact prior or intended head,
            # clean and detached. Observe a completed checkout or a not-applied
            # intent, then use the current collected base below. This local step
            # is idempotent; it never retries inference or a remote mutation.
            save_private_json(root, location, {"schema_version": 1, "evidence": asdict(proof), "target_sha": None})
        if proof.head_sha.casefold() == target.casefold():
            return proof
        queue.require_current_configuration()
        # Fetch an exact object without moving local/tracking branches or
        # FETCH_HEAD. The normal verifier already checked credential-free origin.
        try:
            _git(workspace, "cat-file", "-e", target + "^{commit}")
        except ReviewQueueError:
            _git(workspace, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                 "origin", target, overrides=("remote.origin.uploadpack=git-upload-pack",), timeout_seconds=120)
        queue.require_current_configuration()
        # A fetch may be slow: recheck all local state before writing intent.
        proof = verify_supervisor_workspace(queue.config, repository,
            {"repository": repository_plan["repository"], "evidence_snapshot": {"base_sha": proof.head_sha}})
        if _git(workspace, "symbolic-ref", "-q", "HEAD", empty_ok=True):
            raise ReviewQueueError("Managed workspace branch changed during fetch.")
        filters = _git(workspace, "config", "--name-only", "--get-regexp",
                       r"^filter\..*\.(clean|smudge|process)$", empty_ok=True).splitlines()
        if any(not re.fullmatch(r"filter\.[A-Za-z0-9_.-]+\.(clean|smudge|process)", key) for key in filters):
            raise ReviewQueueError("Managed workspace filter configuration is unsupported.")
        save_private_json(root, location, {"schema_version": 1, "evidence": asdict(proof), "target_sha": target})
        _git(workspace, "checkout", "--detach", target, overrides=tuple(f"{key}=" for key in filters))
        queue.require_current_configuration()
        fresh = verify_supervisor_workspace(queue.config, repository, repository_plan)
        save_private_json(root, location, {"schema_version": 1, "evidence": asdict(fresh), "target_sha": None})
        return fresh
