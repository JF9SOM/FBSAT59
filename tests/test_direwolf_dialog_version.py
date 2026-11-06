"""The Direwolf dialog logs why a version could not be read."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ui import direwolf_dialog


def test_failure_to_run_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise UnicodeDecodeError("cp932", b"\x80", 0, 1, "illegal multibyte sequence")

    monkeypatch.setattr(subprocess, "run", boom)
    with caplog.at_level(logging.WARNING, logger=direwolf_dialog.logger.name):
        assert direwolf_dialog._get_direwolf_version(Path("direwolf.exe")) == "Unknown version"
    assert "UnicodeDecodeError" in caplog.text and "direwolf.exe" in caplog.text


def test_missing_version_line_is_logged_with_the_output(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    done = subprocess.CompletedProcess(["x"], 3, stdout="usage: ...\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: done)
    # No line mentions "version" or "direwolf", so the version stays unknown.
    with caplog.at_level(logging.WARNING, logger=direwolf_dialog.logger.name):
        assert direwolf_dialog._get_direwolf_version(Path("x")) == "Unknown version"
    assert "exit code 3" in caplog.text and "usage" in caplog.text


def test_version_line_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess(["x"], 0, stdout="Dire Wolf version 1.8\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: done)
    assert direwolf_dialog._get_direwolf_version(Path("x")) == "Dire Wolf version 1.8"
