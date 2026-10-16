"""Fixed, inspectable windowless launchers; never accept arbitrary script bodies."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import PureWindowsPath

from .common import ScheduleError


def launcher_source(argv: list[str]) -> str:
    if not argv or any(not isinstance(value, str) or any(c in value for c in "%\x00\r\n") for value in argv):
        # WScript.Shell.Run expands %variables% even inside quoted arguments.
        raise ScheduleError("Windowless schedule arguments cannot contain percent signs or control characters.")
    command = subprocess.list2cmdline(argv)
    if len(command) >= 32767:
        raise ScheduleError("Windowless schedule command exceeds the Windows command-line limit.")
    source = (
        "// Dependabot Watcher windowless launcher v1\n"
        + "// argv: " + json.dumps(argv, ensure_ascii=True) + "\n"
        + "if (WScript.Arguments.length !== 0) WScript.Quit(64);\n"
        + "try {\n"
        + "    var shell = new ActiveXObject(\"WScript.Shell\");\n"
        + "    WScript.Quit(shell.Run(" + json.dumps(command, ensure_ascii=True) + ", 0, true));\n"
        + "} catch (error) { WScript.Quit(70); }\n"
    )
    if len(source.encode("utf-8")) > 65536:
        raise ScheduleError("Windowless launcher exceeds the bounded script size.")
    return source


def launcher_path(directory: str, job_id: str, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return str(PureWindowsPath(directory) / f"{job_id}-{digest}.js")


def verified_launcher_argv(directory: str, job_id: str, path: str, source: object) -> list[str]:
    if not isinstance(source, str) or len(source) > 65536:
        raise ScheduleError("Windowless launcher is missing or unreadable.")
    try:
        argv = json.loads(source.splitlines()[1].removeprefix("// argv: "))
    except (ValueError, IndexError, RecursionError) as exc:
        raise ScheduleError("Windowless launcher metadata is invalid.") from exc
    if not isinstance(argv, list) or source != launcher_source(argv):
        raise ScheduleError("Windowless launcher content does not match its fixed template.")
    if PureWindowsPath(path) != PureWindowsPath(launcher_path(directory, job_id, source)):
        raise ScheduleError("Windowless launcher path or fingerprint does not match.")
    return argv
