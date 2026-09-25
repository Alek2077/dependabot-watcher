from __future__ import annotations

from io import StringIO
from typing import TextIO, cast

from dependabot_watcher.terminal import (
    ProgressMode,
    TerminalPresentation,
    decorate_text_report,
    detect_terminal_presentation,
    format_cli_error,
    live_progress,
    sanitize_human_value,
    text_for_stream,
)


class _TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


class _RedirectedBuffer(StringIO):
    def isatty(self) -> bool:
        return False


class _AsciiTtyBuffer:
    encoding = "ascii"

    def __init__(self) -> None:
        self._buffer = StringIO()

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        return self._buffer.write(value)

    def flush(self) -> None:
        self._buffer.flush()

    def getvalue(self) -> str:
        return self._buffer.getvalue()


def test_terminal_detection_keeps_machine_and_redirected_output_plain() -> None:
    for output_format in ("json", "markdown"):
        presentation = detect_terminal_presentation(
            output_format,
            stdout=_TtyBuffer(),
            stderr=_TtyBuffer(),
            environment={},
        )
        assert presentation == TerminalPresentation(
            interactive=False,
            decorate=False,
            color=False,
            unicode_symbols=False,
            progress=ProgressMode.HIDDEN,
        )

    presentation = detect_terminal_presentation(
        "text",
        stdout=_RedirectedBuffer(),
        stderr=_TtyBuffer(),
        environment={},
    )
    assert presentation.progress is ProgressMode.HIDDEN
    assert presentation.decorate is False


def test_interactive_terminal_defaults_to_animation_and_color() -> None:
    presentation = detect_terminal_presentation(
        "text",
        stdout=_TtyBuffer(),
        stderr=_TtyBuffer(),
        environment={"TERM": "xterm-256color"},
    )

    assert presentation == TerminalPresentation(
        interactive=True,
        decorate=True,
        color=True,
        unicode_symbols=True,
        progress=ProgressMode.ANIMATED,
    )


def test_no_color_retains_hierarchy_and_animation_without_ansi_color() -> None:
    for value in ("1", "0", "false", "no"):
        presentation = detect_terminal_presentation(
            "text",
            stdout=_TtyBuffer(),
            stderr=_TtyBuffer(),
            environment={"TERM": "xterm-256color", "NO_COLOR": value},
        )

        assert presentation.decorate is True
        assert presentation.color is False
        assert presentation.progress is ProgressMode.ANIMATED
        rendered = decorate_text_report(
            "Dependabot Watcher inspection\nStatus: complete\n",
            presentation,
        )
        assert "\x1b[" not in rendered
        assert "─────────────────────────────" in rendered
        assert "✓ Status: complete" in rendered


def test_plain_dumb_and_ci_modes_use_static_accessible_progress() -> None:
    cases = (
        (True, {"TERM": "xterm-256color"}),
        (False, {"TERM": "dumb"}),
        (False, {"TERM": "xterm-256color", "CI": "true"}),
    )
    for plain, environment in cases:
        presentation = detect_terminal_presentation(
            "text",
            stdout=_TtyBuffer(),
            stderr=_TtyBuffer(),
            plain=plain,
            environment=environment,
        )
        assert presentation.interactive is True
        assert presentation.decorate is False
        assert presentation.color is False
        assert presentation.progress is ProgressMode.STATIC


def test_report_decoration_is_interactive_only_and_keeps_semantic_text() -> None:
    report = (
        "Dependabot Watcher maintenance plan\n"
        "Status: partial\n"
        "example-org/example-app (base: main @ abc123)\n"
        "  Recommended Order\n"
        "       Blocker: checks are pending\n"
        "Next: resolve partial evidence.\n"
    )
    decorated = decorate_text_report(
        report,
        TerminalPresentation(
            interactive=True,
            decorate=True,
            color=True,
            unicode_symbols=True,
            progress=ProgressMode.ANIMATED,
        ),
    )

    assert decorated.endswith("\n")
    assert "\x1b[" in decorated
    assert "! " in decorated
    assert "example-org/example-app" in decorated
    assert "checks are pending" in decorated
    assert "resolve partial evidence" in decorated

    plain = TerminalPresentation(
        interactive=True,
        decorate=False,
        color=False,
        unicode_symbols=True,
        progress=ProgressMode.STATIC,
    )
    assert decorate_text_report(report, plain) == report
    assert format_cli_error("GitHub", "unavailable", plain) == "GitHub error: unavailable"


