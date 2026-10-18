"""Exact-consent clean fast-forward integration of selected developer checkouts.

This capability never selects the user's workflow, stashes, resets, switches a
branch, publishes repairs or updates GitHub. Git object acquisition is separate
from branch integration. Intent is saved before integration; uncertainty is
observed, never automatically replayed or rolled back.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import ConfigError, LocalSyncCheckout, WatcherConfig, load_config
from .github import GitHubError
from .review_queue import ReviewQueueError
from .runtime_state import RuntimeStateError, _read_private_json_object, ensure_private_directory, load_private_json, run_lock, save_private_json, validate_user_owned_directory
from .supervisor_workspace import _git as _workspace_git, _github_repository
from .workflow_assessment import checkout_workflow, require_checkout_workflow


class LocalSyncError(ReviewQueueError):
    """Local integration was refused or its outcome requires observation."""


class BranchHeadClient(Protocol):
    def fetch_branch_head(self, repository: str, branch: str) -> str: ...


_JOURNAL = Path("local-sync.json")
_SHA = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
_ID = re.compile(r"sync_[a-f0-9]{32}")
_MAX_RECORDS = 100
_LOCAL_FIELDS = {"repository", "directory", "branch", "git_directory", "common_directory",
                 "directory_identity", "git_identity", "common_identity", "head_sha",
                 "conversion_digest", "origin_digest"}


def _git(path: Path, *arguments: str, overrides: tuple[str, ...] = (), empty_ok: bool = False,
         timeout_seconds: int = 15) -> str:
    # Never interpret a reviewed SHA through local replacement objects.
    try:
        return _workspace_git(path, "--no-replace-objects", *arguments, overrides=overrides,
                              empty_ok=empty_ok, timeout_seconds=timeout_seconds)
    except ReviewQueueError as exc:
        raise LocalSyncError(str(exc).replace("Supervisor workspace", "Local checkout")) from exc


def _separate_control_paths(local: dict[str, Any], config_path: Path | None, state_directory: Path | None) -> None:
    protected = [Path(local[key]).resolve() for key in ("directory", "git_directory", "common_directory")]
    controls = tuple(path for path in (config_path, state_directory) if path is not None)
    for control in (path.expanduser().resolve() for path in controls):
        if any(control == path or control.is_relative_to(path) or path.is_relative_to(control) for path in protected):
            raise LocalSyncError("Configuration and Watcher state must be separate from the selected checkout and Git metadata.")


def validate_checkout_control_locations(path: Path, config_path: Path | None, directory: Path | None) -> None:
    """Inspect metadata paths without inspecting objects or writing Git state."""
    metadata = [str(path), str(path)]
    _separate_control_paths({"directory": str(path), "git_directory": str(path),
                             "common_directory": str(path)}, config_path, directory)
    if path.is_dir():
        try:
            metadata = [_git(path, "rev-parse", "--absolute-git-dir"),
                        _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")]
        except ReviewQueueError as exc:
            if os.path.lexists(path / ".git"):
                raise LocalSyncError("Selected checkout metadata is unavailable; control paths were not written.") from exc
    _separate_control_paths({"directory": str(path), "git_directory": metadata[0],
                             "common_directory": metadata[1]}, config_path, directory)


def validate_control_locations(config: WatcherConfig, config_path: Path | None, directory: Path) -> None:
    """Reject unsafe placement before any config-bound state writer runs."""
    for target in selected_checkouts(config):
        validate_checkout_control_locations(Path(target["directory"]), config_path, directory)


def _reject_external_object_sources(directory: Path, common: Path) -> None:
    # Even ostensibly read-only status/tree commands may lazy-fetch objects.
    settings = _git(directory, "config", "--name-only", "--get-regexp",
                    r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$", empty_ok=True)
    objects = common / "objects"
    pack = objects / "pack"
    if (settings or objects.is_symlink() or not objects.is_dir()
            or os.path.lexists(objects / "info" / "alternates")
            or os.path.lexists(objects / "info" / "http-alternates")
            or pack.is_symlink() or (pack.exists() and not pack.is_dir())
            or (pack.is_dir() and any(item.name.endswith(".promisor") for item in pack.iterdir()))):
        raise LocalSyncError("Partial, promisor or borrowed Git objects require a separately supported workflow.")


def _fetch_credential_overrides(directory: Path, gh: str) -> tuple[str, ...]:
    """Replace every configured helper scope with only the supported broker.

    gh auth setup-git itself installs scoped helpers. Refusing all such scopes
    breaks ordinary authenticated installs; inheriting them permits execution.
    Enumerate names only, then reset each helper list before installing ours.
    """
    names = _git(directory, "config", "--null", "--name-only", "--get-regexp", r"^credential\..*\.helper$", empty_ok=True)
    keys = ["credential.helper", *dict.fromkeys(filter(None, names.split("\0")))]
    if any("=" in key or any(ord(char) < 32 for char in key) for key in keys):
        raise LocalSyncError("Credential helper scope cannot be safely overridden.")
    broker = "!" + shlex.quote(gh) + " auth git-credential"
    return tuple(setting for key in keys for setting in (key + "=", key + "=" + broker))


def _validate_binding(binding: Any) -> None:
    fields = _LOCAL_FIELDS | {"target_id", "target_sha", "commits"}
    if (not isinstance(binding, dict) or not fields <= set(binding)
            or set(binding) - fields - {"tracking_refresh", "workflow"}):
        raise LocalSyncError("Synchronization checkout binding is malformed.")
    workflow = binding.get("workflow")
    if workflow is not None and (not isinstance(workflow, dict)
            or workflow.get("state") not in {"ready", "operator_review"} or workflow.get("method") != "fast_forward"
            or not isinstance(workflow.get("message"), str) or not workflow["message"].strip()
            or len(workflow["message"].encode()) > 8192):
        raise LocalSyncError("Synchronization workflow assessment is malformed.")
    for key in ("repository", "directory", "branch", "git_directory", "common_directory"):
        if not isinstance(binding[key], str) or not binding[key] or any(ord(char) < 32 for char in binding[key]):
            raise LocalSyncError("Synchronization checkout identity is malformed.")
    for key in ("directory", "git_directory", "common_directory"):
        if not Path(binding[key]).is_absolute() or ".." in Path(binding[key]).parts:
            raise LocalSyncError("Synchronization checkout path is malformed.")
    for key in ("directory_identity", "git_identity", "common_identity"):
        value = binding[key]
        if not isinstance(value, list) or len(value) != 2 or any(type(item) is not int or item < 0 for item in value):
            raise LocalSyncError("Synchronization filesystem identity is malformed.")
    for key in ("head_sha", "target_sha", "target_id", "conversion_digest", "origin_digest"):
        value = binding[key]
        pattern = _SHA if key.endswith("sha") else re.compile(r"[a-f0-9]{32}" if key == "target_id" else r"[a-f0-9]{64}")
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise LocalSyncError("Synchronization revision identity is malformed.")
    tracking = binding.get("tracking_refresh")
    if tracking is not None:
        if (not isinstance(tracking, dict) or set(tracking) != {"ref", "old_sha", "config_digest"}
                or tracking["ref"] != "refs/remotes/origin/" + binding["branch"]
                or not isinstance(tracking["config_digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", tracking["config_digest"])
                or tracking["old_sha"] is not None and (
                    not isinstance(tracking["old_sha"], str) or not _SHA.fullmatch(tracking["old_sha"]))):
            raise LocalSyncError("Synchronization tracking reference is malformed.")
    if (type(binding["commits"]) is not int or binding["commits"] < 0
            or binding["commits"] == 0 and (binding["head_sha"] != binding["target_sha"]
                or tracking is None or tracking["old_sha"] == binding["target_sha"])):
        raise LocalSyncError("Synchronization commit count is malformed.")


def _tracking_state(path: Path, branch: str) -> dict[str, Any] | None:
    """Resolve only the selected branch's matching origin upstream, never invent it."""
    keys = (f"branch.{branch}.remote", f"branch.{branch}.merge", "remote.origin.fetch")
    settings = [_git(path, "config", "--get-all", key, empty_ok=True) for key in keys]
    upstream = _git(path, "for-each-ref", "--format=%(upstream:remotename)%00%(upstream:remoteref)%00%(upstream)",
                    "refs/heads/" + branch).split("\0")
    reference = "refs/remotes/origin/" + branch
    if upstream != ["origin", "refs/heads/" + branch, reference]:
        return None  # A different/no upstream is the user's workflow, not ours to change.
    _git(path, "check-ref-format", reference)
    if _git(path, "symbolic-ref", "-q", reference, empty_ok=True):
        raise LocalSyncError("Symbolic remote-tracking references cannot be refreshed safely.")
    old = _git(path, "rev-parse", "--verify", "--quiet", reference, empty_ok=True).lower()
    if old and not _SHA.fullmatch(old):
        raise LocalSyncError("Remote-tracking revision identity is invalid.")
    return {"ref": reference, "old_sha": old or None, "config_digest": _digest(settings)}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def selected_checkouts(config: WatcherConfig, repository: str | None = None) -> list[dict[str, str]]:
    return [dict(repository=item.name, target_id=_digest([item.name.casefold(), target.directory, target.branch])[:32],
                 directory=target.directory, branch=target.branch)
            for item in config.repositories if repository is None or item.name.casefold() == repository.casefold()
            for target in item.local_sync]


