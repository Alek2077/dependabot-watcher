"""Thin WSL-to-Windows review launcher; no model or action authority."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .runtime_state import ensure_private_directory, load_private_json, save_private_json
from .windows_helpers import trusted_windows_executable, windows_host_path


class NativeReviewError(RuntimeError):
    """The optional native review component cannot safely be opened."""


def _wsl_identity() -> tuple[str, str]:
    distribution = os.getenv("WSL_DISTRO_NAME", "")
    if os.name != "posix" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}", distribution):
        raise NativeReviewError("The Windows review component currently requires a WSL installation of Watcher.")
    import pwd  # POSIX-only module; the portable CLI must still import on Windows.
    user = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise NativeReviewError("The current WSL user could not be identified safely.")
    return distribution, user


def _unc_path(path: Path, distribution: str) -> str:
    if not path.is_absolute() or "\x00" in str(path) or "\\" in str(path):
        raise NativeReviewError("The review profile requires an absolute WSL path.")
    return "\\\\wsl.localhost\\" + distribution + str(path).replace("/", "\\")


def _component(path: Path) -> tuple[Path, str]:
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.casefold() != ".exe":
        raise NativeReviewError("Select the installed optional Windows review executable, not a script or directory.")
    # Native component selection is deliberate local setup, not a field from a
    # PR, notification, model reply or arbitrary backend response.
    windows_host_path(candidate)
    with candidate.open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    return candidate, fingerprint


def open_native_review(
    config_path: Path, state_directory: Path, *, component: Path | None = None,
) -> dict[str, Any]:
    """Open an aggregate queue; registration/opening does not authorize work.

    A saved executable fingerprint prevents silently running a replaced local
    component. Deliberately select its path again after an update. CLI launch
    acknowledgement proves process submission only, not visible UI acceptance.
    """
    distribution, user = _wsl_identity()
    config_path = Path(os.path.abspath(config_path.expanduser()))
    load_config(config_path)  # Fail before creating launch state for invalid policy.
    state = ensure_private_directory(Path(os.path.abspath(state_directory.expanduser())))
    private = ensure_private_directory(state / "native-review")
    if component is None:
        if not os.path.lexists(private / "component.json"):
            raise NativeReviewError("Select the optional Windows component with review --native-component PATH first.")
        registration = load_private_json(private, Path("component.json"), maximum_bytes=8192)
        if (not isinstance(registration, dict) or set(registration) != {"schema_version", "path", "sha256"}
                or type(registration["schema_version"]) is not int or registration["schema_version"] != 1
                or not isinstance(registration["path"], str)
                or not isinstance(registration["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", registration["sha256"])):
            raise NativeReviewError("Select the optional Windows component with review --native-component PATH first.")
        candidate, fingerprint = _component(Path(registration["path"]))
        if fingerprint != registration["sha256"]:
            raise NativeReviewError("The registered Windows executable changed; deliberately select the updated component again.")
    else:
        candidate, fingerprint = _component(component)
    launch = {
        "schema_version": 2, "diagnostic_transport": False,
        "component": {"path": windows_host_path(candidate), "sha256": fingerprint},
        "arguments": ["--distribution", distribution, "--user", user, "--exec", "/usr/bin/env",
            "-u", "PYTHONPATH",
            "PATH=" + ":".join((str(Path.home() / ".local/bin"), str(Path.home() / ".npm-global/bin"),
                                  "/usr/local/bin", "/usr/bin", "/bin")),
            str(Path(sys.executable).absolute()), "-I", "-m", "dependabot_watcher.review_bridge",
            "--config", str(config_path), "--state-dir", str(state)],
    }
    # Never overwrite a launch profile under an existing window. Distinct
    # config/interpreter/WSL/component bindings get distinct native instances.
    # Match save_private_json's exact representation so the native consumer can
    # bind the one bounded byte snapshot without cross-language JSON rewriting.
    profile_id = hashlib.sha256(
        (json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    profiles = ensure_private_directory(private / "profiles")
    profile = ensure_private_directory(profiles / profile_id)
    if os.path.lexists(profile / "backend-launch.json"):
        if load_private_json(profile, Path("backend-launch.json"), maximum_bytes=65536) != launch:
            raise NativeReviewError("The saved native launch profile changed; do not reuse it.")
    else:
        save_private_json(profile, Path("backend-launch.json"), launch)
    # This records deliberate component selection, not successful activation.
    # Finish all required local writes before Windows can start a child; a
    # disk failure must not disguise an already submitted process as no launch.
    save_private_json(private, Path("component.json"),
                      {"schema_version": 1, "path": str(candidate), "sha256": fingerprint})
    arguments = ["--launch-file", _unc_path(profile / "backend-launch.json", distribution)]
    # No shell interpolation of paths or user content. PowerShell reads typed
    # JSON and passes a Windows-quoted argument string to the selected exe.
    payload = {"executable": windows_host_path(candidate), "arguments": subprocess.list2cmdline(arguments)}
    script = r"""
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$process = Start-Process -FilePath ([string]$payload.executable) -ArgumentList ([string]$payload.arguments) -PassThru
@{pid=$process.Id; status='submitted'} | ConvertTo-Json -Compress
""".strip()
    try:
        result = subprocess.run([str(trusted_windows_executable("WindowsPowerShell/v1.0/powershell.exe")),
            "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload),
            capture_output=True, text=True, timeout=30, check=False)
        receipt = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise NativeReviewError("Windows did not confirm the review launch. No agent or merge was requested.") from exc
    if (result.returncode != 0 or not isinstance(receipt, dict) or set(receipt) != {"pid", "status"}
            or type(receipt["pid"]) is not int or receipt["pid"] <= 0 or receipt["status"] != "submitted"):
        raise NativeReviewError("Windows did not confirm the review launch. No agent or merge was requested.")
    return {"schema_version": 1, "status": "submitted", "pid": receipt["pid"],
            "configuration_path": str(config_path), "state_directory": str(state),
            "assurance": "process_submission_only", "model_calls": False, "github_mutations": False}
