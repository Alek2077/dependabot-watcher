"""Read-only verification of an explicitly registered isolated Git workspace.

No clone, checkout, fetch, reset or instruction rewriting lives here. This
initial POSIX/WSL launcher accepts linked worktrees, not a user's main checkout.
Repository identity and harness fingerprints are evidence, not a sandbox or
permission to run untrusted tests with unrestricted credentials.
"""
from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .config import SupervisorWorkspaceConfig, WatcherConfig, normalize_repository_name
from .review_queue import ReviewConfigurationChanged, ReviewQueueError, _validate_workspace_evidence
from .runtime_state import RuntimeStateError, validate_user_owned_directory


_MAX_GIT_OUTPUT = 65_536
_MAX_INSTRUCTION = 1_000_000
_SHA = re.compile(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}")


def _git(workspace: Path, *arguments: str, overrides: tuple[str, ...] = (), empty_ok: bool = False) -> str:
    """Bound both pipes and lifetime; discard diagnostics and remote URLs."""
    executable = shutil.which("git")
    if executable is None:
        raise ReviewQueueError("Git is unavailable; no supervisor workspace was verified.")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    command = [executable, "--no-optional-locks", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=/dev/null"]
    for override in overrides:
        command.extend(["-c", override])
    command.extend(["-C", str(workspace), *arguments])
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        output = bytearray()
        totals = {"stdout": 0, "stderr": 0}
        deadline = time.monotonic() + 15
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReviewQueueError("Supervisor workspace Git verification timed out.")
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    totals[key.data] += len(chunk)
                    if totals[key.data] > _MAX_GIT_OUTPUT:
                        raise ReviewQueueError("Supervisor workspace Git evidence exceeds its bound.")
                    if key.data == "stdout":
                        output.extend(chunk)
            code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            if code == 1 and empty_ok and not output:
                return ""
            if code != 0:
                raise ReviewQueueError("Supervisor workspace Git verification failed; private details withheld.")
        return output.decode("utf-8").rstrip("\n")
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ReviewQueueError("Supervisor workspace Git evidence is unavailable; private details withheld.") from exc
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


def _github_repository(remote: str) -> str:
    """Recognize ordinary GitHub remotes without retaining credentials."""
    if not remote or any(character.isspace() or ord(character) < 32 for character in remote):
        raise ReviewQueueError("Supervisor workspace origin has an ambiguous identity.")
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:"):]
    else:
        parsed = urlsplit(remote)
        if (parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com"
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port not in ({None, 443} if parsed.scheme == "https" else {None, 22})
                or parsed.username not in ({None} if parsed.scheme == "https" else {"git"})):
            raise ReviewQueueError("Supervisor workspace origin is not a supported credential-free GitHub identity.")
        path = parsed.path.removeprefix("/")
    return normalize_repository_name(path.removesuffix(".git"), "supervisor workspace origin")


def _instruction_fingerprint(path: Path) -> str:
    """Hash selected trusted instructions, never journal their contents.

    An explicitly selected AGENTS symlink is supported; its owned regular
    target is verified and the link must still resolve to that target afterward.
    This does not establish Codex's discovery or interpretation of the content.
    """
    if path.name.startswith(".env") or "secrets" in path.parts:
        raise ReviewQueueError("Secret files cannot be registered as supervisor instructions.")
    descriptor = -1
    try:
        resolved = path.resolve(strict=True)
        if resolved.name.startswith(".env") or "secrets" in resolved.parts:
            raise ReviewQueueError("Secret files cannot be registered as supervisor instructions.")
        validate_user_owned_directory(resolved.parent)
        state = resolved.lstat()
        if (not stat.S_ISREG(state.st_mode) or state.st_size > _MAX_INSTRUCTION
                or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o022):
            raise ReviewQueueError("Supervisor instruction source is not a bounded trusted regular file.")
        descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (state.st_dev, state.st_ino):
            raise ReviewQueueError("Supervisor instruction source changed during safe open.")
        payload = bytearray()
        while len(payload) <= _MAX_INSTRUCTION:
            chunk = os.read(descriptor, min(65_536, _MAX_INSTRUCTION + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (len(payload) > _MAX_INSTRUCTION or path.resolve(strict=True) != resolved
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)):
            raise ReviewQueueError("Supervisor instruction source changed during verification.")
        bytes(payload).decode("utf-8")
        return hashlib.sha256(payload).hexdigest()
    except (OSError, UnicodeError, RuntimeStateError) as exc:
        raise ReviewQueueError("Supervisor instruction source is unavailable or untrusted.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class SupervisorWorkspaceEvidence:
    repository: str
    directory: str
    git_directory: str
    common_directory: str
    head_sha: str
    instruction_fingerprints: tuple[tuple[str, str], ...]


class SupervisorAuthorityChanged(ReviewConfigurationChanged):
    """The selected trusted instructions no longer match this attempt."""


def verify_supervisor_runtime_binding(context: Mapping[str, Any], settings: Mapping[str, Any]) -> None:
    """Bind a real/provider launch receipt to this job, not independent settings."""
    proof = context.get("workspace_evidence")
    if proof is None:
        return  # Legacy diagnostic callers are not ordinary-launch consent.
    policy = context["supervisor"]
    if (settings.get("workspace") != proof["directory"]
            or settings.get("instruction_sources") != [pair[0] for pair in proof["instruction_fingerprints"]]
            or any(settings.get(field) != policy.get(field) for field in
                   ("model", "reasoning_effort", "access", "validation_authorized"))
            or settings.get("account_type") != policy.get("expected_account_type")):
        raise ReviewQueueError("Supervisor runtime differs from its selected workspace, instructions or policy.")


class SupervisorInstructionGuard:
    """Check a job's selected instruction files, not every file in a worktree.

    Polling rehashes changed file identities/metadata; inference, grant and
    completion checkpoints force a fresh bounded read. This does not freeze
    same-user writers or prove an agent's interpretation of the instructions.
    Missing proof remains supported only for existing diagnostic jobs.
    """

    def __init__(self, evidence: Mapping[str, Any] | None) -> None:
        if evidence is not None:
            _validate_workspace_evidence(evidence)
        self.sources = tuple((path, digest) for path, digest in evidence["instruction_fingerprints"]) if evidence else ()
        self._states: dict[str, tuple[Any, ...]] = {}

    def _state(self, source: str) -> tuple[Any, ...]:
        target = Path(source).resolve(strict=True)
        validate_user_owned_directory(target.parent)
        state = target.lstat()
        return (str(target), state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns,
                state.st_ctime_ns, state.st_mode, state.st_uid)

    def verify(self, *, force: bool = False) -> None:
        try:
            for source, expected in self.sources:
                before = self._state(source)
                if force or self._states.get(source) != before:
                    if _instruction_fingerprint(Path(source)) != expected or self._state(source) != before:
                        raise SupervisorAuthorityChanged("Supervisor instructions changed; prepare a fresh investigation.")
                    self._states[source] = before
        except SupervisorAuthorityChanged:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise SupervisorAuthorityChanged("Supervisor instructions are unavailable or no longer trusted; reload before continuing.") from exc


def verify_supervisor_workspace(
    config: WatcherConfig, repository: str, repository_plan: Mapping[str, Any],
) -> SupervisorWorkspaceEvidence:
    """Verify a registered linked worktree against exact collected base evidence.

    No model/network call or filesystem mutation. A registered worktree may
    share Git objects with the primary clone; this is work isolation, not tool,
    credential or Git-configuration isolation. Other hosts remain unsupported.
    """
    if os.name != "posix":
        raise ReviewQueueError("Supervisor workspace verification currently requires the POSIX/WSL backend.")
    key = repository.casefold()
    matches = [item for item in config.supervisor.workspaces if item.repository.casefold() == key]
    if len(matches) != 1 or not any(item.name.casefold() == key for item in config.repositories):
        raise ReviewQueueError("Supervisor repository has no unique registered workspace.")
    registration: SupervisorWorkspaceConfig = matches[0]
    workspace = Path(registration.directory)
    snapshot = repository_plan.get("evidence_snapshot")
    base = snapshot.get("base_sha") if isinstance(snapshot, Mapping) else None
    if (not isinstance(repository_plan.get("repository"), str)
            or repository_plan["repository"].casefold() != key
            or not isinstance(base, str) or not _SHA.fullmatch(base)):
        raise ReviewQueueError("Supervisor workspace requires exact repository/base evidence.")
    try:
        validate_user_owned_directory(workspace)
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace:
            raise ReviewQueueError("Supervisor workspace must use its actual absolute host path.")
        top = Path(_git(workspace, "rev-parse", "--show-toplevel"))
        git_directory = Path(_git(workspace, "rev-parse", "--absolute-git-dir"))
        common = Path(_git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if top != workspace or git_directory == common:
            raise ReviewQueueError("Register an isolated linked worktree, not a main checkout or subdirectory.")
        validate_user_owned_directory(git_directory)
        validate_user_owned_directory(common)
        remote = _git(workspace, "config", "--local", "--get-all", "remote.origin.url")
        if "\n" in remote or _github_repository(remote).casefold() != key:
            raise ReviewQueueError("Supervisor workspace origin does not match the registered repository.")
        head = _git(workspace, "rev-parse", "--verify", "HEAD^{commit}")
        if not _SHA.fullmatch(head) or head.casefold() != base.casefold():
            raise ReviewQueueError("Supervisor workspace is not at the collected base commit; refresh it before launch.")
        # Git status can execute configured clean/process filters. Neutralize
        # every advertised filter before examining untrusted working files.
        # Linked worktrees with submodules need a separate checked-copy path;
        # never recurse through their independently configured Git processes.
        if any(line.startswith("160000 ") for line in _git(workspace, "ls-files", "--stage").splitlines()):
            raise ReviewQueueError("Supervisor linked-worktree verification does not support submodules yet.")
        filter_keys = _git(workspace, "config", "--name-only", "--get-regexp",
                           r"^filter\..*\.(clean|smudge|process)$", empty_ok=True).splitlines()
        if any(not re.fullmatch(r"filter\.[A-Za-z0-9_.-]+\.(clean|smudge|process)", key) for key in filter_keys):
            raise ReviewQueueError("Supervisor workspace has unsupported Git filter configuration.")
        overrides = tuple(f"{key}=" for key in filter_keys)
        if _git(workspace, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all", overrides=overrides):
            raise ReviewQueueError("Supervisor workspace has local changes; preserve them before continuing.")
        instructions = tuple((source, _instruction_fingerprint(Path(source))) for source in registration.instruction_sources)
        return SupervisorWorkspaceEvidence(repository_plan["repository"], str(workspace), str(git_directory),
                                           str(common), head, instructions)
    except (RuntimeStateError, ValueError) as exc:
        raise ReviewQueueError("Supervisor workspace identity or ownership could not be verified.") from exc
