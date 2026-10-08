"""Bounded setup-time creation of persistent supervisor workspaces.

The supplied checkout is a read-only Git object source. Provisioning creates a
separate private clone and detached linked worktree outside that checkout; it
never checks out, fetches into, or copies dirty files from the source.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - provisioning is POSIX/WSL only
    fcntl = None  # type: ignore[assignment]

from .config import SupervisorWorkspaceConfig, normalize_repository_name
from .runtime_state import RuntimeStateError, validate_user_owned_directory
from .review_queue import ReviewQueueError
from .supervisor_workspace import _github_repository, _instruction_fingerprint


RECEIPT_SCHEMA_VERSION = 1
DEFAULT_WORKSPACE_MAX_BYTES = 1024**3
DEFAULT_WORKSPACE_MINIMUM_FREE_BYTES = 512 * 1024**2
MAX_WORKSPACE_BYTES = 16 * 1024**3
MAX_INSTRUCTION_SOURCES = 32
MAX_TREE_OUTPUT_BYTES = 16 * 1024**2
MAX_ATTRIBUTE_OUTPUT_BYTES = 64 * 1024**2
MAX_TREE_ENTRIES = 100_000
MAX_REF_METADATA_ENTRIES = 100_000
MAX_REF_METADATA_BYTES = 256 * 1024**2
MAX_REF_LIST_OUTPUT_BYTES = 16 * 1024**2
MAX_RECEIPT_BYTES = 65_536
CHECKOUT_FILE_METADATA_RESERVE = 8 * 1024
CHECKOUT_DIRECTORY_METADATA_RESERVE = 4 * 1024
REF_METADATA_ENTRY_RESERVE = 4 * 1024
CLONED_REF_METADATA_RESERVE = 4 * 1024
CHECKOUT_EOL_EXPANSION_FACTOR = 2
DEFAULT_ALLOCATION_UNIT = 4096
PACKED_OBJECT_METADATA_RESERVE = 64
_SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")
_FILTER_KEY = re.compile(r"filter\.[A-Za-z0-9_.-]+\.(?:clean|smudge|process)\Z")
_RECEIPT_FIELDS = {
    "schema_version", "owner", "repository", "source_checkout", "clone_directory",
    "workspace_directory", "base_sha", "base_provenance", "branch",
    "estimated_bytes", "instruction_sources", "status",
}


class WorkspaceProvisioningError(RuntimeError):
    """Raised when a private maintenance copy cannot be safely provisioned."""


@dataclass(frozen=True)
class WorkspaceProvisionRequest:
    repository: str
    source_checkout: Path
    workspace_directory: Path
    branch: str
    collected_base_sha: str | None
    explicit_instruction_sources: tuple[str, ...] = ()
    refresh_authorized: bool = False


@dataclass(frozen=True)
class ProvisionedWorkspace:
    registration: SupervisorWorkspaceConfig
    status: str
    source_checkout: str
    base_sha: str
    base_provenance: str
    estimated_bytes: int
    maximum_bytes: int
    minimum_free_bytes: int
    reserve_paths: tuple[str, ...]
    discovered_instruction_sources: tuple[str, ...]
    instruction_sources: tuple[str, ...]
    source_ancestor_instruction_sources: tuple[str, ...]
    workspace_ancestor_instruction_sources: tuple[str, ...]
    runtime_instruction_discovery_verified: bool = False
    retention: str = "persistent until deliberately removed by the operator"


@dataclass(frozen=True)
class _Preflight:
    request: WorkspaceProvisionRequest
    source: Path
    root: Path
    repository_directory: Path
    clone_directory: Path
    workspace: Path
    base_sha: str
    base_provenance: str
    source_common_directory: Path
    estimated_bytes: int
    checkout_bytes: int
    discovered_sources: tuple[str, ...]
    source_ancestor_sources: tuple[str, ...]
    workspace_ancestor_sources: tuple[str, ...]
    instruction_sources: tuple[str, ...]
    existing_receipt: dict[str, Any] | None


def workspace_root_for_state(state_directory: Path) -> Path:
    """Return the stable default root for persistent maintenance copies."""

    return state_directory.expanduser() / "supervisor-workspaces"


def workspace_directory_for(root: Path, repository: str) -> Path:
    """Map a repository identity to one stable, collision-resistant path."""

    try:
        canonical = normalize_repository_name(repository, "supervisor provision repository")
    except Exception as exc:
        raise WorkspaceProvisioningError("Supervisor provisioning repository identity is invalid.") from exc
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", canonical.casefold()).strip(".-")[:72] or "repository"
    suffix = hashlib.sha256(canonical.casefold().encode("utf-8")).hexdigest()[:12]
    return _absolute_path(root) / f"{slug}-{suffix}" / "worktree"


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts:
        raise WorkspaceProvisioningError("Provisioning paths must be absolute and contain no traversal.")
    return Path(os.path.abspath(expanded))


def _environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
    )
    return environment


def _git(
    directory: Path | None,
    *arguments: str,
    allow_empty: bool = False,
    timeout_seconds: int = 30,
    output_limit: int = 65_536,
    config_overrides: Sequence[str] = (),
) -> bytes:
    """Run Git without ambient GIT_* redirects, hooks, global config or filters."""

    executable = shutil.which("git")
    if executable is None:
        raise WorkspaceProvisioningError("Git is unavailable; no maintenance workspace was created.")
    command = [executable, "--no-optional-locks", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null"]
    for override in config_overrides:
        command.extend(("-c", override))
    if directory is not None:
        command.extend(("-C", str(directory)))
    command.extend(arguments)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command, env=_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        output = bytearray()
        totals = {"stdout": 0, "stderr": 0}
        deadline = time.monotonic() + timeout_seconds
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceProvisioningError("Git provisioning timed out; partial state was preserved.")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    totals[key.data] += len(chunk)
                    if totals[key.data] > output_limit:
                        raise WorkspaceProvisioningError("Git workspace evidence exceeds its configured bound.")
                    if key.data == "stdout":
                        output.extend(chunk)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code == 1 and allow_empty and not output:
                return b""
            if code != 0:
                raise WorkspaceProvisioningError("Git provisioning failed; private diagnostics were withheld.")
        return bytes(output)
    except WorkspaceProvisioningError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceProvisioningError("Git workspace evidence is unavailable; private diagnostics were withheld.") from exc
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _git_text(directory: Path | None, *arguments: str, **kwargs: Any) -> str:
    try:
        return _git(directory, *arguments, **kwargs).decode("utf-8").rstrip("\n")
    except UnicodeError as exc:
        raise WorkspaceProvisioningError("Git returned unsupported workspace metadata.") from exc


def _filter_overrides(directory: Path) -> tuple[str, ...]:
    output = _git(directory, "config", "--local", "--name-only", "--get-regexp",
                  r"^filter\..*\.(clean|smudge|process)$", allow_empty=True)
    try:
        keys = output.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise WorkspaceProvisioningError("Git filter configuration is ambiguous; provisioning stopped.") from exc
    if any(not _FILTER_KEY.fullmatch(key) for key in keys):
        raise WorkspaceProvisioningError("Git filter configuration is unsupported; provisioning stopped.")
    return tuple(f"{key}=" for key in keys)


def _is_wsl() -> bool:
    if os.name != "posix":
        return False
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "microsoft" in release.casefold() or "wsl" in release.casefold()


def _uid() -> int:
    if not hasattr(os, "getuid"):
        raise WorkspaceProvisioningError("Automatic workspace provisioning currently requires POSIX/WSL.")
    return os.getuid()


def _validate_path_ancestors(path: Path, *, allow_missing: bool) -> None:
    """Reject symlink or replaceable ancestors without changing their mode."""

    path = _absolute_path(path)
    components = list(reversed(path.parents)) + [path]
    missing_seen = False
    for component in components:
        try:
            state = component.lstat()
        except FileNotFoundError:
            missing_seen = True
            continue
        except OSError as exc:
            raise WorkspaceProvisioningError("A workspace path ancestor cannot be inspected safely.") from exc
        if missing_seen and not allow_missing:
            raise WorkspaceProvisioningError("A workspace path ancestor is missing.")
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise WorkspaceProvisioningError("Workspace paths cannot pass through symbolic links or non-directories.")
        mode = stat.S_IMODE(state.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise WorkspaceProvisioningError("A workspace path ancestor is writable by another principal.")
    if not allow_missing and missing_seen:
        raise WorkspaceProvisioningError("A workspace path ancestor is missing.")


def _validate_root_state(root: Path) -> None:
    if not os.path.lexists(root):
        return
    _validate_path_ancestors(root, allow_missing=False)
    state = root.lstat()
    if state.st_uid != _uid() or stat.S_IMODE(state.st_mode) & 0o077:
        raise WorkspaceProvisioningError("The existing workspace root is not private to the current user.")


def _ensure_private_root(root: Path) -> None:
    components = list(reversed(root.parents)) + [root]
    for component in components:
        try:
            component.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceProvisioningError("The private workspace root could not be created.") from exc
        try:
            state = component.lstat()
        except OSError as exc:
            raise WorkspaceProvisioningError("The private workspace root could not be verified.") from exc
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise WorkspaceProvisioningError("The private workspace root contains an unsafe path component.")
        if component == root:
            if state.st_uid != _uid() or stat.S_IMODE(state.st_mode) & 0o077:
                raise WorkspaceProvisioningError("The workspace root must be current-user-only and private.")
        elif stat.S_IMODE(state.st_mode) & 0o022 and not stat.S_IMODE(state.st_mode) & stat.S_ISVTX:
            raise WorkspaceProvisioningError("A workspace root ancestor is writable by another principal.")


def _private_file(path: Path, *, maximum_bytes: int = MAX_RECEIPT_BYTES) -> bytes:
    descriptor = -1
    try:
        state = path.lstat()
        if (not stat.S_ISREG(state.st_mode) or state.st_uid != _uid()
                or stat.S_IMODE(state.st_mode) & 0o077 or state.st_size > maximum_bytes):
            raise WorkspaceProvisioningError("Workspace receipt is not a bounded private file.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (state.st_dev, state.st_ino):
            raise WorkspaceProvisioningError("Workspace receipt changed during safe open.")
        data = bytearray()
        while len(data) <= maximum_bytes:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum_bytes:
            raise WorkspaceProvisioningError("Workspace receipt exceeded its safety bound.")
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns
        ):
            raise WorkspaceProvisioningError("Workspace receipt changed while being read.")
        return bytes(data)
    except WorkspaceProvisioningError:
        raise
    except OSError as exc:
        raise WorkspaceProvisioningError("Workspace receipt is missing or unsafe.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_receipt(repository_directory: Path) -> dict[str, Any] | None:
    if not os.path.lexists(repository_directory):
        return None
    _validate_path_ancestors(repository_directory, allow_missing=False)
    state = repository_directory.lstat()
    if state.st_uid != _uid() or stat.S_IMODE(state.st_mode) & 0o077:
        raise WorkspaceProvisioningError("Existing workspace directory is not private; it was preserved.")
    receipt_path = repository_directory / "ownership.json"
    if not os.path.lexists(receipt_path):
        raise WorkspaceProvisioningError("Existing workspace path has no ownership receipt; it was preserved.")
    try:
        receipt = json.loads(_private_file(receipt_path))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise WorkspaceProvisioningError("Existing workspace receipt is invalid; it was preserved.") from exc
    if (not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS
            or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
            or receipt.get("owner") != "dependabot-watcher"
            or receipt.get("status") not in {"provisioning", "ready"}
            or not isinstance(receipt.get("instruction_sources"), list)
            or len(receipt["instruction_sources"]) > MAX_INSTRUCTION_SOURCES
            or not all(isinstance(item, str) for item in receipt["instruction_sources"])):
        raise WorkspaceProvisioningError("Existing workspace receipt has an unsupported schema; it was preserved.")
    return receipt


def _write_receipt(repository_directory: Path, receipt: Mapping[str, Any]) -> None:
    temporary = repository_directory / ".ownership.json.tmp"
    descriptor = -1
    try:
        payload = _receipt_payload(receipt)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, repository_directory / "ownership.json")
    except OSError as exc:
        raise WorkspaceProvisioningError("Workspace ownership receipt could not be written safely.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _root_lock(root: Path) -> Iterator[None]:
    if fcntl is None:
        raise WorkspaceProvisioningError("A safe POSIX provisioning lock is unavailable.")
    lock_path = root / ".provision.lock"
    descriptor = -1
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        state = os.fstat(descriptor)
        if (not stat.S_ISREG(state.st_mode) or state.st_uid != _uid()
                or stat.S_IMODE(state.st_mode) & 0o077):
            raise WorkspaceProvisioningError("Workspace provisioning lock is not private.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except WorkspaceProvisioningError:
        raise
    except OSError as exc:
        raise WorkspaceProvisioningError("Another provisioning operation may be active; no workspace was changed.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_external_or_lazy_object_sources(source: Path, common: Path) -> None:
    """Refuse source metadata that could read objects outside the supplied checkout."""

    settings = _git(source, "config", "--local", "--null", "--list", output_limit=1_000_000)
    partial_clone_extension = False
    promisor_remote = False
    filtered_remote = False
    unsupported_ref_storage = False
    for record in settings.split(b"\0"):
        if not record:
            continue
        key, separator, value = record.partition(b"\n")
        if not separator:
            raise WorkspaceProvisioningError("Source Git configuration is malformed; no workspace was written.")
        try:
            name = key.decode("ascii").casefold()
            setting = value.decode("utf-8").strip().casefold()
        except UnicodeError as exc:
            raise WorkspaceProvisioningError("Source Git configuration is unsupported; no workspace was written.") from exc
        partial_clone_extension = partial_clone_extension or name == "extensions.partialclone"
        promisor_remote = promisor_remote or (
            name.startswith("remote.") and name.endswith(".promisor")
            and setting not in {"false", "no", "off", "0"}
        )
        filtered_remote = filtered_remote or (
            name.startswith("remote.") and name.endswith(".partialclonefilter") and bool(setting)
        )
        unsupported_ref_storage = unsupported_ref_storage or (
            name == "extensions.refstorage" and setting != "files"
        )
    if partial_clone_extension:
        raise WorkspaceProvisioningError("Partial-clone source checkouts are unsupported; no workspace was written.")
    if promisor_remote:
        raise WorkspaceProvisioningError("Promisor source checkouts are unsupported; no workspace was written.")
    if filtered_remote:
        raise WorkspaceProvisioningError("Filtered source checkouts are unsupported; no workspace was written.")
    if unsupported_ref_storage:
        raise WorkspaceProvisioningError(
            "Non-files Git reference storage is unsupported; no workspace was written."
        )

    objects = common / "objects"
    try:
        object_state = objects.lstat()
        if not stat.S_ISDIR(object_state.st_mode) or stat.S_ISLNK(object_state.st_mode):
            raise WorkspaceProvisioningError("Source object store is not a local directory; no workspace was written.")
        alternates = objects / "info" / "alternates"
        if os.path.lexists(alternates):
            raise WorkspaceProvisioningError("Borrowed Git objects are unsupported; no workspace was written.")
        pack_directory = objects / "pack"
        if os.path.lexists(pack_directory):
            pack_state = pack_directory.lstat()
            if not stat.S_ISDIR(pack_state.st_mode) or stat.S_ISLNK(pack_state.st_mode):
                raise WorkspaceProvisioningError("Source pack directory is not local; no workspace was written.")
            if any(item.name.endswith(".promisor") for item in pack_directory.iterdir()):
                raise WorkspaceProvisioningError("Promisor object packs are unsupported; no workspace was written.")
    except WorkspaceProvisioningError:
        raise
    except OSError as exc:
        raise WorkspaceProvisioningError("Source object-store metadata cannot be inspected safely.") from exc


def _source_identity(source_path: Path, repository: str) -> tuple[Path, Path, str]:
    try:
        source = source_path.resolve(strict=True)
        validate_user_owned_directory(source)
        top = Path(_git_text(source, "rev-parse", "--show-toplevel")).resolve(strict=True)
        git_directory = Path(_git_text(source, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
        common = Path(_git_text(source, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        if top != source or git_directory != common:
            raise WorkspaceProvisioningError("Provisioning source must be an owned main checkout, not a linked worktree.")
        validate_user_owned_directory(git_directory)
        _reject_external_or_lazy_object_sources(source, common)
        remote = _git_text(source, "config", "--local", "--get-all", "remote.origin.url")
        try:
            origin_repository = _github_repository(remote)
        except ReviewQueueError as exc:
            raise WorkspaceProvisioningError(
                "Provisioning source origin is not a supported credential-free GitHub identity."
            ) from exc
        if "\n" in remote or origin_repository.casefold() != repository.casefold():
            raise WorkspaceProvisioningError("Provisioning source origin does not match the configured repository.")
        head = _git_text(source, "rev-parse", "--verify", "HEAD^{commit}")
        if not _SHA.fullmatch(head):
            raise WorkspaceProvisioningError("Provisioning source has no valid committed HEAD.")
        return source, common, head
    except WorkspaceProvisioningError:
        raise
    except (OSError, RuntimeStateError, ValueError) as exc:
        raise WorkspaceProvisioningError("Provisioning source identity or ownership could not be verified.") from exc


def source_checkout_git_directory(source_path: Path, repository: str) -> Path:
    """Resolve the common Git metadata directory without modifying the source."""

    _source, common, _head = _source_identity(source_path, repository)
    return common


def _ref_metadata_bytes(source: Path, common: Path, *, allocation_unit: int) -> int:
    """Bound refs copied by `git clone --local`, including loose and packed refs."""

    ref_list = _git(
        source,
        "for-each-ref", "--format=%(refname)",
        output_limit=MAX_REF_LIST_OUTPUT_BYTES,
    )
    references = ref_list.splitlines()
    if len(references) > MAX_REF_METADATA_ENTRIES:
        raise WorkspaceProvisioningError("Source reference count exceeds the bounded clone metadata limit.")
    total = 0
    nodes = 0
    stack: list[Path] = []
    refs_directory = common / "refs"
    try:
        if os.path.lexists(refs_directory):
            state = refs_directory.lstat()
            if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
                raise WorkspaceProvisioningError("Source refs path is not a local directory; no workspace was written.")
            stack.append(refs_directory)
        while stack:
            directory = stack.pop()
            total += allocation_unit + CHECKOUT_DIRECTORY_METADATA_RESERVE
            for entry in os.scandir(directory):
                state = entry.stat(follow_symlinks=False)
                nodes += 1
                if nodes > MAX_REF_METADATA_ENTRIES:
                    raise WorkspaceProvisioningError("Source refs tree exceeds the bounded clone metadata limit.")
                if stat.S_ISLNK(state.st_mode):
                    raise WorkspaceProvisioningError("Source refs tree contains a symlink; no workspace was written.")
                if stat.S_ISDIR(state.st_mode):
                    stack.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(state.st_mode):
                    raise WorkspaceProvisioningError("Source refs tree contains an unsupported file type.")
                allocated = max(
                    allocation_unit,
                    ((state.st_size + allocation_unit - 1) // allocation_unit) * allocation_unit,
                )
                total += allocated + REF_METADATA_ENTRY_RESERVE
                if total > MAX_REF_METADATA_BYTES:
                    raise WorkspaceProvisioningError("Source refs metadata exceeds the bounded clone metadata size.")
        packed_refs = common / "packed-refs"
        if os.path.lexists(packed_refs):
            state = packed_refs.lstat()
            if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
                raise WorkspaceProvisioningError("Source packed-refs is not a regular local file.")
            total += max(
                allocation_unit,
                ((state.st_size + allocation_unit - 1) // allocation_unit) * allocation_unit,
            ) + REF_METADATA_ENTRY_RESERVE
        head = common / "HEAD"
        head_state = head.lstat()
        if stat.S_ISLNK(head_state.st_mode) or not stat.S_ISREG(head_state.st_mode):
            raise WorkspaceProvisioningError("Source HEAD metadata is not a regular local file.")
        total += max(
            allocation_unit,
            ((head_state.st_size + allocation_unit - 1) // allocation_unit) * allocation_unit,
        ) + REF_METADATA_ENTRY_RESERVE
    except WorkspaceProvisioningError:
        raise
    except OSError as exc:
        raise WorkspaceProvisioningError("Source refs metadata cannot be inspected safely.") from exc
    total += len(references) * CLONED_REF_METADATA_RESERVE
    if total > MAX_REF_METADATA_BYTES:
        raise WorkspaceProvisioningError("Source refs metadata exceeds the bounded clone metadata size.")
    return total


def _object_store_bytes(source: Path, *, allocation_unit: int) -> int:
    alternates = Path(_git_text(source, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternates.is_absolute():
        alternates = source / alternates
    try:
        if os.path.lexists(alternates) and alternates.stat().st_size:
            raise WorkspaceProvisioningError(
                "Source checkout borrows Git objects from another repository; provisioning stopped before writes."
            )
    except OSError as exc:
        raise WorkspaceProvisioningError("Source object-store metadata cannot be inspected safely.") from exc
    output = _git_text(source, "count-objects", "-v")
    values: dict[str, int] = {}
    for line in output.splitlines():
        name, separator, raw = line.partition(":")
        if separator and name in {"count", "size", "in-pack", "packs", "size-pack"}:
            try:
                values[name] = int(raw.strip())
            except ValueError as exc:
                raise WorkspaceProvisioningError("Source object-store size could not be bounded.") from exc
    if set(values) != {"count", "size", "in-pack", "packs", "size-pack"} or any(
        value < 0 for value in values.values()
    ):
        raise WorkspaceProvisioningError("Source object-store size could not be bounded.")
    return (
        (values["size"] + values["size-pack"]) * 1024
        + values["count"] * allocation_unit
        + values["in-pack"] * PACKED_OBJECT_METADATA_RESERVE
        + values["packs"] * allocation_unit
    )


def _tree_entries(source: Path, base_sha: str) -> tuple[dict[str, tuple[str, str, str, int]], int]:
    output = _git(source, "ls-tree", "-r", "-l", "-z", "--full-tree", base_sha,
                  output_limit=MAX_TREE_OUTPUT_BYTES)
    entries: dict[str, tuple[str, str, str, int]] = {}
    checkout_bytes = 0
    records = output.split(b"\0")
    if len(records) > MAX_TREE_ENTRIES + 1:
        raise WorkspaceProvisioningError("Repository tree exceeds the bounded workspace file-count limit.")
    for record in records:
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        try:
            # `git ls-tree -l` pads the size column with multiple spaces before
            # the tab-delimited path; parse its ASCII metadata as whitespace.
            fields = metadata.decode("ascii").split()
            relative = raw_path.decode("utf-8")
        except UnicodeError as exc:
            raise WorkspaceProvisioningError("Repository paths or tree metadata cannot be safely bounded.") from exc
        if not separator or len(fields) != 4:
            raise WorkspaceProvisioningError("Repository tree metadata is malformed; provisioning stopped.")
        mode, object_type, object_id, raw_size = fields
        if mode == "160000" or object_type == "commit":
            raise WorkspaceProvisioningError("Supervisor maintenance workspaces do not support Git submodules yet.")
        if object_type != "blob" or not _SHA.fullmatch(object_id):
            raise WorkspaceProvisioningError("Repository tree contains unsupported Git metadata.")
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise WorkspaceProvisioningError("Repository file size cannot be safely bounded.") from exc
        if (not relative or relative.startswith("/") or ".." in PurePosixPath(relative).parts
                or size < 0 or relative in entries):
            raise WorkspaceProvisioningError("Repository tree contains an unsafe or duplicate path.")
        entries[relative] = (mode, object_type, object_id, size)
        checkout_bytes += size
        if checkout_bytes > MAX_WORKSPACE_BYTES:
            raise WorkspaceProvisioningError("Pinned checkout exceeds the hard 16 GiB safety limit.")
    return entries, checkout_bytes


def _validate_checkout_expansion_attributes(source: Path, base_sha: str) -> None:
    """Refuse pinned attribute rules with unbounded expansion or text transcoding."""

    pattern = r"(^|[[:space:]])(ident|working-tree-encoding)([=[:space:]]|$)"
    matches = _git(
        source,
        "grep", "-I", "-n", "-E", pattern, base_sha, "--", ".gitattributes", "**/.gitattributes",
        allow_empty=True,
        timeout_seconds=60,
        output_limit=MAX_ATTRIBUTE_OUTPUT_BYTES,
    )
    if matches:
        raise WorkspaceProvisioningError(
            "Pinned .gitattributes uses ident or working-tree-encoding; bounded checkout sizing cannot be established."
        )


def _normalize_relative_target(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.is_absolute() or "\x00" in path or any(ord(character) < 32 for character in path):
        raise WorkspaceProvisioningError("Repository AGENTS.md symlink has an unsupported target.")
    parts: list[str] = []
    for part in pure.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise WorkspaceProvisioningError(
                    "Committed AGENTS.md has a relative external symlink that would change after relocation; "
                    "no workspace files were written."
                )
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise WorkspaceProvisioningError("Repository AGENTS.md symlink target is not a file.")
    return "/".join(parts)


def _bounded_instruction_blob(source: Path, object_id: str) -> None:
    payload = _git(source, "cat-file", "blob", object_id, output_limit=1_000_001)
    if len(payload) > 1_000_000:
        raise WorkspaceProvisioningError("Pinned AGENTS.md exceeds its one-megabyte trust bound.")
    try:
        payload.decode("utf-8")
    except UnicodeError as exc:
        raise WorkspaceProvisioningError("Pinned AGENTS.md is not UTF-8 text.") from exc


def _root_agent_source(
    source: Path,
    workspace: Path,
    entries: Mapping[str, tuple[str, str, str, int]],
) -> tuple[str, ...]:
    entry = entries.get("AGENTS.md")
    if entry is None:
        return ()
    mode, _kind, object_id, _size = entry
    if mode not in {"100644", "100755", "120000"}:
        raise WorkspaceProvisioningError("Repository root AGENTS.md has an unsupported Git mode.")
    if mode != "120000":
        _bounded_instruction_blob(source, object_id)
        return (str(workspace / "AGENTS.md"),)
    raw_bytes = _git(source, "cat-file", "blob", object_id, output_limit=4096)
    try:
        target_text = raw_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise WorkspaceProvisioningError("Committed AGENTS.md symlink target is not UTF-8.") from exc
    if not target_text or target_text != target_text.strip() or any(ord(char) < 32 for char in target_text):
        raise WorkspaceProvisioningError("Committed AGENTS.md symlink target is malformed.")
    target = Path(target_text)
    if target.is_absolute():
        if target.name.startswith(".env") or "secrets" in target.parts:
            raise WorkspaceProvisioningError("Secret files cannot be registered as supervisor instructions.")
        try:
            _instruction_fingerprint(target)
        except Exception as exc:
            raise WorkspaceProvisioningError("Committed absolute AGENTS.md target is unavailable or untrusted.") from exc
        return (str(workspace / "AGENTS.md"),)

    current = _normalize_relative_target(target_text)
    visited = {"AGENTS.md"}
    for _ in range(16):
        if current in visited:
            raise WorkspaceProvisioningError("Committed AGENTS.md symlink chain contains a loop.")
        visited.add(current)
        if current.startswith(".env") or "secrets" in PurePosixPath(current).parts:
            raise WorkspaceProvisioningError("Secret files cannot be registered as supervisor instructions.")
        target_entry = entries.get(current)
        if target_entry is None:
            raise WorkspaceProvisioningError("Committed AGENTS.md relative symlink target is absent from its pinned tree.")
        target_mode, target_kind, target_object, _target_size = target_entry
        if target_kind != "blob" or target_mode not in {"100644", "100755", "120000"}:
            raise WorkspaceProvisioningError("Committed AGENTS.md symlink target is not a regular text file.")
        if target_mode != "120000":
            _bounded_instruction_blob(source, target_object)
            return (str(workspace / "AGENTS.md"),)
        try:
            nested_text = _git(source, "cat-file", "blob", target_object, output_limit=4096).decode("utf-8")
        except UnicodeError as exc:
            raise WorkspaceProvisioningError("Committed AGENTS.md symlink chain is not UTF-8.") from exc
        nested = PurePosixPath(nested_text)
        if nested.is_absolute():
            raise WorkspaceProvisioningError("Nested absolute AGENTS.md symlinks are unsupported.")
        current = _normalize_relative_target(str(PurePosixPath(current).parent / nested))
    raise WorkspaceProvisioningError("Committed AGENTS.md symlink chain exceeds its safety bound.")


def _ancestor_instruction_sources(directory: Path) -> tuple[str, ...]:
    paths: list[str] = []
    chain = [directory, *directory.parents]
    for ancestor in reversed(chain):
        candidate = ancestor / "AGENTS.md"
        if not os.path.lexists(candidate):
            continue
        try:
            _instruction_fingerprint(candidate)
        except Exception as exc:
            raise WorkspaceProvisioningError(
                f"Discovered ancestor instruction source is unavailable or untrusted: {candidate}."
            ) from exc
        paths.append(str(candidate))
        if len(paths) > MAX_INSTRUCTION_SOURCES:
            raise WorkspaceProvisioningError("Ancestor instruction chain exceeds its 32-source bound.")
    return tuple(paths)


def _instruction_sources(
    source: Path,
    workspace: Path,
    entries: Mapping[str, tuple[str, str, str, int]],
    explicit: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    workspace_ancestors = _ancestor_instruction_sources(workspace.parent)
    source_ancestors = _ancestor_instruction_sources(source.parent)
    discovered = workspace_ancestors + _root_agent_source(source, workspace, entries)
    if len(discovered) > MAX_INSTRUCTION_SOURCES:
        raise WorkspaceProvisioningError("Discovered instruction chain exceeds its 32-source bound.")
    if explicit:
        selected: list[str] = []
        for raw in explicit:
            path = Path(raw).expanduser()
            if not path.is_absolute() or ".." in path.parts:
                raise WorkspaceProvisioningError("Explicit instruction sources must be absolute and traversal-free.")
            normalized = str(Path(os.path.abspath(path)))
            if normalized in selected:
                raise WorkspaceProvisioningError("Explicit instruction source list contains duplicates.")
            if normalized == str(workspace / "AGENTS.md"):
                if not _root_agent_source(source, workspace, entries):
                    raise WorkspaceProvisioningError("Explicit workspace AGENTS.md is absent at the pinned base.")
            else:
                try:
                    _instruction_fingerprint(Path(normalized))
                except Exception as exc:
                    raise WorkspaceProvisioningError("Explicit instruction source is unavailable or untrusted.") from exc
            selected.append(normalized)
        selected_sources = tuple(selected)
    else:
        selected_sources = discovered
    if len(selected_sources) > MAX_INSTRUCTION_SOURCES or len(set(selected_sources)) != len(selected_sources):
        raise WorkspaceProvisioningError("Expected instruction sources are duplicated or exceed their bound.")
    return discovered, selected_sources, source_ancestors, workspace_ancestors


def _preflight_one(
    request: WorkspaceProvisionRequest,
    *,
    root: Path,
    offline: bool,
    maximum_bytes: int,
) -> _Preflight:
    repository = normalize_repository_name(request.repository, "supervisor provision repository")
    source, source_common, local_head = _source_identity(request.source_checkout, repository)
    workspace = _absolute_path(request.workspace_directory)
    expected_workspace = workspace_directory_for(root, repository)
    if workspace != expected_workspace or workspace.name != "worktree":
        raise WorkspaceProvisioningError("Workspace path does not match the stable private layout.")
    root = _absolute_path(root)
    if source.is_relative_to(root) or root.is_relative_to(source):
        raise WorkspaceProvisioningError("Private workspace root must be separate from the source checkout.")
    if source_common.is_relative_to(root) or root.is_relative_to(source_common):
        raise WorkspaceProvisioningError(
            "Private workspace root must be separate from the source Git metadata directory."
        )
    _validate_path_ancestors(root, allow_missing=True)
    _validate_path_ancestors(workspace.parent, allow_missing=True)

    if offline:
        base_sha = local_head
        provenance = "local_unverified_offline"
    else:
        base_sha = request.collected_base_sha or ""
        provenance = "github_collected_sha"
        if not _SHA.fullmatch(base_sha):
            raise WorkspaceProvisioningError("Online provisioning requires the exact GitHub-collected commit SHA.")
    try:
        _git(source, "cat-file", "-e", f"{base_sha}^{{commit}}")
    except WorkspaceProvisioningError as exc:
        if offline:
            raise WorkspaceProvisioningError("Offline source HEAD is not available as a commit; no files were written.") from exc
        raise WorkspaceProvisioningError(
            "The exact GitHub-collected base is absent from the supplied checkout. "
            "This setup path does not acquire missing commits; no files were written."
        ) from exc

    entries, _logical_checkout_bytes = _tree_entries(source, base_sha)
    _validate_checkout_expansion_attributes(source, base_sha)
    allocation_unit = _allocation_unit(workspace.parent)
    checkout_bytes = _checkout_allocation_estimate(
        entries,
        allocation_unit=allocation_unit,
    )
    object_bytes = _object_store_bytes(source, allocation_unit=allocation_unit)
    ref_metadata_bytes = _ref_metadata_bytes(source, source_common, allocation_unit=allocation_unit)
    # A private clone copies Git objects without shared hardlinks or alternates.
    # Include allocation-unit rounding and a bounded per-file/directory reserve
    # for the checkout, plus small clone metadata. It remains an estimate, not
    # a hard quota.
    estimated_bytes = object_bytes + checkout_bytes + ref_metadata_bytes + 1024**2
    discovered, selected, source_ancestors, workspace_ancestors = _instruction_sources(
        source, workspace, entries, request.explicit_instruction_sources
    )
    repository_directory = workspace.parent
    clone_directory = repository_directory / "source-clone"
    existing = _read_receipt(repository_directory)
    plan = _Preflight(
        request=request,
        source=source,
        root=root,
        repository_directory=repository_directory,
        clone_directory=clone_directory,
        workspace=workspace,
        base_sha=base_sha,
        base_provenance=provenance,
        source_common_directory=source_common,
        estimated_bytes=estimated_bytes,
        checkout_bytes=checkout_bytes,
        discovered_sources=discovered,
        source_ancestor_sources=source_ancestors,
        workspace_ancestor_sources=workspace_ancestors,
        instruction_sources=selected,
        existing_receipt=existing,
    )
    if existing is not None:
        expected = _receipt(plan, status=str(existing.get("status")), estimated_bytes=existing.get("estimated_bytes", 0))
        if any(existing.get(key) != expected.get(key) for key in (
            "repository", "source_checkout", "clone_directory", "workspace_directory",
            "base_sha", "base_provenance", "branch", "instruction_sources",
        )):
            raise WorkspaceProvisioningError(
                "An owned workspace exists for a different source, base, branch or instruction chain; it was preserved."
            )
    elif os.path.lexists(repository_directory):
        raise WorkspaceProvisioningError("Existing workspace path is not provisioner-owned; it was preserved.")
    if _storage_growth_bytes(plan) > maximum_bytes:
        raise WorkspaceProvisioningError(
            f"Additional private object store plus pinned checkout is estimated at {_storage_growth_bytes(plan)} bytes, "
            f"above the configured workspace limit of {maximum_bytes}."
        )
    for status in ("provisioning", "ready"):
        _receipt_payload(_receipt(plan, status=status, estimated_bytes=estimated_bytes))
    return plan


def _validate_reserve_paths(paths: Sequence[Path], *, required: bool) -> tuple[Path, ...]:
    if required and not paths:
        raise WorkspaceProvisioningError(
            "WSL provisioning requires an explicit physical-volume reserve path "
            "(for example --supervisor-validation-reserve-path /mnt/c)."
        )
    normalized: list[Path] = []
    for raw in paths:
        path = _absolute_path(raw)
        try:
            _validate_readonly_reserve_ancestors(path)
            state = path.lstat()
        except OSError as exc:
            raise WorkspaceProvisioningError("Every configured physical reserve path must already exist.") from exc
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise WorkspaceProvisioningError("Physical reserve paths must be existing non-symlink directories.")
        if path not in normalized:
            normalized.append(path)
    return tuple(normalized)


def _validate_readonly_reserve_ancestors(path: Path) -> None:
    """Validate a statvfs-only path without applying private-destination mode rules."""

    path = _absolute_path(path)
    for component in reversed(path.parents):
        try:
            state = component.lstat()
        except OSError as exc:
            raise WorkspaceProvisioningError("Physical reserve path ancestors must already exist.") from exc
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise WorkspaceProvisioningError("Physical reserve paths cannot traverse links or non-directories.")
    try:
        state = path.lstat()
    except OSError as exc:
        raise WorkspaceProvisioningError("Every configured physical reserve path must already exist.") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
        raise WorkspaceProvisioningError("Physical reserve paths must be existing non-symlink directories.")


def _allocation_unit(path: Path) -> int:
    probe = _space_probe(path)
    try:
        value = os.statvfs(probe).f_frsize
    except OSError as exc:
        raise WorkspaceProvisioningError("Workspace allocation unit cannot be inspected safely.") from exc
    return value if value > 0 else DEFAULT_ALLOCATION_UNIT


def _checkout_allocation_estimate(
    entries: Mapping[str, tuple[str, str, str, int]],
    *,
    allocation_unit: int,
) -> int:
    """Estimate allocated checkout bytes plus bounded per-entry metadata overhead."""

    if allocation_unit <= 0:
        allocation_unit = DEFAULT_ALLOCATION_UNIT
    allocated_files = 0
    directory_slots = 0
    for relative, (_mode, _object_type, _object_id, size) in entries.items():
        expanded_size = size * CHECKOUT_EOL_EXPANSION_FACTOR
        allocated_files += max(
            allocation_unit,
            ((expanded_size + allocation_unit - 1) // allocation_unit) * allocation_unit,
        )
        parts = PurePosixPath(relative).parts
        # Counting shared directories repeatedly deliberately overestimates.
        directory_slots += max(0, len(parts) - 1)
    metadata_reserve = (
        len(entries) * CHECKOUT_FILE_METADATA_RESERVE
        + directory_slots * CHECKOUT_DIRECTORY_METADATA_RESERVE
    )
    return allocated_files + metadata_reserve


def _space_probe(path: Path) -> Path:
    candidate = path
    while not os.path.lexists(candidate):
        if candidate.parent == candidate:
            raise WorkspaceProvisioningError("Workspace storage path has no existing ancestor.")
        candidate = candidate.parent
    _validate_path_ancestors(candidate, allow_missing=False)
    return candidate if candidate.is_dir() else candidate.parent


def _free_bytes(path: Path) -> int:
    try:
        values = os.statvfs(path)
    except OSError as exc:
        raise WorkspaceProvisioningError("A required workspace storage path cannot be inspected.") from exc
    return values.f_bavail * values.f_frsize


def _storage_growth_bytes(plan: _Preflight) -> int:
    """Estimate only bytes this attempt may add for a new or interrupted workspace."""

    receipt = plan.existing_receipt
    if receipt is None:
        return plan.estimated_bytes
    if receipt.get("status") != "provisioning" or os.path.lexists(plan.workspace):
        return 0
    if os.path.lexists(plan.clone_directory):
        return plan.checkout_bytes + 1024**2
    return plan.estimated_bytes


def _validate_capacity(
    plans: Sequence[_Preflight],
    *,
    minimum_free_bytes: int,
    reserve_paths: tuple[Path, ...],
) -> None:
    pending = [(plan, _storage_growth_bytes(plan)) for plan in plans]
    pending = [(plan, growth) for plan, growth in pending if growth > 0]
    if not pending:
        return
    required_by_device: dict[int, int] = {}
    probes_by_device: dict[int, Path] = {}
    try:
        for plan, growth in pending:
            probe = _space_probe(plan.workspace.parent)
            device = probe.stat().st_dev
            required_by_device[device] = required_by_device.get(device, 0) + growth
            probes_by_device[device] = probe
    except OSError as exc:
        raise WorkspaceProvisioningError("Workspace storage device cannot be identified safely.") from exc
    for device, estimated_bytes in required_by_device.items():
        required = estimated_bytes + minimum_free_bytes
        if _free_bytes(probes_by_device[device]) < required:
            raise WorkspaceProvisioningError(
                "Insufficient free space for all private object stores, checkouts and the configured reserve; "
                "no files were written."
            )
    total_estimated_bytes = sum(required_by_device.values())
    required_on_reserve = total_estimated_bytes + minimum_free_bytes
    for path in reserve_paths:
        if _free_bytes(path) < required_on_reserve:
            raise WorkspaceProvisioningError(
                f"Physical reserve path {path} lacks room for all estimated copies plus reserve; no files were written."
            )


def _receipt(preflight: _Preflight, *, status: str, estimated_bytes: int) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "owner": "dependabot-watcher",
        "repository": preflight.request.repository,
        "source_checkout": str(preflight.source),
        "clone_directory": str(preflight.clone_directory),
        "workspace_directory": str(preflight.workspace),
        "base_sha": preflight.base_sha,
        "base_provenance": preflight.base_provenance,
        "branch": preflight.request.branch,
        "estimated_bytes": estimated_bytes,
        "instruction_sources": list(preflight.instruction_sources),
        "status": status,
    }


def _receipt_payload(receipt: Mapping[str, Any]) -> bytes:
    payload = (json.dumps(dict(receipt), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_RECEIPT_BYTES:
        raise WorkspaceProvisioningError(
            "Expected instruction paths and ownership metadata exceed the private receipt size bound."
        )
    return payload


def _verify_clone(clone: Path, expected_origin: str) -> None:
    if not os.path.lexists(clone):
        raise WorkspaceProvisioningError("Owned receipt refers to a missing private clone; no files were removed.")
    validate_user_owned_directory(clone)
    resolved = clone.resolve(strict=True)
    top = Path(_git_text(clone, "rev-parse", "--show-toplevel")).resolve(strict=True)
    common = Path(_git_text(clone, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    if top != resolved or common != resolved / ".git":
        raise WorkspaceProvisioningError("Private clone identity changed; existing state was preserved.")
    validate_user_owned_directory(common)
    remote = _git_text(clone, "config", "--local", "--get-all", "remote.origin.url")
    if remote != expected_origin:
        raise WorkspaceProvisioningError("Private clone origin changed; existing state was preserved.")
    try:
        entries = sorted(item.name for item in clone.iterdir())
    except OSError as exc:
        raise WorkspaceProvisioningError("Private clone contents could not be inspected safely.") from exc
    if entries != [".git"]:
        raise WorkspaceProvisioningError("Private clone contains unexpected files; they were preserved.")


def _empty_template_directory(repository_directory: Path) -> Path:
    template = repository_directory / "empty-template"
    try:
        if not os.path.lexists(template):
            template.mkdir(mode=0o700)
        state = template.lstat()
        if (not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode)
                or state.st_uid != _uid() or stat.S_IMODE(state.st_mode) & 0o077
                or any(template.iterdir())):
            raise WorkspaceProvisioningError("Private Git template directory is not empty and owned; it was preserved.")
    except WorkspaceProvisioningError:
        raise
    except OSError as exc:
        raise WorkspaceProvisioningError("Private empty Git template could not be prepared safely.") from exc
    return template


def _create_private_clone(preflight: _Preflight, expected_origin: str) -> None:
    template = _empty_template_directory(preflight.repository_directory)
    _git(None, "clone", "--local", "--no-hardlinks", "--no-checkout", "--template", str(template),
         str(preflight.source), str(preflight.clone_directory), timeout_seconds=120)
    _git(preflight.clone_directory, "config", "--local", "--replace-all", "remote.origin.url", expected_origin)
    _git(preflight.clone_directory, "cat-file", "-e", f"{preflight.base_sha}^{{commit}}")


def _verify_workspace(preflight: _Preflight, expected_origin: str) -> None:
    workspace = preflight.workspace
    try:
        validate_user_owned_directory(workspace)
        resolved = workspace.resolve(strict=True)
        top = Path(_git_text(workspace, "rev-parse", "--show-toplevel")).resolve(strict=True)
        git_directory = Path(_git_text(workspace, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
        common = Path(_git_text(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        if top != resolved or git_directory == common or common != preflight.clone_directory.resolve(strict=True) / ".git":
            raise WorkspaceProvisioningError("Owned maintenance path is not the expected linked worktree; it was preserved.")
        validate_user_owned_directory(git_directory)
        validate_user_owned_directory(common)
        remote = _git_text(workspace, "config", "--local", "--get-all", "remote.origin.url")
        head = _git_text(workspace, "rev-parse", "--verify", "HEAD^{commit}")
        if remote != expected_origin or head.casefold() != preflight.base_sha.casefold():
            raise WorkspaceProvisioningError("Owned maintenance worktree identity or exact base changed; it was preserved.")
        staged = _git(workspace, "ls-files", "--stage")
        if any(line.startswith(b"160000 ") for line in staged.splitlines()):
            raise WorkspaceProvisioningError("Supervisor maintenance workspaces do not support submodules yet.")
        if _git(workspace, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all",
                config_overrides=_filter_overrides(workspace)):
            raise WorkspaceProvisioningError("Maintenance worktree has local changes; they were preserved.")
        for source in preflight.instruction_sources:
            _instruction_fingerprint(Path(source))
    except WorkspaceProvisioningError:
        raise
    except (OSError, RuntimeStateError, ValueError) as exc:
        raise WorkspaceProvisioningError("Owned maintenance worktree could not be verified; it was preserved.") from exc


def _add_worktree(preflight: _Preflight) -> None:
    if os.path.lexists(preflight.workspace):
        raise WorkspaceProvisioningError("Maintenance workspace path already exists; it was preserved.")
    _git(
        preflight.clone_directory, "worktree", "add", "--detach",
        str(preflight.workspace), preflight.base_sha, timeout_seconds=120,
        output_limit=65_536, config_overrides=_filter_overrides(preflight.clone_directory),
    )


def _provision_one(preflight: _Preflight, *, maximum_bytes: int, minimum_free_bytes: int,
                   reserve_paths: tuple[Path, ...]) -> ProvisionedWorkspace:
    repository_directory = preflight.repository_directory
    clone = preflight.clone_directory
    origin = f"https://github.com/{preflight.request.repository}.git"
    status = "reused"
    receipt = preflight.existing_receipt
    if receipt is not None:
        if receipt.get("status") == "ready":
            _verify_clone(clone, origin)
            _verify_workspace(preflight, origin)
        elif receipt.get("status") == "provisioning":
            if os.path.lexists(preflight.workspace):
                _verify_clone(clone, origin)
                _verify_workspace(preflight, origin)
                _write_receipt(repository_directory, _receipt(
                    preflight, status="ready", estimated_bytes=preflight.estimated_bytes
                ))
            elif os.path.lexists(clone):
                _verify_clone(clone, origin)
                _add_worktree(preflight)
                _verify_workspace(preflight, origin)
                _write_receipt(repository_directory, _receipt(
                    preflight, status="ready", estimated_bytes=preflight.estimated_bytes
                ))
            else:
                try:
                    names = {item.name for item in repository_directory.iterdir()}
                except OSError as exc:
                    raise WorkspaceProvisioningError(
                        "Incomplete ownership receipt could not be inspected; existing state was preserved."
                    ) from exc
                if not names.issubset({"ownership.json", "empty-template"}):
                    raise WorkspaceProvisioningError(
                        "Incomplete ownership receipt has unexpected files; existing state was preserved."
                    )
                _create_private_clone(preflight, origin)
                _add_worktree(preflight)
                _verify_clone(clone, origin)
                _verify_workspace(preflight, origin)
                _write_receipt(repository_directory, _receipt(
                    preflight, status="ready", estimated_bytes=preflight.estimated_bytes
                ))
        else:
            raise WorkspaceProvisioningError("Ownership receipt and workspace state disagree; files were preserved.")
    else:
        if os.path.lexists(repository_directory):
            raise WorkspaceProvisioningError("Workspace path appeared after preflight; it was preserved.")
        repository_directory.mkdir(mode=0o700)
        state = repository_directory.stat()
        if state.st_uid != _uid() or stat.S_IMODE(state.st_mode) & 0o077:
            raise WorkspaceProvisioningError("Per-repository workspace directory is not private.")
        _write_receipt(repository_directory, _receipt(
            preflight, status="provisioning", estimated_bytes=preflight.estimated_bytes
        ))
        _create_private_clone(preflight, origin)
        _add_worktree(preflight)
        _verify_clone(clone, origin)
        _verify_workspace(preflight, origin)
        _write_receipt(repository_directory, _receipt(
            preflight, status="ready", estimated_bytes=preflight.estimated_bytes
        ))
        status = "created"
    registration = SupervisorWorkspaceConfig(
        repository=preflight.request.repository,
        directory=str(preflight.workspace),
        instruction_sources=preflight.instruction_sources,
        refresh_authorized=preflight.request.refresh_authorized,
    )
    return ProvisionedWorkspace(
        registration=registration,
        status=status,
        source_checkout=str(preflight.source),
        base_sha=preflight.base_sha,
        base_provenance=preflight.base_provenance,
        estimated_bytes=preflight.estimated_bytes,
        maximum_bytes=maximum_bytes,
        minimum_free_bytes=minimum_free_bytes,
        reserve_paths=tuple(str(path) for path in reserve_paths),
        discovered_instruction_sources=preflight.discovered_sources,
        instruction_sources=preflight.instruction_sources,
        source_ancestor_instruction_sources=preflight.source_ancestor_sources,
        workspace_ancestor_instruction_sources=preflight.workspace_ancestor_sources,
    )


def provision_supervisor_workspaces(
    requests: Sequence[WorkspaceProvisionRequest],
    *,
    root: Path,
    offline: bool,
    maximum_bytes: int = DEFAULT_WORKSPACE_MAX_BYTES,
    minimum_free_bytes: int = DEFAULT_WORKSPACE_MINIMUM_FREE_BYTES,
    reserve_paths: Sequence[Path] = (),
) -> tuple[ProvisionedWorkspace, ...]:
    """Preflight every repository, then create or reuse persistent private copies."""

    if os.name != "posix" or fcntl is None:
        raise WorkspaceProvisioningError("Automatic supervisor workspace provisioning currently requires POSIX/WSL.")
    if not 1 <= len(requests) <= 20:
        raise WorkspaceProvisioningError("Provisioning requires between one and twenty repositories.")
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_WORKSPACE_BYTES:
        raise WorkspaceProvisioningError("Workspace maximum bytes must be between one byte and 16 GiB.")
    if type(minimum_free_bytes) is not int or not 1 <= minimum_free_bytes <= 64 * 1024**3:
        raise WorkspaceProvisioningError("Workspace free-space reserve is outside its supported bound.")
    root = _absolute_path(root)
    _validate_root_state(root)
    normalized_reserves = _validate_reserve_paths(reserve_paths, required=_is_wsl())
    plans: list[_Preflight] = []
    seen_repositories: set[str] = set()
    seen_paths: set[str] = set()
    for request in requests:
        repository = normalize_repository_name(request.repository, "supervisor provision repository")
        key = repository.casefold()
        workspace = _absolute_path(request.workspace_directory)
        if key in seen_repositories or os.path.normcase(str(workspace)) in seen_paths:
            raise WorkspaceProvisioningError("Provisioning repositories and destination paths must be unique.")
        seen_repositories.add(key)
        seen_paths.add(os.path.normcase(str(workspace)))
        if len(request.explicit_instruction_sources) > MAX_INSTRUCTION_SOURCES:
            raise WorkspaceProvisioningError("Explicit instruction chain exceeds its 32-source bound.")
        plans.append(_preflight_one(
            request, root=root, offline=offline, maximum_bytes=maximum_bytes
        ))
    total_estimated_bytes = sum(_storage_growth_bytes(plan) for plan in plans)
    if total_estimated_bytes > maximum_bytes:
        raise WorkspaceProvisioningError(
            f"All new private object stores and checkouts are estimated at {total_estimated_bytes} bytes, "
            f"above the configured setup-wide workspace limit of {maximum_bytes}."
        )
    _validate_capacity(plans, minimum_free_bytes=minimum_free_bytes, reserve_paths=normalized_reserves)

    # This is the first filesystem mutation. Every selected input, destination,
    # base, instruction chain and storage reserve has passed preflight.
    _ensure_private_root(root)
    output: list[ProvisionedWorkspace] = []
    with _root_lock(root):
        # Recheck ownership under the lock in case another setup process created
        # or changed a destination after the read-only preflight.
        plans = [
            _preflight_one(plan.request, root=root, offline=offline, maximum_bytes=maximum_bytes)
            for plan in plans
        ]
        total_estimated_bytes = sum(_storage_growth_bytes(plan) for plan in plans)
        if total_estimated_bytes > maximum_bytes:
            raise WorkspaceProvisioningError(
                "All new private copies exceed the configured setup-wide workspace limit; no additional files were written."
            )
        _validate_capacity(plans, minimum_free_bytes=minimum_free_bytes, reserve_paths=normalized_reserves)
        for plan in plans:
            output.append(_provision_one(
                plan, maximum_bytes=maximum_bytes,
                minimum_free_bytes=minimum_free_bytes, reserve_paths=normalized_reserves,
            ))
    return tuple(output)