def test_progress_modes_keep_static_output_and_clear_animated_output() -> None:
    hidden_stream = StringIO()
    hidden = TerminalPresentation(
        interactive=False,
        decorate=False,
        color=False,
        unicode_symbols=False,
        progress=ProgressMode.HIDDEN,
    )
    with live_progress("Collecting evidence...", presentation=hidden, stream=hidden_stream):
        pass
    assert hidden_stream.getvalue() == ""

    static_stream = StringIO()
    static = TerminalPresentation(
        interactive=True,
        decorate=False,
        color=False,
        unicode_symbols=True,
        progress=ProgressMode.STATIC,
    )
    with live_progress("Collecting evidence...", presentation=static, stream=static_stream):
        assert static_stream.getvalue() == "Collecting evidence...\n"
    assert static_stream.getvalue() == "Collecting evidence...\n"

    animated_stream = _TtyBuffer()
    animated = TerminalPresentation(
        interactive=True,
        decorate=True,
        color=False,
        unicode_symbols=True,
        progress=ProgressMode.ANIMATED,
    )
    with live_progress(
        "Collecting evidence...",
        presentation=animated,
        stream=animated_stream,
        interval_seconds=1.0,
    ):
        assert animated_stream.getvalue().startswith("\r⠋ Collecting evidence...")
    assert animated_stream.getvalue().endswith("\r\x1b[2K")


def test_animated_progress_adds_elapsed_reassurance_for_long_operations() -> None:
    clock_calls = 0

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 0.0 if clock_calls == 1 else 31.0

    animated_stream = _TtyBuffer()
    animated = TerminalPresentation(
        interactive=True,
        decorate=True,
        color=False,
        unicode_symbols=True,
        progress=ProgressMode.ANIMATED,
    )

    with live_progress(
        "Collecting evidence...",
        presentation=animated,
        stream=animated_stream,
        interval_seconds=1.0,
        elapsed_after_seconds=10.0,
        clock=clock,
    ):
        assert "Still working: Collecting evidence (31s elapsed)" in animated_stream.getvalue()

    assert animated_stream.getvalue().endswith("\r\x1b[2K")


def test_ascii_terminal_uses_ascii_symbols_and_spinner() -> None:
    stdout = _AsciiTtyBuffer()
    stderr = _AsciiTtyBuffer()
    presentation = detect_terminal_presentation(
        "text",
        stdout=cast(TextIO, stdout),
        stderr=cast(TextIO, stderr),
        environment={"TERM": "xterm", "NO_COLOR": "1"},
    )
    assert presentation.unicode_symbols is False

    rendered = decorate_text_report(
        "Dependabot Watcher doctor\nStatus: ready\n",
        presentation,
    )
    assert "[OK]" in rendered
    assert "------------------------" in rendered

    with live_progress(
        "Checking...",
        presentation=presentation,
        stream=cast(TextIO, stderr),
        interval_seconds=1.0,
    ):
        pass
    assert stderr.getvalue().startswith("\r-")


def test_ascii_stream_replaces_unsupported_report_characters() -> None:
    stream = _AsciiTtyBuffer()
    report = "Dependabot Watcher inspection\nPR #1: update emoji-package 😀\n"
    safe_report = text_for_stream(report, cast(TextIO, stream))

    assert safe_report == "Dependabot Watcher inspection\nPR #1: update emoji-package ?\n"
    safe_report.encode("ascii", errors="strict")


def test_human_values_strip_terminal_controls_and_flatten_lines() -> None:
    hostile = "\x1b]0;owned\x07Title\r\nnext\x1b[31m"
    cleaned = sanitize_human_value(hostile)
    assert cleaned == "Title next"
    assert "\x1b" not in cleaned
    assert "\n" not in cleaned

    error = format_cli_error(
        "GitHub",
        hostile,
        TerminalPresentation(
            interactive=False,
            decorate=False,
            color=False,
            unicode_symbols=False,
            progress=ProgressMode.HIDDEN,
        ),
    )
    assert error == "GitHub error: Title next"
