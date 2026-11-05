"""Tests for the line-by-line console process wrapper (comms.aprs.line_process)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from comms.aprs.line_process import PipeLineProcess, split_console_text


def test_split_strips_ansi_and_keeps_partial_line() -> None:
    lines, rest = split_console_text(
        "", "\x1b[?25l\x1b[32mPTT 0 = 1\x1b[0m\r\nReady\x1b]0;title\x07 to"
    )
    assert lines == ["PTT 0 = 1"]
    assert rest == "Ready to"


def test_split_joins_chunks_and_drops_blank_lines() -> None:
    lines, rest = split_console_text("PTT 0", " = 0\r\n\r\n")
    assert lines == ["PTT 0 = 0"]
    assert rest == ""


def test_pipe_process_delivers_lines_and_end_marker(tmp_path: Path) -> None:
    proc = PipeLineProcess([sys.executable, "-u", "-c", "print('one'); print('two')"], tmp_path)
    got: list[str | None] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        item = proc.lines.get(timeout=5)
        got.append(item)
        if item is None:
            break
    assert got == ["one", "two", None]
    assert proc.wait(5) == 0
