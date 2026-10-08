from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher.config import SupervisorConfig, load_config, parse_config
from dependabot_watcher.supervisor_workspace import verify_supervisor_workspace
from dependabot_watcher.cli import main
from dependabot_watcher.setup import SetupError, SetupRequest, setup as create_setup
import dependabot_watcher.supervisor_workspace_provisioning as provisioning
from dependabot_watcher.supervisor_workspace_provisioning import (
    WorkspaceProvisioningError,
    WorkspaceProvisionRequest,
    provision_supervisor_workspaces,
    workspace_directory_for,
)


REPOSITORY = "example-org/example-app"


def git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def source_repository(path: Path, *, agents_target: str | None = None) -> tuple[Path, str]:
    path.mkdir()
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "Provisioning test")
    git(path, "config", "user.email", "provisioning@example.invalid")
    git(path, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    if agents_target is None:
        (path / "AGENTS.md").write_text("Review dependency changes; preserve user work.\n", encoding="utf-8")
    else:
        (path / "AGENTS.md").symlink_to(agents_target)
    (path / "app.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
    (path / ".gitattributes").write_text("app.py filter=unsafe\n", encoding="utf-8")
    git(path, "add", "AGENTS.md", "app.py", ".gitattributes")
    git(path, "commit", "-m", "Synthetic committed base")
    return path, git(path, "rev-parse", "HEAD")


def separate_git_dir_repository(path: Path) -> tuple[Path, str, Path]:
    path.mkdir()
    original, base_sha = source_repository(path / "original")
    checkout = path / "checkout"
    common = path / "external-git-metadata"
    empty_template = path / "empty-template"
    empty_template.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main",
         f"--separate-git-dir={common}", f"--template={empty_template}", str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )
    git(checkout, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    git(checkout, "fetch", "--no-tags", str(original), "main")
    fetched_head = git(checkout, "rev-parse", "FETCH_HEAD")
    git(checkout, "update-ref", "refs/heads/main", fetched_head)
    git(checkout, "symbolic-ref", "HEAD", "refs/heads/main")
    git(checkout, "-c", "core.hooksPath=/dev/null", "reset", "--hard", "HEAD")
    return checkout, base_sha, common


def request(source: Path, root: Path, base_sha: str | None) -> WorkspaceProvisionRequest:
    return WorkspaceProvisionRequest(
        repository=REPOSITORY,
        source_checkout=source,
        workspace_directory=workspace_directory_for(root, REPOSITORY),
        branch="main",
        collected_base_sha=base_sha,
    )


def _supervisor_config(workspace: Path, instructions: tuple[str, ...]):
    return parse_config({
        "schema_version": 1,
        "repositories": [{"name": REPOSITORY}],
        "supervisor": {
            "mode": "approval_first",
            "provider": "codex_app_server",
            "model": "selected-test-model",
            "reasoning_effort": "low",
            "expected_account_type": "chatgpt",
            "access": "inherit",
            "model_use_acknowledged": True,
            "workspaces": [{
                "repository": REPOSITORY,
                "directory": str(workspace),
                "instruction_sources": list(instructions),
            }],
        },
    })


def _setup_supervisor_config():
    return SupervisorConfig(
        mode="approval_first",
        provider="codex_app_server",
        model="selected-test-model",
        reasoning_effort="low",
        expected_account_type="chatgpt",
        access="inherit",
        model_use_acknowledged=True,
    )


def test_provisions_private_exact_base_from_dirty_source_and_reuses_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state" / "supervisor-workspaces"
    marker = tmp_path / "hook-or-filter-ran"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o700)
    git(source, "config", "filter.unsafe.smudge", f"touch {marker}")
    fallback_templates = tmp_path / "fallback-templates"
    (fallback_templates / "hooks").mkdir(parents=True)
    fallback_hook = fallback_templates / "hooks" / "post-checkout"
    fallback_hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    fallback_hook.chmod(0o700)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(fallback_templates))
    (source / "app.py").write_text("VALUE = 'dirty developer edit'\n", encoding="utf-8")
    (source / "developer-only.txt").write_text("keep in source checkout\n", encoding="utf-8")
    original_branch = git(source, "branch", "--show-current")
    original_head = git(source, "rev-parse", "HEAD")
    original_status = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    original_filter = git(source, "config", "--local", "--get", "filter.unsafe.smudge")
    monkeypatch.setattr(
        "dependabot_watcher.supervisor_workspace_provisioning._is_wsl",
        lambda: False,
    )

    created = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]
    workspace = Path(created.registration.directory)

    assert created.status == "created"
    assert created.base_sha == base_sha
    assert created.base_provenance == "github_collected_sha"
    assert created.registration.instruction_sources == (str(workspace / "AGENTS.md"),)
    assert git(source, "branch", "--show-current") == original_branch
    assert git(source, "rev-parse", "HEAD") == original_head
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all") == original_status
    assert git(source, "config", "--local", "--get", "filter.unsafe.smudge") == original_filter
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 'dirty developer edit'\n"
    assert (source / "developer-only.txt").read_text(encoding="utf-8") == "keep in source checkout\n"
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 'committed'\n"
    assert not (workspace / "developer-only.txt").exists()
    assert not marker.exists()
    assert (workspace / ".git").is_file()
    assert not (workspace.parent / "source-clone" / ".git" / "hooks" / "post-checkout").exists()
    assert git(workspace, "config", "--local", "--get", "remote.origin.url") == (
        f"https://github.com/{REPOSITORY}.git"
    )

    repository_directory = workspace.parent
    receipt = repository_directory / "ownership.json"
    assert stat.S_IMODE(repository_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "ready"
    config = _supervisor_config(workspace, created.registration.instruction_sources)
    proof = verify_supervisor_workspace(config, REPOSITORY, {
        "repository": REPOSITORY,
        "evidence_snapshot": {"base_sha": base_sha},
    })
    assert proof.head_sha == base_sha

    reused = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]
    assert reused.status == "reused"
    assert reused.registration == created.registration


