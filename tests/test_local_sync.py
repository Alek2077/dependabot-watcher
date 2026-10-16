from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dependabot_watcher import local_sync
from dependabot_watcher.config import load_config, parse_config
from dependabot_watcher.local_sync import LocalSyncError, confirm_sync, handle_sync_request, prepare_sync, recover_sync, selected_checkouts
from dependabot_watcher.review_queue import ReviewQueueError
from dependabot_watcher.runtime_state import load_private_json, save_private_json
from test_supervisor_workspace import git


class BranchClient:
    def __init__(self, sha: str):
        self.sha = sha
        self.reads: list[tuple[str, str]] = []

    def fetch_branch_head(self, repository: str, branch: str) -> str:
        self.reads.append((repository, branch))
        return self.sha


@pytest.fixture
def sync_case(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "checkout"
    path.mkdir()
    git(path, "init", "-b", "integration")
    git(path, "config", "user.name", "Synthetic Operator")
    git(path, "config", "user.email", "operator@example.invalid")
    git(path, "remote", "add", "origin", "https://github.com/example-org/example-app.git")
    (path / "app.txt").write_text("before\n")
    (path / ".gitignore").write_text("ignored.txt\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "Initial")
    old = git(path, "rev-parse", "HEAD")
    git(path, "switch", "-c", "remote-update")
    (path / "app.txt").write_text("after\n")
    git(path, "add", "app.txt")
    git(path, "commit", "-m", "Update")
    new = git(path, "rev-parse", "HEAD")
    git(path, "switch", "integration")
    config_path = tmp_path / "config.toml"
    config_path.write_text('schema_version = 1\n[[repositories]]\nname = "example-org/example-app"\n'
        'default_branch = "integration"\n[[repositories.local_sync]]\n'
        f'directory = {json.dumps(str(path))}\nbranch = "integration"\n')
    config_path.chmod(0o600)
    config = load_config(config_path)
    return dict(path=path, config=config, config_path=config_path, state=tmp_path / "state", old=old,
                new=new, client=BranchClient(new), target_id=selected_checkouts(config)[0]["target_id"])


def prepare(case: dict[str, Any]) -> dict[str, Any]:
    return prepare_sync(case["config"], case["config_path"], case["state"], case["client"], case["target_id"])


def confirm(case: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    return confirm_sync(case["config"], case["config_path"], case["state"], case["client"],
                        prepared["review_id"], prepared["approval_token"])


def test_fast_forward_is_preflighted_and_explicitly_confirmed(sync_case):
    case = sync_case
    before_refs = git(case["path"], "show-ref")
    prepared = prepare(case)
    assert prepared["status"] == "ready" and prepared["mutation_performed"] is False
    assert prepared["binding"]["commits"] == 1
    assert git(case["path"], "show-ref") == before_refs
    assert (case["path"] / "app.txt").read_text() == "before\n"
    result = confirm(case, prepared)
    assert result["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "HEAD") == case["new"]
    assert git(case["path"], "symbolic-ref", "HEAD") == "refs/heads/integration"
    assert (case["path"] / "app.txt").read_text() == "after\n"
    assert git(case["path"], "status", "--porcelain") == ""
    with pytest.raises(LocalSyncError, match="consumed"):
        confirm(case, prepared)
    assert prepare(case)["status"] == "up_to_date"


def test_receipt_order_survives_backward_clock_without_extending_approval(
    sync_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = sync_case
    first = prepare(case)
    confirm(case, first)
    first_record = load_private_json(case["state"], Path("local-sync.json"))["records"][first["review_id"]]
    git(case["path"], "switch", "remote-update")
    (case["path"] / "app.txt").write_text("third\n")
    git(case["path"], "commit", "-am", "Third revision")
    case["client"].sha = git(case["path"], "rev-parse", "HEAD")
    git(case["path"], "switch", "integration")
    earlier = datetime.fromisoformat(first_record["expires_at"]) - timedelta(hours=1)

    class CorrectedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return earlier.astimezone(tz)

    monkeypatch.setattr(local_sync, "datetime", CorrectedClock)
    second = prepare(case)
    records = load_private_json(case["state"], Path("local-sync.json"))["records"]
    assert records[second["review_id"]]["sequence"] > first_record["sequence"]
    assert datetime.fromisoformat(records[second["review_id"]]["expires_at"]) == earlier + timedelta(minutes=15)
    assert records[first["review_id"]] == first_record
    assert git(case["path"], "rev-parse", "HEAD") == case["new"]


@pytest.mark.parametrize("sequence", [0, True, 2**63])
def test_native_receipt_sequence_rejects_out_of_range_values(sync_case: dict[str, Any], sequence: object) -> None:
    case = sync_case
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    journal["records"][prepared["review_id"]]["sequence"] = sequence
    save_private_json(case["state"], Path("local-sync.json"), journal)
    with pytest.raises(LocalSyncError, match="invalid record"):
        handle_sync_request(case["config"], case["config_path"], case["state"], case["client"],
            {"schema_version": 1, "request_id": "sync_bounds", "operation": "local_sync_targets"})


def test_native_receipt_sequence_exhaustion_preserves_journal(sync_case: dict[str, Any]) -> None:
    case = sync_case
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    record = journal["records"][prepared["review_id"]]
    record.update(sequence=2**63 - 1, status="failed",
        result={"status": "observed_not_applied", "binding": record["binding"], "mutation_performed": False})
    save_private_json(case["state"], Path("local-sync.json"), journal)
    with pytest.raises(LocalSyncError, match="sequence is exhausted"):
        prepare(case)
    assert load_private_json(case["state"], Path("local-sync.json")) == journal
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


@pytest.mark.parametrize("count", [21, 100])
def test_bounded_receipt_history_uses_causal_order_not_random_ids(sync_case: dict[str, Any], count: int) -> None:
    case = sync_case
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    prototype = journal["records"].pop(prepared["review_id"])
    identities = []
    for index in range(count):
        identity = "sync_" + f"{count-index:032x}"
        identities.append(identity)
        record = copy.deepcopy(prototype)
        record.update(sequence=index + 1, status="failed",
            result={"status": "observed_not_applied", "binding": record["binding"], "mutation_performed": False})
        journal["records"][identity] = record
    save_private_json(case["state"], Path("local-sync.json"), journal)
    observations = local_sync.synchronization_observations(case["config"], case["state"], "example-org/example-app")
    assert [item["review_id"] for item in observations] == identities[-20:]
    if count == 100:
        newest = prepare(case)
        remaining = load_private_json(case["state"], Path("local-sync.json"))["records"]
        assert identities[0] not in remaining and identities[-1] in remaining
        assert remaining[newest["review_id"]]["sequence"] == count + 1


def configure_tracking(case: dict[str, Any], *, missing: bool = False) -> None:
    git(case["path"], "config", "branch.integration.remote", "origin")
    git(case["path"], "config", "branch.integration.merge", "refs/heads/integration")
    if not missing:
        git(case["path"], "update-ref", "refs/remotes/origin/integration", case["old"])


@pytest.mark.parametrize("missing", [False, True])
def test_approved_sync_refreshes_only_matching_tracking_reference(sync_case, missing):
    case = sync_case
    configure_tracking(case, missing=missing)
    git(case["path"], "update-ref", "refs/remotes/origin/unrelated", case["old"])
    prepared = prepare(case)
    assert prepared["binding"]["tracking_refresh"]["old_sha"] == (None if missing else case["old"])
    assert confirm(case, prepared)["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["new"]
    assert git(case["path"], "rev-parse", "refs/remotes/origin/unrelated") == case["old"]
    assert not (case["path"] / ".git" / "FETCH_HEAD").exists()


def test_current_branch_stale_tracking_requires_exact_confirmation(sync_case):
    case = sync_case
    configure_tracking(case)
    git(case["path"], "reset", "--hard", case["new"])
    prepared = prepare(case)
    assert prepared["status"] == "ready" and prepared["binding"]["commits"] == 0
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["old"]
    assert confirm(case, prepared)["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["new"]
    assert prepare(case)["status"] == "up_to_date"


@pytest.mark.parametrize("change", ["ref", "config", "symbolic"])
def test_tracking_change_after_review_preserves_checkout(sync_case, change):
    case = sync_case
    configure_tracking(case)
    prepared = prepare(case)
    if change == "config":
        git(case["path"], "config", "branch.integration.merge", "refs/heads/other")
    elif change == "symbolic":
        git(case["path"], "symbolic-ref", "refs/remotes/origin/integration", "refs/heads/remote-update")
    else:
        git(case["path"], "update-ref", "refs/remotes/origin/integration", case["new"])
    with pytest.raises(LocalSyncError):
        confirm(case, prepared)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    assert (case["path"] / "app.txt").read_text() == "before\n"


def test_legacy_saved_approval_does_not_gain_tracking_authority(sync_case):
    from dependabot_watcher import local_sync
    case = sync_case
    configure_tracking(case)
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    binding = journal["records"][prepared["review_id"]]["binding"]
    del binding["tracking_refresh"]
    save_private_json(case["state"], Path("local-sync.json"), journal)
    prepared["approval_token"] = local_sync._token(prepared["review_id"], binding)
    assert confirm(case, prepared)["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["old"]


def test_other_upstream_is_not_reconfigured_or_refreshed(sync_case):
    case = sync_case
    configure_tracking(case)
    git(case["path"], "config", "branch.integration.merge", "refs/heads/other")
    prepared = prepare(case)
    assert prepared["binding"]["tracking_refresh"] is None
    assert confirm(case, prepared)["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["old"]
    assert git(case["path"], "config", "branch.integration.merge") == "refs/heads/other"


def test_tracking_lock_refusal_is_atomic_and_observed_without_replay(sync_case):
    case = sync_case
    configure_tracking(case)
    prepared = prepare(case)
    lock = case["path"] / ".git/refs/remotes/origin/integration.lock"
    lock.write_text("another owner")
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    assert lock.read_text() == "another owner"
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["old"]
    assert recover_sync(None, case["config_path"], case["state"], prepared["review_id"])["result"]["status"] == "observed_not_applied"


def test_symbolic_tracking_race_cannot_redirect_transaction(sync_case):
    case = sync_case
    configure_tracking(case)
    prepared = prepare(case)
    class RacingClient(BranchClient):
        def fetch_branch_head(self, repository, branch):
            result = super().fetch_branch_head(repository, branch)
            if len(self.reads) == 2:
                git(case["path"], "symbolic-ref", "refs/remotes/origin/integration", "refs/heads/remote-update")
            return result
    case["client"] = RacingClient(case["new"])
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    assert git(case["path"], "rev-parse", "refs/heads/remote-update") == case["new"]
    assert (case["path"] / "app.txt").read_text() == "before\n"


@pytest.mark.parametrize("applied", [False, True])
def test_tracking_only_recovery_observes_saved_intent(sync_case, applied):
    case = sync_case
    configure_tracking(case)
    git(case["path"], "reset", "--hard", case["new"])
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    journal["records"][prepared["review_id"]]["status"] = "executing"
    save_private_json(case["state"], Path("local-sync.json"), journal)
    if applied:
        git(case["path"], "update-ref", "refs/remotes/origin/integration", case["new"])
    before = git(case["path"], "show-ref")
    result = recover_sync(None, case["config_path"], case["state"], prepared["review_id"])
    assert result["result"]["status"] == ("observed_applied" if applied else "observed_not_applied")
    assert git(case["path"], "show-ref") == before


def test_recovery_preserves_inconsistent_branch_tracking_outcome(sync_case):
    case = sync_case
    configure_tracking(case)
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    journal["records"][prepared["review_id"]]["status"] = "executing"
    save_private_json(case["state"], Path("local-sync.json"), journal)
    git(case["path"], "reset", "--hard", case["new"])
    result = recover_sync(None, case["config_path"], case["state"], prepared["review_id"])
    assert result["result"]["status"] == "manual_repair_required"
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == case["old"]


@pytest.mark.parametrize("capable", [False, True])
def test_native_preview_requires_tracking_disclosure_capability(sync_case, capable):
    case = sync_case
    configure_tracking(case)
    request = {"schema_version": 1, "request_id": "sync_preview", "operation": "prepare_local_sync",
               "target_id": case["target_id"]}
    if capable:
        request["tracking_refresh_supported"] = True
    result = handle_sync_request(case["config"], case["config_path"], case["state"], case["client"], request)
    binding = result["review"]["binding"]
    assert ("tracking_refresh" in binding) is capable
    assert confirm(case, result["review"])["status"] == "synchronized"
    assert git(case["path"], "rev-parse", "refs/remotes/origin/integration") == (case["new"] if capable else case["old"])


@pytest.mark.parametrize("tracking_only", [False, True])
def test_cli_preview_discloses_exact_tracking_mutation(sync_case, monkeypatch, capsys, tracking_only):
    from dependabot_watcher import cli
    case = sync_case
    configure_tracking(case)
    if tracking_only:
        git(case["path"], "reset", "--hard", case["new"])
    monkeypatch.setattr(cli, "GhCliClient", lambda **kwargs: case["client"])
    assert cli.main(["sync", "--repo", "example-org/example-app", "--config", str(case["config_path"]),
                     "--state-dir", str(case["state"])]) == 0
    output = capsys.readouterr().out
    assert "refs/remotes/origin/integration" in output
    assert case["old"] in output and case["new"] in output
    assert "No other remote-tracking references" in output
    assert "(0 commits)" in output if tracking_only else "(1 commits)" in output
    assert git(case["path"], "rev-parse", "origin/integration") == case["old"]


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked", "branch", "divergent", "origin", "skip", "filter", "sparse"])
def test_unsupported_checkout_is_preserved(sync_case, change):
    case, path = sync_case, sync_case["path"]
    if change in {"tracked", "staged", "divergent"}:
        (path / "app.txt").write_text("Preserve this work\n")
        if change != "tracked":
            git(path, "add", "app.txt")
        if change == "divergent":
            git(path, "commit", "-m", "Local work")
    elif change == "untracked":
        (path / "work.txt").write_text("Keep me\n")
    elif change == "branch":
        git(path, "switch", "remote-update")
    elif change == "origin":
        git(path, "remote", "set-url", "origin", "https://github.com/example-org/wrong.git")
    elif change == "skip":
        git(path, "update-index", "--skip-worktree", "app.txt")
    elif change == "filter":
        git(path, "config", "filter.danger.smudge", "false")
    else:
        git(path, "config", "core.sparseCheckout", "true")
    before = git(path, "show-ref"), (path / "app.txt").read_bytes()
    with pytest.raises(ReviewQueueError):
        prepare(case)
    assert (git(path, "show-ref"), (path / "app.txt").read_bytes()) == before


@pytest.mark.parametrize("change", ["tracked", "branch", "remote", "config"])
def test_stale_approval_cannot_integrate(sync_case, change):
    case = sync_case
    prepared = prepare(case)
    if change == "tracked":
        (case["path"] / "app.txt").write_text("Keep this\n")
    elif change == "branch":
        git(case["path"], "switch", "remote-update")
    elif change == "remote":
        case["client"].sha = case["old"]
    else:
        case["config_path"].write_text('schema_version=1\n[[repositories]]\nname="example-org/example-app"\n')
    before = git(case["path"], "show-ref"), (case["path"] / "app.txt").read_bytes()
    with pytest.raises(ReviewQueueError):
        confirm(case, prepared)
    assert (git(case["path"], "show-ref"), (case["path"] / "app.txt").read_bytes()) == before


def test_ignored_collision_is_not_overwritten(sync_case):
    case, path = sync_case, sync_case["path"]
    git(path, "switch", "remote-update")
    (path / "ignored.txt").write_text("New tracked remote file\n")
    git(path, "add", "-f", "ignored.txt")
    git(path, "commit", "-m", "Track formerly ignored file")
    case["client"].sha = git(path, "rev-parse", "HEAD")
    git(path, "switch", "integration")
    (path / "ignored.txt").write_text("Keep local ignored content\n")
    with pytest.raises(ReviewQueueError):
        prepare(case)
    assert (path / "ignored.txt").read_text() == "Keep local ignored content\n"
    assert git(path, "rev-parse", "HEAD") == case["old"]


def test_failed_attempt_is_not_replayed_and_recovery_only_observes(sync_case, monkeypatch):
    case = sync_case
    prepared = prepare(case)
    def failed(*args):
        raise LocalSyncError("Synthetic failure before integration")
    monkeypatch.setattr("dependabot_watcher.local_sync._fast_forward", failed)
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    with pytest.raises(LocalSyncError):
        confirm(case, prepared)
    result = recover_sync(case["config"], case["config_path"], case["state"], prepared["review_id"])
    assert result["result"]["status"] == "observed_not_applied"
    assert result["mutation_performed"] is False
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_recovery_observes_completed_integration(sync_case):
    case = sync_case
    prepared = prepare(case)
    confirm(case, prepared)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    journal["records"][prepared["review_id"]]["status"] = "executing"
    journal["records"][prepared["review_id"]]["result"] = None
    save_private_json(case["state"], Path("local-sync.json"), journal)
    refs = git(case["path"], "show-ref")
    result = recover_sync(case["config"], case["config_path"], case["state"], prepared["review_id"])
    assert result["result"]["status"] == "observed_applied"
    assert git(case["path"], "show-ref") == refs


def test_old_profiles_remain_remote_only():
    config = parse_config({"schema_version": 1, "repositories": [{"name": "example-org/example-app"}]})
    assert selected_checkouts(config) == []


def test_reference_transaction_holds_branch_and_head_locks(sync_case, monkeypatch):
    from dependabot_watcher import local_sync
    case = sync_case
    prepared = prepare(case)
    original = local_sync._git
    observed = []
    def checked(path, *args, **kwargs):
        if args[:3] == ("read-tree", "-m", "-u"):
            observed.append(True)
            assert (path / ".git" / "HEAD.lock").is_file()
            assert (path / ".git" / "refs" / "heads" / "integration.lock").is_file()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(local_sync, "_git", checked)
    assert confirm(case, prepared)["status"] == "synchronized"
    assert observed == [True]


def test_prepared_transaction_failure_releases_git_locks(sync_case, monkeypatch):
    from dependabot_watcher import local_sync
    case = sync_case
    prepared = prepare(case)
    original = local_sync._git
    def refused(path, *args, **kwargs):
        if args[:3] == ("read-tree", "-m", "-u"):
            raise LocalSyncError("Synthetic integration refusal")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(local_sync, "_git", refused)
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    assert not (case["path"] / ".git" / "HEAD.lock").exists()
    assert not (case["path"] / ".git" / "refs" / "heads" / "integration.lock").exists()
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    assert (case["path"] / "app.txt").read_text() == "before\n"


@pytest.mark.parametrize("field,value", [("target_sha", "invalid"), ("commits", True),
                                         ("directory_identity", [1]), ("directory", "relative")])
def test_malformed_saved_binding_is_refused(sync_case, field, value):
    case = sync_case
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    journal["records"][prepared["review_id"]]["binding"][field] = value
    save_private_json(case["state"], Path("local-sync.json"), journal)
    with pytest.raises(LocalSyncError, match="malformed"):
        confirm(case, prepared)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_saved_git_directory_cannot_redirect_recovery_writes(sync_case, tmp_path):
    case = sync_case
    prepared = prepare(case)
    other = tmp_path / "unrelated"
    other.mkdir()
    journal = load_private_json(case["state"], Path("local-sync.json"))
    journal["records"][prepared["review_id"]]["binding"]["git_directory"] = str(other)
    save_private_json(case["state"], Path("local-sync.json"), journal)
    with pytest.raises(LocalSyncError, match="identity changed"):
        recover_sync(case["config"], case["config_path"], case["state"], prepared["review_id"])
    assert list(other.iterdir()) == []


def test_native_channel_requires_exact_registered_target_and_separate_confirmation(sync_case):
    case = sync_case
    def exchange(operation, **fields):
        return handle_sync_request(case["config"], case["config_path"], case["state"], case["client"],
            {"schema_version": 1, "request_id": "sync_test", "operation": operation, **fields})
    assert exchange("local_sync_targets")["targets"][0]["target_id"] == case["target_id"]
    review = exchange("prepare_local_sync", target_id=case["target_id"])["review"]
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]
    with pytest.raises(LocalSyncError, match="bounded request"):
        exchange("confirm_local_sync", review_id=review["review_id"])
    with pytest.raises(LocalSyncError, match="bounded request"):
        exchange("prepare_local_sync", target_id=case["target_id"], directory=str(case["path"]))
    result = exchange("confirm_local_sync", review_id=review["review_id"], approval_token=review["approval_token"])
    assert result["review"]["status"] == "synchronized"


def test_supervisor_receives_historical_local_outcome_not_live_readiness(sync_case):
    from dependabot_watcher.review_queue import ReviewQueue
    from dependabot_watcher.supervision import _validate_context
    from test_execution import saved_plan
    from test_review_queue import persist_plan
    case = sync_case
    queue = ReviewQueue(case["config"], case["state"], config_path=case["config_path"])
    persist_plan(queue.directory, saved_plan())
    prepared = prepare(case)
    confirm(case, prepared)
    context = queue.supervision_context("example-org/example-app")
    _validate_context(context)
    observation = context["local_sync_observations"][0]
    assert observation["outcome"] == "synchronized"
    assert observation["applicability"] == "historical_recheck_before_use"
    assert "approval_token" not in observation
    assert context["mutation_authorized"] is False
    observation["directory"] = "/unregistered"
    with pytest.raises(ReviewQueueError, match="out of scope"):
        _validate_context(context)


def test_git_replacement_objects_cannot_change_approved_tree(sync_case):
    case = sync_case
    git(case["path"], "replace", case["new"], case["old"])
    prepared = prepare(case)
    assert confirm(case, prepared)["status"] == "synchronized"
    assert (case["path"] / "app.txt").read_text() == "after\n"
    assert git(case["path"], "--no-replace-objects", "status", "--porcelain") == ""


@pytest.mark.parametrize("kind", ["graft", "linked"])
def test_grafted_or_shared_checkout_is_not_integrated(sync_case, tmp_path, kind):
    case = sync_case
    if kind == "graft":
        (case["path"] / ".git" / "info" / "grafts").write_text(case["old"] + "\n")
    else:
        git(case["path"], "worktree", "add", "--detach", str(tmp_path / "linked"), case["old"])
    with pytest.raises(LocalSyncError, match="graft|worktrees"):
        prepare(case)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


@pytest.mark.parametrize("control", ["config", "state"])
def test_control_paths_cannot_overlap_selected_checkout(sync_case, control):
    case = sync_case
    if control == "config":
        inside = case["path"] / ".git" / "watcher.toml"
        inside.write_bytes(case["config_path"].read_bytes())
        inside.chmod(0o600)
        case["config_path"] = inside
    else:
        case["state"] = case["path"] / "watcher-state"
    with pytest.raises(LocalSyncError, match="separate"):
        prepare(case)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_recovery_survives_removed_configuration(sync_case, monkeypatch, capsys):
    from dependabot_watcher import cli, local_sync
    case = sync_case
    prepared = prepare(case)
    def fail(*arguments):
        raise LocalSyncError("Synthetic refusal")
    monkeypatch.setattr(local_sync, "_fast_forward", fail)
    with pytest.raises(LocalSyncError):
        confirm(case, prepared)
    case["config_path"].unlink()
    assert cli.main(["sync", "--config", str(case["config_path"]), "--state-dir", str(case["state"]),
                     "--review-id", prepared["review_id"], "--recover", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "observed_not_applied"


def test_provider_failure_with_prepared_git_transaction_is_recoverable(sync_case):
    from dependabot_watcher.github import GitHubError
    case = sync_case
    class FailingBranch(BranchClient):
        def fetch_branch_head(self, repository, branch):
            result = super().fetch_branch_head(repository, branch)
            if len(self.reads) == 4:
                raise GitHubError("Synthetic transport failure")
            return result
    case["client"] = FailingBranch(case["new"])
    prepared = prepare(case)
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    assert journal["records"][prepared["review_id"]]["status"] == "recovery_required"
    assert not (case["path"] / ".git" / "HEAD.lock").exists()
    assert recover_sync(None, case["config_path"], case["state"], prepared["review_id"])["result"]["status"] == "observed_not_applied"


def test_applied_attempt_remains_observable_after_registration_removed(sync_case, monkeypatch):
    from dependabot_watcher import local_sync
    case = sync_case
    prepared = prepare(case)
    original = local_sync._fast_forward
    def apply_then_change(binding, check):
        original(binding, check)
        case["config_path"].write_text('schema_version=1\n[[repositories]]\nname="example-org/example-app"\n')
    monkeypatch.setattr(local_sync, "_fast_forward", apply_then_change)
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    changed = load_config(case["config_path"])
    listing = handle_sync_request(changed, case["config_path"], case["state"], case["client"],
        {"schema_version": 1, "request_id": "sync_orphan", "operation": "local_sync_targets"})
    assert listing["targets"][0]["target_id"] == case["target_id"]
    assert listing["reviews"][0]["status"] == "recovery_required"
    result = recover_sync(None, case["config_path"], case["state"], prepared["review_id"])
    assert result["result"]["status"] == "observed_applied"


def test_post_integration_identity_drift_is_not_reported_as_success(sync_case, monkeypatch):
    from dependabot_watcher import local_sync
    case = sync_case
    prepared = prepare(case)
    original = local_sync._fast_forward
    def apply_then_change(binding, check):
        original(binding, check)
        git(case["path"], "config", "core.eol", "crlf")
    monkeypatch.setattr(local_sync, "_fast_forward", apply_then_change)
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    assert journal["records"][prepared["review_id"]]["status"] == "recovery_required"


def test_missing_object_fetch_cannot_use_rewritten_file_transport_or_repo_helper(sync_case, tmp_path):
    case = sync_case
    marker = tmp_path / "helper-was-run"
    git(case["path"], "config", "credential.helper", "!touch " + str(marker))
    git(case["path"], "config", "url.file:///unavailable-synthetic/.insteadOf", "https://github.com/")
    case["client"].sha = "f" * 40
    with pytest.raises(ReviewQueueError):
        prepare(case)
    assert not marker.exists()
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


@pytest.mark.parametrize("setting,value", [
    ("url.https://untrusted.invalid/.insteadOf", "https://github.com/"),
    ("http.https://github.com/example-org/.sslVerify", "false"),
])
def test_missing_object_fetch_rejects_https_rewrite_and_scoped_tls_override(sync_case, setting, value):
    case = sync_case
    git(case["path"], "config", setting, value)
    case["client"].sha = "f" * 40
    with pytest.raises(LocalSyncError, match="rewrites|TLS"):
        prepare(case)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


@pytest.mark.parametrize("source", ["info", "configured"])
def test_external_attribute_sources_are_not_silently_used(sync_case, tmp_path, source):
    case = sync_case
    if source == "info":
        (case["path"] / ".git" / "info" / "attributes").write_text("*.txt eol=crlf\n")
    else:
        attributes = tmp_path / "attributes"
        attributes.write_text("*.txt eol=crlf\n")
        git(case["path"], "config", "core.attributesFile", str(attributes))
    with pytest.raises(LocalSyncError, match="attributes"):
        prepare(case)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_recovery_refuses_state_inside_checkout_before_creating_lock(sync_case):
    case = sync_case
    prepared = prepare(case)
    inside = case["path"] / "private-state"
    inside.mkdir(mode=0o700)
    journal = inside / "local-sync.json"
    journal.write_bytes((case["state"] / "local-sync.json").read_bytes())
    journal.chmod(0o600)
    before = inside.stat().st_mode
    with pytest.raises(LocalSyncError, match="separate"):
        recover_sync(None, case["config_path"], inside, prepared["review_id"])
    assert inside.stat().st_mode == before
    assert not (inside / "run.lock").exists()


def test_recovery_bridge_needs_neither_configuration_nor_live_transport(sync_case, monkeypatch):
    from io import BytesIO
    from dependabot_watcher import local_sync, review_bridge
    case = sync_case
    prepared = prepare(case)
    def fail(*arguments):
        raise LocalSyncError("Synthetic refusal")
    monkeypatch.setattr(local_sync, "_fast_forward", fail)
    with pytest.raises(LocalSyncError):
        confirm(case, prepared)
    case["config_path"].unlink()
    for operation, extra in [("local_sync_targets", {}), ("recover_local_sync", {"review_id": prepared["review_id"]})]:
        outgoing = BytesIO()
        incoming = BytesIO(json.dumps({"schema_version": 1, "request_id": "sync_recovery", "operation": operation, **extra}).encode())
        assert review_bridge.exchange(None, incoming, outgoing, recovery_directory=case["state"]) == 0
        response = json.loads(outgoing.getvalue())
        assert response["status"] == "ok"
        if operation == "local_sync_targets":
            assert response["targets"][0]["target_id"] == case["target_id"]
        else:
            assert response["review"]["result"]["status"] == "observed_not_applied"


@pytest.mark.parametrize("source", ["partial", "promisor", "filter", "alternates", "http-alternates", "pack"])
def test_external_object_sources_are_refused_before_status_or_fetch(sync_case, monkeypatch, source):
    from dependabot_watcher import local_sync
    case, path = sync_case, sync_case["path"]
    settings = {"partial": "extensions.partialClone", "promisor": "remote.origin.promisor",
                "filter": "remote.origin.partialCloneFilter"}
    if source in settings:
        git(path, "config", settings[source], "true")
    elif source == "pack":
        (path / ".git" / "objects" / "pack" / "synthetic.promisor").touch()
    else:
        (path / ".git" / "objects" / "info" / source).write_text("/unavailable-synthetic-objects\n")
    original = local_sync._git
    def guarded(directory, *arguments, **keywords):
        assert arguments[0] not in {"status", "read-tree", "fetch", "cat-file"}
        return original(directory, *arguments, **keywords)
    monkeypatch.setattr(local_sync, "_git", guarded)
    with pytest.raises(LocalSyncError, match="objects"):
        prepare(case)
    assert case["client"].reads == []
    assert (path / "app.txt").read_text() == "before\n"


@pytest.mark.parametrize("setting", ["http.sslCAInfo", "http.sslCAPath", "http.extraHeader",
    "http.proxy", "http.https://github.com/.sslCAInfo", "http.https://github.com/.sslCAPath",
    "http.https://github.com/.proxy", "http.https://github.com/.followRedirects",
    "http.cookieFile", "http.saveCookies", "http.https://github.com/.cookieFile"])
def test_missing_object_fetch_rejects_credential_transport_overrides(sync_case, monkeypatch, setting):
    from dependabot_watcher import local_sync
    case = sync_case
    git(case["path"], "config", setting, "synthetic-untrusted-setting")
    case["client"].sha = "f" * 40
    original = local_sync._git
    def guarded(directory, *arguments, **keywords):
        assert arguments[0] != "fetch", "Credential-bearing fetch must not start"
        return original(directory, *arguments, **keywords)
    monkeypatch.setattr(local_sync, "_git", guarded)
    with pytest.raises(LocalSyncError, match="TLS|CA|proxy"):
        prepare(case)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_repeated_previews_supersede_unused_approval_without_exhausting_history(sync_case):
    case = sync_case
    previous = prepare(case)
    current = previous
    for _ in range(101):
        current = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    assert list(journal["records"]) == [current["review_id"]]
    with pytest.raises(LocalSyncError, match="absent|consumed"):
        confirm(case, previous)
    assert confirm(case, current)["status"] == "synchronized"


def test_expired_previews_for_other_targets_do_not_exhaust_history(sync_case):
    case = sync_case
    prepared = prepare(case)
    journal = load_private_json(case["state"], Path("local-sync.json"))
    record = journal["records"].pop(prepared["review_id"])
    record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    record["binding"]["target_id"] = "e" * 32
    journal["records"] = {"sync_" + f"{index:032x}": record for index in range(100)}
    save_private_json(case["state"], Path("local-sync.json"), journal)
    current = prepare(case)
    assert len(load_private_json(case["state"], Path("local-sync.json"))["records"]) == 1
    assert confirm(case, current)["status"] == "synchronized"


@pytest.mark.parametrize("entrypoint", ["queue", "reconcile"])
@pytest.mark.parametrize("external_metadata", [False, True])
def test_config_bound_state_writers_preserve_checkout_and_metadata(sync_case, tmp_path, entrypoint, external_metadata):
    from dependabot_watcher.review_queue import ReviewQueue
    from dependabot_watcher.reconciliation import reconcile
    case = sync_case
    root = case["path"]
    if external_metadata:
        root = tmp_path / "external-git"
        git(case["path"], "init", "--separate-git-dir", str(root))
    inside = root / "watcher-state"
    inside.mkdir(mode=0o755)
    before = inside.stat().st_mode
    with pytest.raises(LocalSyncError, match="separate"):
        if entrypoint == "queue":
            ReviewQueue(case["config"], inside, config_path=case["config_path"])
        else:
            reconcile(case["config"], case["client"], config_path=case["config_path"], state_directory=inside)
    assert inside.stat().st_mode == before
    assert list(inside.iterdir()) == []
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_setup_does_not_write_configuration_into_external_git_metadata(sync_case, tmp_path):
    from dependabot_watcher.setup import SetupError, SetupRequest, setup
    from dependabot_watcher.config import LocalSyncCheckout
    case = sync_case
    metadata = tmp_path / "external-git"
    git(case["path"], "init", "--separate-git-dir", str(metadata))
    before = metadata.stat().st_mode
    output = metadata / "watcher.toml"
    with pytest.raises(SetupError, match="separate"):
        setup(output, SetupRequest(repositories=("example-org/example-app",),
            branches={"example-org/example-app": "integration"}, offline=True,
            acknowledge_local_sync=True,
            local_sync={"example-org/example-app": (LocalSyncCheckout(str(case["path"]), "integration"),)}))
    assert not output.exists()
    assert metadata.stat().st_mode == before
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "CURL_CA_BUNDLE", "https_proxy"])
def test_missing_object_fetch_refuses_environment_transport_customization(sync_case, monkeypatch, variable):
    case = sync_case
    case["client"].sha = "f" * 40
    monkeypatch.setenv(variable, "/synthetic-untrusted-transport")
    with pytest.raises(LocalSyncError, match="TLS|CA|proxy"):
        prepare(case)
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_scoped_credential_helper_and_cookie_file_are_not_used(sync_case, tmp_path):
    case = sync_case
    marker, cookie = tmp_path / "helper-ran", tmp_path / "cookies"
    cookie.write_text("Synthetic cookie bytes; not a real credential\n")
    before = cookie.read_bytes()
    git(case["path"], "config", "credential.https://github.com.helper", "!touch " + str(marker))
    git(case["path"], "config", "http.cookieFile", str(cookie))
    case["client"].sha = "f" * 40
    with pytest.raises(LocalSyncError, match="credential|cookie"):
        prepare(case)
    assert not marker.exists() and cookie.read_bytes() == before
    assert git(case["path"], "rev-parse", "HEAD") == case["old"]


def test_scoped_helpers_are_replaced_at_actual_git_credential_boundary(sync_case, tmp_path):
    from dependabot_watcher.local_sync import _fetch_credential_overrides
    case = sync_case
    marker, broker = tmp_path / "untrusted-helper-ran", tmp_path / "trusted-gh"
    broker.write_text("#!/bin/sh\nprintf 'username=synthetic\\npassword=synthetic-not-a-secret\\n'\n")
    broker.chmod(0o700)
    for key in ("credential.helper", "credential.https://github.com.helper",
                "credential.https://github.com/example-org.helper"):
        git(case["path"], "config", "--add", key, "!touch " + str(marker))
    overrides = _fetch_credential_overrides(case["path"], str(broker))
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    command = ["git", "-C", str(case["path"])]
    for override in overrides:
        command.extend(["-c", override])
    result = subprocess.run([*command, "credential", "fill"], env=environment,
        input="protocol=https\nhost=github.com\npath=example-org/example-app.git\n\n",
        capture_output=True, text=True, timeout=10, check=True)
    assert "password=synthetic-not-a-secret" in result.stdout
    assert not marker.exists(), "No inherited generic or URL-scoped helper may execute"
    broker.write_text("#!/bin/sh\nexit 0\n")
    unavailable = subprocess.run([*command, "credential", "fill"], env=environment,
        input="protocol=https\nhost=github.com\npath=example-org/example-app.git\n\n",
        capture_output=True, text=True, timeout=10)
    assert unavailable.returncode != 0
    assert not marker.exists(), "Broker unavailability must not fall back to an inherited helper"


@pytest.mark.parametrize("scope", ["https://github.com/\ncore.fsmonitor", "https://github.com/ambiguous=value"])
def test_malformed_helper_scope_is_refused_as_one_nul_delimited_key(sync_case, monkeypatch, scope):
    from dependabot_watcher import local_sync
    # Git rejects literal-newline keys at config-write time. Inject malformed
    # protocol data to exercise our boundary without claiming a valid Git file.
    monkeypatch.setattr(local_sync, "_git", lambda *args, **kwargs: f"credential.{scope}.helper\0")
    with pytest.raises(LocalSyncError, match="scope"):
        local_sync._fetch_credential_overrides(sync_case["path"], "/supported/gh")


def test_execution_preflight_rejects_checkout_state_before_loading_or_chmod(sync_case, monkeypatch):
    from dependabot_watcher import execution
    case = sync_case
    inside = case["path"] / ".git" / "watcher-state"
    inside.mkdir(mode=0o755)
    before = inside.stat().st_mode
    monkeypatch.setattr(execution, "load_run_state", lambda *args: pytest.fail("Unsafe state read before path guard"))
    with pytest.raises(LocalSyncError, match="separate"):
        execution.preflight_execution(case["config"], case["client"], {}, "synthetic_step",
            state_directory=inside, config_path=case["config_path"])
    assert inside.stat().st_mode == before and list(inside.iterdir()) == []


def test_partial_worktree_outcome_is_saved_for_manual_repair_without_replay(sync_case, monkeypatch, capsys):
    from dependabot_watcher import cli, local_sync
    case = sync_case
    prepared = prepare(case)
    def partial_write(binding, check):
        check()
        # Actual index/worktree update, then refusal before the ref commit.
        git(case["path"], "read-tree", "-m", "-u", case["old"], case["new"])
        raise LocalSyncError("Controlled failure after worktree update")
    monkeypatch.setattr(local_sync, "_fast_forward", partial_write)
    with pytest.raises(LocalSyncError, match="outcome needs"):
        confirm(case, prepared)
    before = git(case["path"], "show-ref"), git(case["path"], "status", "--porcelain"), (case["path"] / "app.txt").read_bytes()
    recovered = recover_sync(None, case["config_path"], case["state"], prepared["review_id"])
    assert recovered["status"] == "recovery_required"
    assert recovered["result"]["status"] == "manual_repair_required"
    assert recovered["mutation_performed"] is False
    assert (git(case["path"], "show-ref"), git(case["path"], "status", "--porcelain"), (case["path"] / "app.txt").read_bytes()) == before
    assert cli.main(["sync", "--config", str(case["config_path"]), "--state-dir", str(case["state"]),
        "--review-id", prepared["review_id"], "--recover"]) == cli.ExitCode.EXECUTION_ERROR
    assert "Repair is required" in capsys.readouterr().out
    assert load_private_json(case["state"], Path("local-sync.json"))["records"][prepared["review_id"]]["result"]["status"] == "manual_repair_required"
    with pytest.raises(LocalSyncError):
        confirm(case, prepared)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-death and Git transaction acceptance")
def test_process_kill_after_worktree_write_preserves_intent_and_git_releases_its_locks(sync_case):
    case = sync_case
    child = r'''
import os, signal, sys
from pathlib import Path
from dependabot_watcher import local_sync
from dependabot_watcher.config import load_config
config_path, state = Path(sys.argv[1]), Path(sys.argv[2])
config = load_config(config_path)
class Branch:
    def fetch_branch_head(self, repository, branch): return sys.argv[3]
client = Branch()
preview = local_sync.prepare_sync(config, config_path, state, client, local_sync.selected_checkouts(config)[0]["target_id"])
original = local_sync._git
def kill_after_write(path, *arguments, **keywords):
    result = original(path, *arguments, **keywords)
    if arguments[:3] == ("read-tree", "-m", "-u"):
        print(preview["review_id"], flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    return result
local_sync._git = kill_after_write
local_sync.confirm_sync(config, config_path, state, client, preview["review_id"], preview["approval_token"])
'''
    result = subprocess.run([sys.executable, "-c", child, str(case["config_path"]), str(case["state"]), case["new"]],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == -9, result.stderr
    review_id = result.stdout.strip()
    journal = load_private_json(case["state"], Path("local-sync.json"))
    assert journal["records"][review_id]["status"] == "executing"
    locks = [case["path"] / ".git" / "HEAD.lock", case["path"] / ".git" / "refs" / "heads" / "integration.lock"]
    deadline = time.monotonic() + 5
    while any(path.exists() for path in locks) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(path.exists() for path in locks), "Git must release its own transaction locks after parent EOF"
    before = git(case["path"], "show-ref"), git(case["path"], "status", "--porcelain"), (case["path"] / "app.txt").read_bytes()
    recovered = recover_sync(None, case["config_path"], case["state"], review_id)
    assert recovered["result"]["status"] == "manual_repair_required"
    assert recovered["mutation_performed"] is False
    assert (git(case["path"], "show-ref"), git(case["path"], "status", "--porcelain"), (case["path"] / "app.txt").read_bytes()) == before
