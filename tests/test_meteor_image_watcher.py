"""Tests for comms.meteor.image_watcher and the METEOR tab's use of it.

SatDump creates a PNG before it has finished writing it. A half-written file cannot be
read (libpng "Read Error"), and the watcher used to mark every file as seen on the first
poll, so such an image was lost for good (found 2026-10-23 on a Windows IQ decode).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot

from comms.meteor.image_watcher import _MAX_RETRIES, ImageWatcher
from ui.meteor_tab import MeteorTab


def _png_bytes(tmp_path: Path) -> bytes:
    img = QImage(400, 300, QImage.Format.Format_RGB32)
    img.fill(QColor("#3366cc"))
    probe = tmp_path / "_probe.png"
    img.save(str(probe))
    data = probe.read_bytes()
    probe.unlink()
    return data


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ui.meteor_tab._load_meteor_settings", lambda: {})
    monkeypatch.setattr("ui.meteor_tab._load_sdr_settings", lambda: {})
    monkeypatch.setattr("ui.meteor_tab._save_meteor_settings", lambda data: None)


class TestImageWatcherRetry:
    def test_a_file_is_announced_once_by_default(self, tmp_path: Path, qtbot: QtBot) -> None:
        w = ImageWatcher(tmp_path)
        w.start()
        seen: list[Path] = []
        w.new_image.connect(seen.append)
        (tmp_path / "a.png").write_bytes(b"x")
        w.poll_now()
        w.poll_now()
        assert seen == [tmp_path / "a.png"]

    def test_retry_offers_the_file_again(self, tmp_path: Path, qtbot: QtBot) -> None:
        w = ImageWatcher(tmp_path)
        w.start()
        seen: list[Path] = []
        w.new_image.connect(seen.append)
        (tmp_path / "a.png").write_bytes(b"x")
        w.poll_now()
        w.retry(tmp_path / "a.png")
        w.poll_now()
        assert seen == [tmp_path / "a.png"] * 2

    def test_a_file_that_never_becomes_readable_is_given_up_on(
        self, tmp_path: Path, qtbot: QtBot
    ) -> None:
        w = ImageWatcher(tmp_path)
        w.start()
        seen: list[Path] = []
        w.new_image.connect(seen.append)
        (tmp_path / "bad.png").write_bytes(b"x")
        for _ in range(_MAX_RETRIES + 10):
            w.poll_now()
            w.retry(tmp_path / "bad.png")
        w.poll_now()
        assert len(seen) == _MAX_RETRIES + 1


class TestMeteorTabKeepsHalfWrittenImages:
    def test_image_written_in_two_steps_ends_up_in_the_history(
        self, tmp_path: Path, qtbot: QtBot
    ) -> None:
        data = _png_bytes(tmp_path)
        tab = MeteorTab()
        qtbot.addWidget(tab)
        watcher = ImageWatcher(tmp_path)
        tab._watcher = watcher
        watcher.new_image.connect(tab._on_new_image)
        watcher.start()

        target = tmp_path / "msu_mr_rgb_MCIR.png"
        target.write_bytes(data[: len(data) // 2])  # SatDump has written half of it
        watcher.poll_now()
        assert tab._history_list.count() == 0

        target.write_bytes(data)  # SatDump finishes the file
        watcher.poll_now()
        assert tab._history_list.count() == 1