def test_private_clone_copies_and_bounds_loose_and_packed_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    git(source, "update-ref", "refs/heads/feature", base_sha)
    git(source, "update-ref", "refs/remotes/upstream/main", base_sha)
    git(source, "update-ref", "refs/tags/v1", base_sha)
    git(source, "pack-refs", "--all", "--prune")
    common = Path(git(source, "rev-parse", "--absolute-git-dir"))
    packed_refs = common / "packed-refs"
    source_refs = set(git(source, "for-each-ref", "--format=%(refname)").splitlines())
    metadata_reserve = provisioning._ref_metadata_bytes(source, common, allocation_unit=4096)
    root = tmp_path / "private-state"
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)

    result = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]
    clone = Path(result.registration.directory).parent / "source-clone"
    clone_refs = set(git(clone, "for-each-ref", "--format=%(refname)").splitlines())

    assert packed_refs.is_file()
    assert metadata_reserve >= packed_refs.stat().st_size + (
        len(source_refs) * provisioning.CLONED_REF_METADATA_RESERVE
    )
    assert {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"} <= source_refs
    assert {"refs/remotes/origin/main", "refs/remotes/origin/feature", "refs/tags/v1"} <= clone_refs
    assert "refs/remotes/origin/main" in clone_refs


def test_reference_count_bound_refuses_before_private_workspace_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    git(source, "update-ref", "refs/heads/second", base_sha)
    root = tmp_path / "private-state"
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)
    monkeypatch.setattr(provisioning, "MAX_REF_METADATA_ENTRIES", 1)

    with pytest.raises(WorkspaceProvisioningError, match="reference count exceeds"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()


def test_provisioning_root_cannot_overlap_external_common_git_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha, common = separate_git_dir_repository(tmp_path / "fixture")
    root = common / "maintenance-workspaces"
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="source Git metadata directory"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()
    assert (source / ".git").is_file()
    assert Path(git(source, "rev-parse", "--absolute-git-dir")) == common


def test_interrupted_clone_recovery_rechecks_incremental_free_space_before_worktree_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)
    created = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]
    workspace = Path(created.registration.directory)
    clone = workspace.parent / "source-clone"
    git(clone, "worktree", "remove", "--force", str(workspace))
    receipt_path = workspace.parent / "ownership.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["status"] = "provisioning"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(provisioning, "_free_bytes", lambda _path: 0)

    with pytest.raises(WorkspaceProvisioningError, match="Insufficient free space"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert clone.is_dir()
    assert not workspace.exists()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "provisioning"


def test_unknown_existing_destination_is_preserved_without_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    workspace = workspace_directory_for(root, REPOSITORY)
    root.mkdir(mode=0o700)
    workspace.parent.mkdir(mode=0o700)
    private_file = workspace.parent / "user-data.txt"
    private_file.write_text("preserve\n", encoding="utf-8")
    head = git(source, "rev-parse", "HEAD")
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="no ownership receipt"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert private_file.read_text(encoding="utf-8") == "preserve\n"
    assert git(source, "rev-parse", "HEAD") == head


def test_missing_online_base_fails_before_creating_workspace_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="does not acquire missing commits"):
        provision_supervisor_workspaces(
            [request(source, root, "f" * 40)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()


def test_partial_clone_source_is_refused_before_object_lookup_or_workspace_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    git(source, "config", "--local", "extensions.partialClone", "origin")
    git(source, "config", "--local", "remote.origin.promisor", "true")
    root = tmp_path / "private-state"
    original_status = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    original_partial_clone = git(source, "config", "--local", "--get", "extensions.partialClone")
    calls: list[tuple[str, ...]] = []
    real_git = provisioning._git

    def recording_git(directory: Path | None, *arguments: str, **kwargs: Any) -> bytes:
        calls.append(arguments)
        return real_git(directory, *arguments, **kwargs)

    monkeypatch.setattr(provisioning, "_git", recording_git)
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="Partial-clone source checkouts are unsupported"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all") == original_status
    assert git(source, "config", "--local", "--get", "extensions.partialClone") == original_partial_clone
    assert all("fetch" not in arguments for arguments in calls)


def test_workspace_size_limit_fails_before_creating_workspace_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="above the configured workspace limit"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=1024,
            minimum_free_bytes=1,
        )

    assert not root.exists()


def test_wsl_requires_an_existing_physical_reserve_before_workspace_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: True)

    with pytest.raises(WorkspaceProvisioningError, match="requires an explicit physical-volume reserve path"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()


def test_wsl_physical_reserve_accepts_readonly_world_writable_mount_without_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    reserve = tmp_path / "physical-volume"
    reserve.mkdir(mode=0o777)
    reserve.chmod(0o777)
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: True)
    monkeypatch.setattr(provisioning, "_free_bytes", lambda _path: 10**12)

    result = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
        reserve_paths=(reserve,),
    )[0]

    assert result.reserve_paths == (str(reserve),)
    assert stat.S_IMODE(reserve.stat().st_mode) == 0o777


