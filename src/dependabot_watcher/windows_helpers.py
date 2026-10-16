from __future__ import annotations

import ctypes
import os
from pathlib import Path


class WindowsHelperError(RuntimeError):
    """Raised when a trusted Windows system helper cannot be resolved."""


def _native_system32() -> Path:
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))  # type: ignore[attr-defined]
    if length <= 0 or length >= len(buffer):
        raise WindowsHelperError("Windows System32 could not be resolved safely.")
    path = Path(buffer.value)
    if not path.is_absolute():
        raise WindowsHelperError("Windows System32 did not resolve to an absolute path.")
    return path.resolve(strict=True)


def _wsl_system32() -> Path:
    if not os.getenv("WSL_DISTRO_NAME"):
        raise WindowsHelperError("Windows helpers are available only on native Windows or WSL.")
    path = Path("/mnt/c/Windows/System32")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WindowsHelperError("The trusted WSL System32 mount is unavailable.") from exc
    if resolved.as_posix().casefold() != "/mnt/c/windows/system32":
        raise WindowsHelperError("The WSL System32 mount did not resolve canonically.")
    return resolved


def trusted_windows_executable(relative_path: str) -> Path:
    """Resolve one allowlisted helper below the authoritative System32 directory."""

    normalized = relative_path.replace("\\", "/").strip("/")
    if normalized.casefold() not in {
        "wsl.exe",
        "wscript.exe",
        "windowspowershell/v1.0/powershell.exe",
    }:
        raise WindowsHelperError("Unsupported Windows system helper requested.")
    system32 = _native_system32() if os.name == "nt" else _wsl_system32()
    candidate = system32.joinpath(*normalized.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WindowsHelperError("The trusted Windows system helper is unavailable.") from exc
    try:
        resolved.relative_to(system32)
    except ValueError as exc:
        raise WindowsHelperError("Windows system helper escaped System32.") from exc
    if not resolved.is_file():
        raise WindowsHelperError("The trusted Windows system helper is unavailable.")
    return resolved


def windows_host_path(path: Path) -> str:
    """Return a Windows command path for a native or WSL-resolved helper."""

    if os.name == "nt":
        return str(path)
    parts = path.as_posix().split("/")
    if len(parts) > 4 and parts[1] == "mnt" and len(parts[2]) == 1:
        return f"{parts[2].upper()}:\\" + "\\".join(parts[3:])
    raise WindowsHelperError("WSL helper path could not be converted to a Windows host path.")