def _target(config: WatcherConfig, target_id: str) -> tuple[str, LocalSyncCheckout]:
    matches = [item for item in selected_checkouts(config) if item["target_id"] == target_id]
    if len(matches) != 1:
        raise LocalSyncError("Select one configured local checkout; no synchronization capability covers this target.")
    item = matches[0]
    return item["repository"], LocalSyncCheckout(item["directory"], item["branch"])


def _identity(path: Path) -> list[int]:
    validate_user_owned_directory(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise LocalSyncError("Local synchronization requires an actual absolute checkout path, not a symlink alias.")
    state = path.stat()
    return [state.st_dev, state.st_ino]


def _checkout_dirty(directory: Path) -> bool:
    return bool(_git(directory, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=all",
                     overrides=("core.untrackedCache=false", "core.ignoreStat=false")))


def _local_state(repository: str, target: LocalSyncCheckout, *, require_clean: bool = True) -> dict[str, Any]:
    if os.name != "posix":
        raise LocalSyncError("Local synchronization currently requires the POSIX/WSL backend.")
    directory = Path(target.directory)
    identity = _identity(directory)
    _git(directory, "check-ref-format", "--branch", target.branch)
    top = _git(directory, "rev-parse", "--show-toplevel")
    git_directory = Path(_git(directory, "rev-parse", "--absolute-git-dir"))
    common = Path(_git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if top != str(directory):
        raise LocalSyncError("Register the checkout root, not a subdirectory.")
    git_identity, common_identity = _identity(git_directory), _identity(common)
    _reject_external_object_sources(directory, common)
    if (git_directory != common or len(_git(directory, "worktree", "list", "--porcelain").split("worktree ")) != 2):
        raise LocalSyncError("Linked or shared worktrees require a separately coordinated synchronization workflow.")
    if os.path.lexists(common / "info" / "grafts"):
        raise LocalSyncError("Git graft history is unsupported; no local integration was performed.")
    if _git(directory, "symbolic-ref", "-q", "HEAD", empty_ok=True) != "refs/heads/" + target.branch:
        raise LocalSyncError("The selected branch is not checked out; no branch was switched.")
    remote = _git(directory, "remote", "get-url", "--all", "origin")
    try:
        remote_repository = _github_repository(remote)
    except ReviewQueueError as exc:
        raise LocalSyncError("Checkout origin is not a supported GitHub identity; Git URL rewrites may be in effect.") from exc
    if "\n" in remote or remote_repository.casefold() != repository.casefold():
        raise LocalSyncError("Checkout origin does not match the configured GitHub repository.")
    # Never run clean/process/smudge helpers, fsmonitor, hooks or target code.
    # Refusing unsupported conversions is preferable to silently changing files
    # that depend on the user's filter workflow.
    filters = _git(directory, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$", empty_ok=True)
    if filters:
        raise LocalSyncError("This checkout has Git filters requiring a separately supported integration workflow.")
    sparse = _git(directory, "config", "--get", "core.sparseCheckout", empty_ok=True)
    if sparse and sparse != "false":
        raise LocalSyncError("Sparse checkouts are not supported by clean fast-forward synchronization.")
    storage = _git(directory, "config", "--get", "extensions.refStorage", empty_ok=True)
    if storage and storage != "files":
        raise LocalSyncError("This Git reference storage needs a separately supported integration workflow.")
    flags = _git(directory, "ls-files", "-v").splitlines()
    if any(line and (line[0].islower() or line[0] == "S") for line in flags):
        raise LocalSyncError("Assume-unchanged or skip-worktree files prevent trustworthy clean-checkout verification.")
    if any(line.startswith("160000 ") for line in _git(directory, "ls-files", "--stage").splitlines()):
        raise LocalSyncError("Submodule checkouts require a separately supported integration workflow.")
    if require_clean and _checkout_dirty(directory):
        raise LocalSyncError("Local changes or untracked files were preserved. Clean fast-forward synchronization is unavailable.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        if os.path.lexists(git_directory / marker):
            raise LocalSyncError("An unfinished Git operation must be resolved through the user's workflow first.")
    head = _git(directory, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if not _SHA.fullmatch(head):
        raise LocalSyncError("Checkout commit identity is invalid.")
    # Record no raw Git config. Material checkout conversions are bound by hash.
    conversions = [_git(directory, "config", "--get", key, empty_ok=True)
                   for key in ("core.autocrlf", "core.eol", "core.symlinks", "core.filemode", "core.attributesFile")]
    # Attribute files outside the commit can change checkout conversion without
    # advancing HEAD. Do not claim a revision-bound conversion in that case.
    default_attributes = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "git" / "attributes"
    git_prefix = Path(_git(directory, "--exec-path")).parent.parent
    external_attributes = (common / "info" / "attributes", default_attributes,
                           git_prefix / "etc" / "gitattributes", Path("/etc/gitattributes"))
    if conversions[-1] or any(os.path.lexists(path) for path in external_attributes):
        raise LocalSyncError("External Git attributes require a separately supported conversion workflow.")
    return dict(repository=repository, directory=str(directory), branch=target.branch,
                git_directory=str(git_directory), common_directory=str(common),
                directory_identity=identity, git_identity=git_identity, common_identity=common_identity,
                head_sha=head, conversion_digest=_digest(conversions), origin_digest=_digest(remote))


def _current_config(config: WatcherConfig, config_path: Path) -> None:
    if load_config(config_path) != config:
        raise LocalSyncError("Configuration changed; review synchronization again.")


def _check_collisions(path: Path, old: str, target: str) -> None:
    """Git may overwrite ignored files by default; explicitly preserve them."""
    names = _git(path, "diff-tree", "--no-commit-id", "--no-renames", "-r", "--name-only", "-z",
                 "--diff-filter=AMT", old, target).split("\0")
    for name in filter(None, names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise LocalSyncError("Target paths cannot be safely integrated.")
        candidate = path / relative
        for parent in candidate.parents:
            if parent == path:
                break
            if parent.is_symlink():
                raise LocalSyncError("A target path crosses a local symlink; local content was preserved.")
        if os.path.lexists(candidate):
            try:
                _git(path, "ls-files", "--error-unmatch", "--", name)
            except ReviewQueueError as exc:
                raise LocalSyncError("A target file would replace local untracked or ignored content; it was preserved.") from exc


def _journal(directory: Path, *, read_only: bool = False) -> dict[str, Any]:
    if read_only:
        if not os.path.lexists(directory):
            return {"schema_version": 1, "records": {}}
        validate_user_owned_directory(directory)
        if os.name == "posix" and directory.stat().st_mode & 0o077:
            raise LocalSyncError("Saved synchronization state must already be private for read-only observation.")
    else:
        directory = ensure_private_directory(directory)
    value = ((_read_private_json_object(directory / _JOURNAL, maximum_bytes=5_000_000) if read_only
              else load_private_json(directory, _JOURNAL)) if os.path.lexists(directory / _JOURNAL)
             else {"schema_version": 1, "records": {}})
    if (set(value) != {"schema_version", "records"} or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or not isinstance(value["records"], dict)
            or len(value["records"]) > _MAX_RECORDS):
        raise LocalSyncError("Local synchronization history is malformed.")
    for key, record in value["records"].items():
        if (not isinstance(key, str) or not _ID.fullmatch(key) or not isinstance(record, dict)
                or set(record) not in ({"binding", "config_digest", "expires_at", "status", "result"},
                                      {"binding", "config_digest", "expires_at", "status", "result", "sequence"})
                or ("sequence" in record and (type(record["sequence"]) is not int or not 1 <= record["sequence"] <= 2**63 - 1))
                or not isinstance(record["status"], str)
                or record["status"] not in {"awaiting_confirmation", "executing", "completed", "failed", "recovery_required"}
                or not isinstance(record["binding"], dict) or not isinstance(record["expires_at"], str)
                or not isinstance(record["config_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["config_digest"])):
            raise LocalSyncError("Local synchronization history contains an invalid record.")
        _validate_binding(record["binding"])
        try:
            expiration = datetime.fromisoformat(record["expires_at"])
            if expiration.utcoffset() is None:
                raise ValueError("Missing timezone")
        except ValueError as exc:
            raise LocalSyncError("Synchronization expiration is malformed.") from exc
        result = record["result"]
        if record["status"] in {"completed", "failed"} or (record["status"] == "recovery_required" and result is not None):
            if (not isinstance(result, dict) or set(result) != {"status", "binding", "mutation_performed"}
                    or result["binding"] != record["binding"] or type(result["mutation_performed"]) is not bool
                    or not isinstance(result["status"], str)
                    or result["status"] not in {"synchronized", "observed_applied", "observed_not_applied", "manual_repair_required"}
                    or (record["status"] == "recovery_required" and
                        (result["status"] != "manual_repair_required" or result["mutation_performed"] is not False))
                    or (record["status"] != "recovery_required" and result["status"] == "manual_repair_required")):
                raise LocalSyncError("Synchronization outcome is malformed.")
        elif result is not None:
            raise LocalSyncError("Unfinished synchronization has an invalid outcome.")
    return value


def _save(directory: Path, journal: dict[str, Any]) -> None:
    save_private_json(directory, _JOURNAL, journal)


def _token(review_id: str, binding: dict[str, Any]) -> str:
    return "sync-" + _digest([review_id, binding])


def _ordered_records(records: dict[str, Any]) -> list[tuple[str, Any]]:
    return sorted(records.items(), key=lambda entry: (
        entry[1].get("sequence", 0), datetime.fromisoformat(entry[1]["expires_at"]), entry[0]))


def synchronization_observations(config: WatcherConfig, directory: Path, repository: str) -> list[dict[str, Any]]:
    """Bounded historical evidence for reasoning, not live checkout readiness.

    Caller holds the state lock. Never inspect a developer worktree or acquire
    mutation authority while assembling an agent's evidence packet.
    """
    selected = {item["target_id"] for item in selected_checkouts(config, repository)}
    records = {identity: record for identity, record in _journal(directory)["records"].items()
               if record["binding"]["target_id"] in selected}
    return [{"review_id": identity, "target_id": record["binding"]["target_id"],
             "directory": record["binding"]["directory"], "branch": record["binding"]["branch"],
             "from_sha": record["binding"]["head_sha"], "target_sha": record["binding"]["target_sha"],
             "status": record["status"], "outcome": record["result"]["status"] if record["result"] else None,
             "applicability": "historical_recheck_before_use"}
            for identity, record in _ordered_records(records)[-20:]]


def observe_checkout(config: WatcherConfig, config_path: Path, target: dict[str, str],
                     directory: Path, client: BranchHeadClient | None) -> dict[str, Any]:
    """Point-in-time guidance only: no fetch, ref update, preview or approval.

    A cached origin ref is reported separately from the GitHub read. Missing
    objects do not trigger a download; integration readiness belongs to prepare.
    """
    observed: dict[str, Any] = {"target_id": target["target_id"], "status": "unknown",
        "checked_at": datetime.now(timezone.utc).isoformat(), "mutation_performed": False}
    try:
        _current_config(config, config_path)
        repository, checkout = _target(config, target["target_id"])
        observed["workflow"] = checkout_workflow(config, directory, repository, checkout.directory, checkout.branch)
        local = _local_state(repository, checkout)
        _separate_control_paths(local, config_path, directory)
        observed["head_sha"] = local["head_sha"]
        tracking = _tracking_state(Path(checkout.directory), checkout.branch)
        observed["tracking_sha"] = tracking["old_sha"] if tracking else None
        if client is None or not callable(getattr(client, "fetch_branch_head", None)):
            observed["message"] = "Live remote status is unavailable. Check this checkout before updating."
            return observed
        remote = client.fetch_branch_head(repository, checkout.branch).lower()
        if not _SHA.fullmatch(remote):
            raise LocalSyncError("GitHub returned an invalid branch commit.")
        _current_config(config, config_path)
        if _local_state(repository, checkout) != local:
            raise LocalSyncError("Checkout changed during this check; refresh before reviewing an update.")
        if _tracking_state(Path(checkout.directory), checkout.branch) != tracking:
            raise LocalSyncError("Origin tracking changed during this check; refresh before reviewing an update.")
        observed["remote_sha"] = remote
        observed["status"] = "current" if local["head_sha"] == remote and (
            tracking is None or tracking["old_sha"] == remote) else "review_needed"
        observed["message"] = ("Local branch matches the checked GitHub commit."
            if observed["status"] == "current" else
            "Local branch or origin tracking differs from GitHub. Review the local update; no files were changed.")
    except (OSError, ReviewQueueError, GitHubError, ConfigError, RuntimeStateError) as exc:
        observed["status"] = "unavailable"
        observed["message"] = str(exc)
    return observed


def prepare_sync(config: WatcherConfig, config_path: Path, directory: Path, client: BranchHeadClient,
                 target_id: str, *, refresh_tracking: bool = True) -> dict[str, Any]:
    """Read live state and acquire missing objects, without integrating a branch."""
    _current_config(config, config_path)
    repository, target = _target(config, target_id)
    workflow = require_checkout_workflow(config, directory, repository, target.directory, target.branch)
    local = _local_state(repository, target)
    _separate_control_paths(local, config_path, directory)
    path = Path(target.directory)
    target_sha = client.fetch_branch_head(repository, target.branch).lower()
    if not _SHA.fullmatch(target_sha):
        raise LocalSyncError("GitHub returned an invalid branch commit.")
    try:
        _git(path, "cat-file", "-e", target_sha + "^{commit}")
    except ReviewQueueError:
        gh = shutil.which("gh")
        if gh is None:
            raise LocalSyncError("Missing Git objects require the supported GitHub CLI credential broker.")
        if _git(path, "config", "--name-only", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$", empty_ok=True):
            raise LocalSyncError("Git URL rewrites are unsupported for trusted missing-object acquisition.")
        # Reject transport customization rather than silently substituting the
        # user's CA/proxy policy. No credential-bearing request runs in that case.
        unsafe_http = _git(path, "config", "--name-only", "--get-regexp",
            r"^http\.(.*\.)?(sslverify|sslcainfo|sslcapath|sslcert|sslkey|sslbackend|proxy.*|extraheader|followredirects|curloptresolve|cookiefile|savecookies)$",
            empty_ok=True)
        transport_environment = {"ssl_cert_file", "ssl_cert_dir", "curl_ca_bundle", "http_proxy", "https_proxy", "all_proxy"}
        if unsafe_http or any(key.casefold() in transport_environment and value for key, value in os.environ.items()):
            raise LocalSyncError("Custom TLS, CA, proxy, header, cookie, credential helper or redirect settings require a separately supported fetch workflow.")
        # Do not execute repository-selected SSH/credential/remote helpers.
        # Clear inherited helpers, use only our existing GitHub CLI broker,
        # and fail closed if URL rewrites select a non-HTTPS protocol.
        _git(path, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-maintenance",
             "https://github.com/" + repository + ".git", target_sha,
             overrides=("gc.auto=0", "protocol.allow=never", "protocol.https.allow=always",
                        "http.sslVerify=true", "http.https://github.com/.sslVerify=true", "http.followRedirects=false",
                        "credential.interactive=false", "core.askPass=/bin/false",
                        *_fetch_credential_overrides(path, gh)), timeout_seconds=120)
    _current_config(config, config_path)
    if _local_state(repository, target) != local:
        raise LocalSyncError("Local checkout changed during preflight; nothing was integrated.")
    tracking = _tracking_state(path, target.branch) if refresh_tracking else None
    if local["head_sha"] == target_sha and (tracking is None or tracking["old_sha"] == target_sha):
        return {"schema_version": 1, "status": "up_to_date", "binding": local, "mutation_performed": False}
    try:
        _git(path, "merge-base", "--is-ancestor", local["head_sha"], target_sha)
    except ReviewQueueError as exc:
        raise LocalSyncError("The selected local branch is ahead or divergent; no rebase, reset or merge commit was attempted.") from exc
    if any(line.startswith("160000 ") for line in _git(path, "ls-tree", "-r", target_sha).splitlines()):
        raise LocalSyncError("The target contains submodules; no integration was performed.")
    _check_collisions(path, local["head_sha"], target_sha)
    # Native two-tree safety additionally checks tracked/untracked changes.
    _git(path, "read-tree", "--dry-run", "-m", "-u", local["head_sha"], target_sha)
    if require_checkout_workflow(config, directory, repository, target.directory, target.branch) != workflow:
        raise LocalSyncError("Workflow guidance changed during preflight; ask the agent to reassess before approving an update.")
    binding = {**local, "target_id": target_id, "target_sha": target_sha,
               "commits": int(_git(path, "rev-list", "--count", local["head_sha"] + ".." + target_sha)),
               "workflow": workflow}
    if refresh_tracking:
        binding["tracking_refresh"] = tracking
    with run_lock(directory):
        _current_config(config, config_path)
        journal = _journal(directory)
        unresolved = [item for item in journal["records"].values() if item["binding"].get("target_id") == target_id
                      and item["status"] in {"executing", "recovery_required"}]
        if unresolved:
            raise LocalSyncError("A prior synchronization outcome needs observation; do not approve another attempt.")
        sequence = max((item.get("sequence", 0) for item in journal["records"].values()), default=0) + 1
        if sequence > 2**63 - 1:
            raise LocalSyncError("Synchronization history sequence is exhausted; existing outcomes were preserved.")
        # A new preview supersedes only unconsumed previews for this target.
        # Expired previews for other targets hold no execution intent either.
        now = datetime.now(timezone.utc)
        for key, item in list(journal["records"].items()):
            if item["status"] == "awaiting_confirmation" and (
                    item["binding"]["target_id"] == target_id or now >= datetime.fromisoformat(item["expires_at"])):
                del journal["records"][key]
        if len(journal["records"]) >= _MAX_RECORDS:
            retired = next((key for key, item in _ordered_records(journal["records"])
                            if item["status"] in {"completed", "failed"}), None)
            if retired is None:
                raise LocalSyncError("Synchronization history is full; existing decisions were preserved.")
            del journal["records"][retired]
        review_id = "sync_" + secrets.token_hex(16)
        journal["records"][review_id] = {"binding": binding, "config_digest": _digest(asdict(config)),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            # Durable preparation order is independent of wall-clock changes.
            # Legacy receipts have no sequence and stay readable as history.
            "sequence": sequence,
            "status": "awaiting_confirmation", "result": None}
        _save(directory, journal)
    return {"schema_version": 1, "status": "ready", "review_id": review_id, "binding": binding,
            "approval_token": _token(review_id, binding), "mutation_performed": False}


def _fast_forward(binding: dict[str, Any], check: Callable[[], None]) -> None:
    """Hold Git's branch transaction while safely updating index/worktree.

    The prepared compare-and-swap Git transaction holds the selected branch
    and HEAD locks. External filesystem writers
    remain outside this lock. Git's worktree safety checks must still succeed.
    """
    path = Path(binding["directory"])
    executable = shutil.which("git")
    if executable is None:
        raise LocalSyncError("Git is unavailable.")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    command = [executable, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(path),
               "update-ref", "--stdin", "-m", "Watcher confirmed local fast-forward"]
    check()
    process = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
    assert process.stdin is not None and process.stdout is not None
    def transaction(instruction: str, expected: str) -> None:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(instruction.encode())
        process.stdin.flush()
        response = bytearray()
        deadline = time.monotonic() + 15
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not response.endswith(b"\n") and len(response) < 100:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise LocalSyncError("Git reference transaction timed out; inspect the saved outcome.")
                chunk = os.read(process.stdout.fileno(), 100 - len(response))
                if not chunk:
                    break
                response.extend(chunk)
        # Git transaction acknowledgements are fixed, tiny protocol lines.
        if response != (expected + ": ok\n").encode():
            raise LocalSyncError(f"Git refused the synchronization transaction at {expected}; inspect the saved outcome.")
    try:
        transaction("start\n", "start")
        updates = f'option no-deref\nupdate refs/heads/{binding["branch"]} {binding["target_sha"]} {binding["head_sha"]}\n'
        tracking = binding.get("tracking_refresh")
        if tracking is not None:
            old = tracking["old_sha"] or "0" * len(binding["target_sha"])
            updates += f'update {tracking["ref"]} {binding["target_sha"]} {old}\n'
        transaction(updates + "prepare\n", "prepare")
        check()
        _check_collisions(path, binding["head_sha"], binding["target_sha"])
        _git(path, "read-tree", "-m", "-u", binding["head_sha"], binding["target_sha"])
        transaction("commit\n", "commit")
        process.stdin.close()
        if process.wait(timeout=15) != 0:
            raise LocalSyncError("Git did not confirm synchronization completion.")
    finally:
        # EOF lets Git abort a prepared transaction and release its own locks.
        # Killing immediately can strand those locks after an ordinary refusal.
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def confirm_sync(config: WatcherConfig, config_path: Path, directory: Path, client: BranchHeadClient,
                 review_id: str, token: str) -> dict[str, Any]:
    """Consume exact approval once; record intent before any local integration."""
    if not _ID.fullmatch(review_id):
        raise LocalSyncError("Synchronization review identity is invalid.")
    validate_control_locations(config, config_path, directory)
    with run_lock(directory):
        _current_config(config, config_path)
        journal = _journal(directory)
        record = journal["records"].get(review_id)
        if record is None or record["status"] != "awaiting_confirmation":
            raise LocalSyncError("This synchronization approval is absent or already consumed; inspect its outcome.")
        binding = record["binding"]
        if (record["config_digest"] != _digest(asdict(config))
                or datetime.now(timezone.utc) >= datetime.fromisoformat(record["expires_at"])
                or not secrets.compare_digest(token, _token(review_id, binding))):
            raise LocalSyncError("Synchronization approval expired or changed; review the exact checkout again.")
        repository, target = _target(config, binding["target_id"])
        if binding.get("workflow") != require_checkout_workflow(config, directory, repository, target.directory, target.branch):
            raise LocalSyncError("Workflow assessment changed or this legacy review lacks it; review the local update again.")
        def check() -> None:
            _current_config(config, config_path)
            workflow = require_checkout_workflow(config, directory, repository, target.directory, target.branch)
            if binding.get("workflow") != workflow:
                raise LocalSyncError("Workflow assessment changed or this legacy review lacks it; review the local update again.")
            actual = _local_state(repository, target)
            if actual != {key: binding[key] for key in _LOCAL_FIELDS}:
                raise LocalSyncError("Local checkout identity, branch or files changed; nothing will be overwritten.")
            if "tracking_refresh" in binding and _tracking_state(Path(target.directory), target.branch) != binding["tracking_refresh"]:
                raise LocalSyncError("Remote-tracking reference or upstream configuration changed; review synchronization again.")
            if client.fetch_branch_head(repository, target.branch).lower() != binding["target_sha"]:
                raise LocalSyncError("The remote branch changed after review; request a fresh synchronization review.")
        actual = _local_state(repository, target)
        _separate_control_paths(actual, config_path, directory)
        if actual != {key: binding[key] for key in _LOCAL_FIELDS}:
            raise LocalSyncError("Local checkout changed; review synchronization again.")
        lock_directory = ensure_private_directory(Path(actual["git_directory"]) / "dependabot-watcher-sync")
        with run_lock(lock_directory):
            check()
            try:
                record["status"] = "executing"
                _save(directory, journal)
                _fast_forward(binding, check)
                _current_config(config, config_path)
                current = _local_state(repository, target)
                expected = {key: value for key, value in binding.items() if key in _LOCAL_FIELDS}
                expected["head_sha"] = binding["target_sha"]
                if current != expected:
                    raise LocalSyncError("The local synchronization outcome could not be verified.")
                if "tracking_refresh" in binding:
                    expected_tracking = binding["tracking_refresh"]
                    if expected_tracking is not None:
                        expected_tracking = {**expected_tracking, "old_sha": binding["target_sha"]}
                    if _tracking_state(Path(target.directory), target.branch) != expected_tracking:
                        raise LocalSyncError("The tracking-reference outcome could not be verified.")
                record["status"] = "completed"
                record["result"] = {"status": "synchronized", "binding": binding, "mutation_performed": True}
                _save(directory, journal)
                return {"schema_version": 1, "review_id": review_id, **record["result"]}
            except (OSError, subprocess.SubprocessError, ReviewQueueError, GitHubError, ConfigError, RuntimeStateError) as exc:
                record["status"] = "recovery_required"
                try:
                    _save(directory, journal)
                except (OSError, RuntimeStateError):
                    pass  # Earlier durable intent remains visible for observation.
                raise LocalSyncError("Synchronization outcome needs checking. Do not retry; use the saved review's recovery action.") from exc


def recover_sync(config: WatcherConfig | None, config_path: Path, directory: Path, review_id: str) -> dict[str, Any]:
    """Observe an uncertain attempt, without updating any Git files or refs."""
    initial = _journal(directory, read_only=True)["records"].get(review_id)
    if initial is None:
        raise LocalSyncError("Synchronization review is unavailable.")
    _separate_control_paths(initial["binding"], None, directory)
    with run_lock(directory):
        journal = _journal(directory)
        record = journal["records"].get(review_id)
        if record is None:
            raise LocalSyncError("Synchronization review is unavailable.")
        binding = record["binding"]
        # Read-only observation stays available after registration/config drift.
        # The private saved binding is revalidated against actual ownership,
        # origin, branch and metadata; it never grants a new integration.
        repository = binding["repository"]
        target = LocalSyncCheckout(binding["directory"], binding["branch"])
        actual = _local_state(repository, target, require_clean=False)
        if any(actual[key] != binding[key] for key in actual if key != "head_sha"):
            raise LocalSyncError("Checkout identity changed; preserve it for separately authorized recovery.")
        with run_lock(ensure_private_directory(Path(actual["git_directory"]) / "dependabot-watcher-sync")):
            current = _local_state(repository, target, require_clean=False)
            if any(current[key] != binding[key] for key in current if key != "head_sha"):
                raise LocalSyncError("Checkout identity changed; preserve it for separately authorized recovery.")
            if current["head_sha"] not in {binding["head_sha"], binding["target_sha"]}:
                raise LocalSyncError("Checkout moved to an unexpected commit; no recovery mutation was performed.")
            if record["status"] in {"executing", "recovery_required"}:
                tracking = binding.get("tracking_refresh")
                actual_tracking = _tracking_state(Path(binding["directory"]), binding["branch"]) if tracking is not None else None
                applied_tracking = {**tracking, "old_sha": binding["target_sha"]} if tracking is not None else None
                applied = current["head_sha"] == binding["target_sha"] and actual_tracking == applied_tracking
                not_applied = current["head_sha"] == binding["head_sha"] and actual_tracking == tracking
                if _checkout_dirty(Path(binding["directory"])) or not (applied or not_applied):
                    # This may be a partial Git write or subsequent user work.
                    # Do not guess, roll back, replay, or retain file contents.
                    record["status"] = "recovery_required"
                    record["result"] = {"status": "manual_repair_required", "binding": binding, "mutation_performed": False}
                else:
                    record["status"] = "completed" if applied else "failed"
                    record["result"] = {"status": "observed_applied" if applied else "observed_not_applied",
                                        "binding": binding, "mutation_performed": False}
                _save(directory, journal)
            return {"schema_version": 1, "review_id": review_id, "status": record["status"],
                    "binding": binding, "result": record["result"], "mutation_performed": False}


def handle_sync_request(config: WatcherConfig | None, config_path: Path, directory: Path,
                        client: BranchHeadClient | None, request: dict[str, Any]) -> dict[str, Any]:
    """Native channel selects registered identities, never caller-supplied paths."""
    operation = request.get("operation")
    fields = {"schema_version", "request_id", "operation"}
    additions = {"local_sync_targets": set(), "local_sync_status": set(), "prepare_local_sync": {"target_id"},
                 "confirm_local_sync": {"review_id", "approval_token"},
                 "recover_local_sync": {"review_id"}}
    tracking_capable = operation == "prepare_local_sync" and request.get("tracking_refresh_supported") is True
    if (not isinstance(operation, str) or operation not in additions
            or set(request) != fields | additions[operation] | ({"tracking_refresh_supported"} if tracking_capable else set())
            or type(request.get("schema_version")) is not int or request["schema_version"] != 1
            or not isinstance(request.get("request_id"), str)
            or not re.fullmatch(r"sync_[A-Za-z0-9_]{1,59}", request["request_id"])
            or any(not isinstance(request[key], str) for key in additions[operation])):
        raise LocalSyncError("Local synchronization requires an exact bounded request.")
    if operation not in {"local_sync_targets", "recover_local_sync", "local_sync_status"}:
        if config is None or client is None:
            raise LocalSyncError("Integration requires current configuration and the supported GitHub transport.")
        _current_config(config, config_path)
    if operation == "local_sync_targets":
        targets = selected_checkouts(config) if config is not None else []
        journal = _journal(directory, read_only=True)
        selected = {target["target_id"] for target in targets}
        reviews = [{"review_id": identity, **record} for identity, record in journal["records"].items()
                       if record["binding"]["target_id"] in selected or record["status"] in {"executing", "recovery_required"}]
        for review in reviews:
            binding = review["binding"]
            if binding["target_id"] not in selected:
                targets.append({key: binding[key] for key in ("repository", "target_id", "directory", "branch")})
                selected.add(binding["target_id"])
        result = {"targets": targets, "reviews": reviews}
    elif operation == "local_sync_status":
        if config is None:
            raise LocalSyncError("Current checkout observation requires current configuration.")
        _current_config(config, config_path)
        result = {"observations": [observe_checkout(config, config_path, target, directory, client)
                                   for target in selected_checkouts(config)]}
    elif operation == "prepare_local_sync":
        assert config is not None and client is not None
        result = {"review": prepare_sync(config, config_path, directory, client, request["target_id"],
                                        refresh_tracking=tracking_capable)}
    elif operation == "confirm_local_sync":
        assert config is not None and client is not None
        result = {"review": confirm_sync(config, config_path, directory, client,
                                       request["review_id"], request["approval_token"])}
    else:
        result = {"review": recover_sync(config, config_path, directory, request["review_id"])}
    return {"schema_version": 1, "request_id": request["request_id"], "status": "ok", **result}