def test_checkout_estimate_includes_allocation_rounding_and_per_entry_metadata() -> None:
    estimate = provisioning._checkout_allocation_estimate({
        "small.py": ("100644", "blob", "a" * 40, 1),
        "nested/large.py": ("100644", "blob", "b" * 40, 4097),
    }, allocation_unit=4096)

    assert estimate == 4096 + 12288 + 2 * provisioning.CHECKOUT_FILE_METADATA_RESERVE + (
        provisioning.CHECKOUT_DIRECTORY_METADATA_RESERVE
    )


def test_pinned_checkout_expansion_attributes_are_refused_before_private_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _base_sha = source_repository(tmp_path / "source")
    (source / ".gitattributes").write_text(
        "*.py working-tree-encoding=UTF-16LE\n",
        encoding="utf-8",
    )
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "Add expanding checkout encoding")
    base_sha = git(source, "rev-parse", "HEAD")
    root = tmp_path / "private-state"
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="working-tree-encoding"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()


def test_oversized_receipt_is_rejected_before_workspace_root_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)
    monkeypatch.setattr(provisioning, "MAX_RECEIPT_BYTES", 256)

    with pytest.raises(WorkspaceProvisioningError, match="receipt size bound"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()


def test_receipt_writer_defensively_refuses_oversized_serialization_before_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_directory = tmp_path / "owned"
    repository_directory.mkdir(mode=0o700)
    monkeypatch.setattr(provisioning, "MAX_RECEIPT_BYTES", 128)

    with pytest.raises(WorkspaceProvisioningError, match="receipt size bound"):
        provisioning._write_receipt(
            repository_directory,
            {"instruction_sources": ["x" * 256]},
        )

    assert not (repository_directory / "ownership.json").exists()
    assert not (repository_directory / ".ownership.json.tmp").exists()


def test_relative_external_agents_symlink_is_refused_before_workspace_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "AGENTS.md").write_text("Shared instructions\n", encoding="utf-8")
    source, base_sha = source_repository(tmp_path / "source", agents_target="../shared/AGENTS.md")
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    with pytest.raises(WorkspaceProvisioningError, match="relative external symlink"):
        provision_supervisor_workspaces(
            [request(source, root, base_sha)],
            root=root,
            offline=False,
            maximum_bytes=64 * 1024**2,
            minimum_free_bytes=1,
        )

    assert not root.exists()
    assert git(source, "rev-parse", "HEAD") == base_sha
    assert (source / "AGENTS.md").is_symlink()


