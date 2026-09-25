from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Thread
from typing import Iterator, Mapping, TextIO


class ProgressMode(str, Enum):
    HIDDEN = "hidden"
    STATIC = "static"
    ANIMATED = "animated"


@dataclass(frozen=True)
class TerminalPresentation:
    """Presentation capabilities for one CLI invocation."""

    interactive: bool
    decorate: bool
    color: bool
    unicode_symbols: bool
    progress: ProgressMode


_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_SPINNER_FRAMES = ("-", "\\", "|", "/")
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _environment_flag(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name)
    return bool(value and value.strip().lower() not in {"0", "false", "no"})


def _supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "✓─⠋".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def sanitize_human_value(value: object) -> str:
    """Flatten untrusted display values and remove terminal control sequences."""

    text = _ANSI_ESCAPE.sub("", str(value or ""))
    text = "".join(
        " " if ord(character) < 32 or 127 <= ord(character) <= 159 else character
        for character in text
    )
    return " ".join(text.split())


def text_for_stream(value: str, stream: TextIO) -> str:
    """Replace characters unsupported by a stream without hiding report structure."""

    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        try:
            return value.encode(encoding, errors="replace").decode(encoding)
        except LookupError:
            return value
    return value


def detect_terminal_presentation(
    output_format: str,
    *,
    stdout: TextIO,
    stderr: TextIO,
    plain: bool = False,
    environment: Mapping[str, str] | None = None,
) -> TerminalPresentation:
    """Resolve terminal behavior without changing the selected report contract."""

    environment = os.environ if environment is None else environment
    interactive = output_format == "text" and _is_tty(stdout) and _is_tty(stderr)
    if not interactive:
        return TerminalPresentation(
            interactive=False,
            decorate=False,
            color=False,
            unicode_symbols=False,
            progress=ProgressMode.HIDDEN,
        )

    simple_terminal = environment.get("TERM", "").lower() == "dumb"
    continuous_log = _environment_flag(environment, "CI")
    plain_mode = plain or simple_terminal or continuous_log
    no_color = bool(environment.get("NO_COLOR")) or (
        environment.get("CLICOLOR") == "0"
    )
    return TerminalPresentation(
        interactive=True,
        decorate=not plain_mode,
        color=not plain_mode and not no_color,
        unicode_symbols=_supports_unicode(stdout) and _supports_unicode(stderr),
        progress=ProgressMode.STATIC if plain_mode else ProgressMode.ANIMATED,
    )


def _paint(value: str, *codes: str, enabled: bool) -> str:
    if not enabled:
        return value
    return "".join(codes) + value + _RESET


def _status_style(status: str, *, unicode_symbols: bool) -> tuple[str, str]:
    normalized = status.strip().lower()
    if normalized in {"complete", "created", "ready", "merged", "already_applied"}:
        return ("✓" if unicode_symbols else "[OK]"), _GREEN
    if normalized in {"partial", "attention", "disabled"}:
        return ("!" if unicode_symbols else "[!]"), _YELLOW
    if normalized in {"blocked", "error", "failed", "unavailable"}:
        return ("✗" if unicode_symbols else "[X]"), _RED
    return ("•" if unicode_symbols else "[*]"), _CYAN


