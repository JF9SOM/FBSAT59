"""Tests for the Python-thread UI hang watchdog."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core import ui_hang_watchdog as wd


def test_stall_writes_all_thread_stacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "_STALL_SECONDS", 0.2)
    monkeypatch.setattr(wd, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr("platformdirs.user_log_dir", lambda *a, **k: str(tmp_path))
    wd.start()
    try:
        time.sleep(0.8)  # no kick() -> stall
    finally:
        wd.stop()
    text = (tmp_path / "ui_hang.log").read_text()
    assert text.count("Timeout (") == 1  # once per stall, not repeatedly
    assert "most recent call first" in text
    assert "test_stall_writes_all_thread_stacks" in text


def test_kick_prevents_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "_STALL_SECONDS", 0.3)
    monkeypatch.setattr(wd, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr("platformdirs.user_log_dir", lambda *a, **k: str(tmp_path))
    wd.start()
    try:
        for _ in range(12):
            wd.kick()
            time.sleep(0.05)
    finally:
        wd.stop()
    assert "Timeout" not in (tmp_path / "ui_hang.log").read_text()