def test_absolute_external_agents_symlink_is_preserved_and_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "shared-instructions" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text("Shared trusted policy\n", encoding="utf-8")
    source, base_sha = source_repository(tmp_path / "source", agents_target=str(target))
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    result = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]
    workspace = Path(result.registration.directory)

    assert (workspace / "AGENTS.md").is_symlink()
    assert os.readlink(workspace / "AGENTS.md") == str(target)
    assert result.registration.instruction_sources == (str(workspace / "AGENTS.md"),)


def test_empty_repository_instruction_chain_is_explicit_not_synthesized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base_sha = source_repository(tmp_path / "source")
    (source / "AGENTS.md").unlink()
    git(source, "rm", "AGENTS.md")
    git(source, "commit", "-m", "No repository instructions")
    base_sha = git(source, "rev-parse", "HEAD")
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    result = provision_supervisor_workspaces(
        [request(source, root, base_sha)],
        root=root,
        offline=False,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]

    assert result.discovered_instruction_sources == ()
    assert result.instruction_sources == ()
    assert result.runtime_instruction_discovery_verified is False
    assert not (Path(result.registration.directory) / "AGENTS.md").exists()


def test_offline_base_is_local_and_never_reported_as_network_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, local_head = source_repository(tmp_path / "source")
    root = tmp_path / "private-state"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    result = provision_supervisor_workspaces(
        [request(source, root, None)],
        root=root,
        offline=True,
        maximum_bytes=64 * 1024**2,
        minimum_free_bytes=1,
    )[0]

    assert result.base_sha == local_head
    assert result.base_provenance == "local_unverified_offline"