def _style_line(line: str, presentation: TerminalPresentation) -> str:
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    if stripped.startswith("Status: "):
        status = stripped.split(":", 1)[1].strip()
        symbol, color = _status_style(
            status,
            unicode_symbols=presentation.unicode_symbols,
        )
        label = _paint("Status:", _BOLD, enabled=presentation.color)
        value = _paint(status, _BOLD, color, enabled=presentation.color)
        return f"{indent}{symbol} {label} {value}"
    if (
        "ERROR" in stripped
        or "WARNING" in stripped
        or stripped.startswith("Configuration error:")
        or stripped.startswith("Execution error:")
    ):
        color = _YELLOW if "WARNING" in stripped else _RED
        return indent + _paint(stripped, _BOLD, color, enabled=presentation.color)
    if (
        "TRUNCATED" in stripped
        or stripped.startswith("Blocker:")
        or stripped.startswith("Evidence is partial")
    ):
        return indent + _paint(stripped, _BOLD, _YELLOW, enabled=presentation.color)
    if stripped in {"Findings:", "Next:", "Recommended Order", "Advisory errors:"}:
        return indent + _paint(stripped, _BOLD, enabled=presentation.color)
    if stripped.startswith("Next:"):
        label, value = stripped.split(":", 1)
        return (
            indent
            + _paint(label + ":", _BOLD, _CYAN, enabled=presentation.color)
            + value
        )
    if not indent and (" (expected base:" in line or " (base:" in line):
        return _paint(line, _BOLD, enabled=presentation.color)
    if ":" in stripped and not stripped.startswith(("http://", "https://")):
        label, value = stripped.split(":", 1)
        if 0 < len(label) <= 36:
            return indent + _paint(label + ":", _BOLD, enabled=presentation.color) + value
    return line


def decorate_text_report(report: str, presentation: TerminalPresentation) -> str:
    """Add restrained interactive hierarchy while preserving plain report text."""

    if not presentation.decorate or not report:
        return report
    trailing_newline = report.endswith("\n")
    lines = report.rstrip("\n").split("\n")
    if not lines:
        return report
    title = lines[0]
    width = min(max(len(title), 24), 72)
    rule_character = "─" if presentation.unicode_symbols else "-"
    decorated = [
        _paint(title, _BOLD, _CYAN, enabled=presentation.color),
        _paint(rule_character * width, _DIM, enabled=presentation.color),
    ]
    decorated.extend(_style_line(line, presentation) for line in lines[1:])
    result = "\n".join(decorated)
    return result + ("\n" if trailing_newline else "")


def format_cli_error(
    category: str,
    message: object,
    presentation: TerminalPresentation,
) -> str:
    text = f"{sanitize_human_value(category)} error: {sanitize_human_value(message)}"
    if not presentation.decorate:
        return text
    marker = "✗" if presentation.unicode_symbols else "[X]"
    return marker + " " + _paint(text, _BOLD, _RED, enabled=presentation.color)


class _Spinner:
    def __init__(
        self,
        message: str,
        *,
        stream: TextIO,
        presentation: TerminalPresentation,
        interval_seconds: float,
    ) -> None:
        self._message = message
        self._stream = stream
        self._presentation = presentation
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._index = 0
        self._frames = (
            _SPINNER_FRAMES
            if presentation.unicode_symbols
            else _ASCII_SPINNER_FRAMES
        )
        self._rendered_width = len(message) + 2
        self._failed = False

    def _write(self, value: str) -> None:
        if self._failed:
            return
        try:
            self._stream.write(value)
            self._stream.flush()
        except (OSError, UnicodeError, ValueError):
            self._failed = True

    def _draw(self) -> None:
        frame = self._frames[self._index % len(self._frames)]
        frame = _paint(frame, _CYAN, enabled=self._presentation.color)
        with self._lock:
            self._write(f"\r{frame} {self._message}")

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._index += 1
            self._draw()

    def start(self) -> None:
        self._draw()
        if self._failed:
            return
        self._thread = Thread(target=self._run, name="dependabot-watcher-spinner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.2, self._interval_seconds * 3))
        with self._lock:
            if self._presentation.decorate:
                self._write("\r\x1b[2K")
            else:
                self._write("\r" + (" " * self._rendered_width) + "\r")


@contextmanager
def live_progress(
    message: str,
    *,
    presentation: TerminalPresentation,
    stream: TextIO,
    interval_seconds: float = 0.08,
) -> Iterator[None]:
    """Show interactive progress without touching machine or redirected output."""

    if presentation.progress is ProgressMode.HIDDEN:
        yield
        return
    if presentation.progress is ProgressMode.STATIC:
        try:
            stream.write(message + "\n")
            stream.flush()
            yield
        finally:
            pass
        return

    spinner = _Spinner(
        message,
        stream=stream,
        presentation=presentation,
        interval_seconds=interval_seconds,
    )
    spinner.start()
    try:
        yield
    finally:
        spinner.stop()