def test_setup_cli_persists_workspace_and_reports_offline_base_as_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, local_head = source_repository(tmp_path / "source")
    config_path = tmp_path / "watcher.toml"
    root = tmp_path / "private-state" / "workspaces"
    monkeypatch.setattr("dependabot_watcher.supervisor_workspace_provisioning._is_wsl", lambda: False)

    result = main([
        "setup",
        "--repo", REPOSITORY,
        "--config", str(config_path),
        "--offline",
        "--format", "json",
        "--supervisor-mode", "approval_first",
        "--supervisor-model", "selected-test-model",
        "--supervisor-reasoning", "low",
        "--supervisor-account", "chatgpt",
        "--supervisor-access", "inherit",
        "--acknowledge-supervisor-use",
        "--supervisor-provision", f"{REPOSITORY}={source}",
        "--supervisor-workspace-root", str(root),
        "--supervisor-workspace-max-bytes", str(64 * 1024**2),
    ])
    report = json.loads(capsys.readouterr().out)
    saved = load_config(config_path)
    workspace = Path(saved.supervisor.workspaces[0].directory)

    assert result == 0
    assert report["verification"]["mode"] == "offline"
    assert report["verification"]["github_authentication"] == "skipped"
    assert report["verification"]["provisioned_workspace_bases"][REPOSITORY]["status"] == (
        "local_unverified_offline"
    )
    assert report["supervisor"]["workspace_verified"] is False
    assert report["supervisor"]["workspace_provisioning"][0]["base_sha"] == local_head
    assert report["supervisor"]["workspace_provisioning"][0]["retention"].startswith("persistent")
    assert report["supervisor"]["workspace_provisioning"][0]["runtime_instruction_discovery_verified"] is False
    assert saved.supervisor.workspaces[0].instruction_sources == (str(workspace / "AGENTS.md"),)
    assert workspace.is_dir()


def test_cli_rejects_duplicate_provision_choices_before_filesystem_side_effects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _base_sha = source_repository(tmp_path / "source")
    config_path = tmp_path / "watcher.toml"
    root = tmp_path / "private-state" / "workspaces"

    result = main([
        "setup",
        "--repo", REPOSITORY,
        "--config", str(config_path),
        "--offline",
        "--supervisor-mode", "approval_first",
        "--supervisor-access", "inherit",
        "--supervisor-provision", f"{REPOSITORY}={source}",
        "--supervisor-provision", f"{REPOSITORY}={source}",
        "--supervisor-workspace-root", str(root),
    ])
    captured = capsys.readouterr()

    assert result != 0
    assert "do not mix duplicate registrations" in captured.err
    assert not config_path.exists()
    assert not root.exists()


@pytest.mark.parametrize("output_location", ("source", "source_alias", "store"))
def test_setup_rejects_configuration_output_overlapping_provisioning_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_location: str,
) -> None:
    source, _base_sha = source_repository(tmp_path / "source")
    root = tmp_path / "private-state" / "supervisor-workspaces"
    workspace = workspace_directory_for(root, REPOSITORY)
    if output_location == "source":
        output = source / "generated-config.toml"
    elif output_location == "source_alias":
        alias = tmp_path / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        output = alias / "generated-config.toml"
    else:
        output = workspace.parent / "generated-config.toml"
    request_value = SetupRequest(
        repositories=(REPOSITORY,),
        branches={},
        offline=True,
        supervisor=_setup_supervisor_config(),
        supervisor_provision_sources={REPOSITORY: source},
        supervisor_workspace_root=root,
    )
    original_status = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)

    with pytest.raises(SetupError, match="Configuration output path overlaps"):
        create_setup(output, request_value)

    assert not output.exists()
    assert not root.exists()
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all") == original_status


@pytest.mark.parametrize("overlap", ("configuration", "workspace_root"))
def test_setup_rejects_external_git_metadata_overlap_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
) -> None:
    source, _base_sha, common = separate_git_dir_repository(tmp_path / "fixture")
    root = common / "private-workspaces" if overlap == "workspace_root" else tmp_path / "private-state"
    output = common / "generated-config.toml" if overlap == "configuration" else tmp_path / "watcher.toml"
    request_value = SetupRequest(
        repositories=(REPOSITORY,),
        branches={},
        offline=True,
        supervisor=_setup_supervisor_config(),
        supervisor_provision_sources={REPOSITORY: source},
        supervisor_workspace_root=root,
    )
    monkeypatch.setattr(provisioning, "_is_wsl", lambda: False)

    with pytest.raises(SetupError, match="overlaps the source Git metadata directory"):
        create_setup(output, request_value)

    assert not output.exists()
    if overlap == "workspace_root":
        assert not root.exists()
    assert Path(git(source, "rev-parse", "--absolute-git-dir")) == common
