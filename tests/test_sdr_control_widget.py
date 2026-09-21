"""SdrControlWidget's IQ-recording playback position slider.

Regression: the slider used to seek only from sliderMoved (fired while
dragging), so on macOS -- where a plain click on the bar jumps the handle
straight to the click point -- clicking moved the handle without seeking
and the next position-poll tick snapped it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import numpy as np
from PySide6.QtCore import QDate, QDateTime, QPoint, Qt, QTime, QTimeZone
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from ui.sdr_control_widget import SdrControlWidget


class _FakeFileDevice:
    """Minimal SdrFileDevice stand-in that records seeks."""

    sample_rate = 250_000.0
    center_freq = 0.0
    duration_s = 100.0
    at_end = False

    def __init__(self) -> None:
        self.position_s = 0.0
        self.seeks: list[float] = []

    def start_stream(self) -> bool:
        return True

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        return None

    def stop_stream(self) -> None:
        pass

    def seek(self, position_s: float) -> None:
        self.seeks.append(position_s)
        self.position_s = position_s


def _make_widget(qtbot: QtBot) -> tuple[SdrControlWidget, _FakeFileDevice]:
    w = SdrControlWidget()
    qtbot.addWidget(w)
    w.resize(600, 700)
    w.show()
    device = _FakeFileDevice()
    # A mock rather than a real SDRPipeline: that constructs a Demodulator,
    # which needs scipy, an optional dependency CI does not install.
    pipeline: Any = MagicMock()
    pipeline._device = device
    w.set_pipeline(pipeline, is_replay=True)
    w._update_playback_position()  # sets the slider range from the device
    device.seeks.clear()
    return w, device


def test_clicking_the_bar_seeks(qtbot: QtBot) -> None:
    w, device = _make_widget(qtbot)
    slider = w._playback_slider
    assert slider.maximum() == 100

    click_at = QPoint(int(slider.width() * 0.7), slider.rect().center().y())
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, click_at)

    assert device.seeks, "a click on the bar must seek, not just move the handle"
    assert device.seeks[-1] == float(slider.value())
    assert slider.value() > 0

    # The position poll must keep the handle where the click put it.
    w._update_playback_position()
    assert slider.value() == int(device.position_s)


def test_dragging_the_handle_seeks(qtbot: QtBot) -> None:
    w, device = _make_widget(qtbot)
    slider = w._playback_slider

    y = slider.rect().center().y()
    QTest.mousePress(
        slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(8, y)
    )
    for x in range(20, int(slider.width() * 0.6), 20):
        QTest.mouseMove(slider, QPoint(x, y))
    QTest.mouseRelease(
        slider,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(int(slider.width() * 0.6), y),
    )

    assert device.seeks
    assert device.seeks[-1] == float(slider.value())
    assert slider.value() > 30


def test_position_poll_does_not_seek(qtbot: QtBot) -> None:
    w, device = _make_widget(qtbot)
    device.position_s = 42.4

    w._update_playback_position()

    assert w._playback_slider.value() == 42
    assert device.seeks == []


def test_waterfall_opened_during_playback_knows_it_is_a_replay(qtbot: QtBot) -> None:
    """Opened after playback started, the dialog must still be told it is one
    (it shows file positions, not clock times, and a relative axis)."""
    w, _ = _make_widget(qtbot)
    w._on_show_waterfall()
    dialog = w._waterfall_dialog
    assert dialog is not None
    qtbot.addWidget(dialog)
    assert dialog._is_replay is True


def test_time_zone_setting_reaches_the_waterfall_dialog(qtbot: QtBot) -> None:
    w, _ = _make_widget(qtbot)
    w.set_use_utc(False)  # before the dialog exists: applied when it is created
    w._on_show_waterfall()
    dialog = w._waterfall_dialog
    assert dialog is not None
    qtbot.addWidget(dialog)
    assert dialog._use_utc is False
    w.set_use_utc(True)  # afterwards: forwarded straight away
    assert dialog._use_utc is True


class _TimedFileDevice(_FakeFileDevice):
    """A file device that also knows the recording's UTC start time."""

    def __init__(self, start: datetime | None) -> None:
        super().__init__()
        self.start_time_utc = start
        self.start_time_confirmed = start is not None

    def set_start_time_utc(self, when: datetime | None, confirmed: bool = True) -> None:
        self.start_time_utc = when
        self.start_time_confirmed = confirmed


def _make_timed_widget(
    qtbot: QtBot, start: datetime | None, is_replay: bool = True
) -> tuple[SdrControlWidget, _TimedFileDevice]:
    w = SdrControlWidget()
    qtbot.addWidget(w)
    device = _TimedFileDevice(start)
    pipeline: Any = MagicMock()
    pipeline._device = device
    w.set_pipeline(pipeline, is_replay=is_replay)
    return w, device


def _shown_start(w: SdrControlWidget) -> str:
    return str(w._playback_start_edit.dateTime().toString("yyyy-MM-dd HH:mm:ss"))


class TestRecordingStartTimeForm:
    """The "Start (UTC)" field right of Offset: the recording's start time,
    read from the file name when possible, otherwise 00:00 and user-editable."""

    def test_shows_the_time_read_from_the_file_name(self, qtbot: QtBot) -> None:
        w, _device = _make_timed_widget(qtbot, datetime(2026, 9, 20, 6, 55, 51, tzinfo=UTC))
        assert _shown_start(w) == "2026-09-20 06:55:51"
        assert w._playback_start_edit.isEnabled()

    def test_unreadable_name_shows_midnight_and_hands_it_to_the_device(self, qtbot: QtBot) -> None:
        w, device = _make_timed_widget(qtbot, None)
        shown = _shown_start(w)
        assert shown.endswith(" 00:00:00")
        assert device.start_time_utc is not None
        assert device.start_time_utc.hour == device.start_time_utc.minute == 0
        assert device.start_time_utc.tzinfo is UTC
        assert not device.start_time_confirmed  # a placeholder, not a real time

    def test_editing_the_field_updates_the_device(self, qtbot: QtBot) -> None:
        w, device = _make_timed_widget(qtbot, None)
        w._playback_start_edit.setDateTime(
            QDateTime(QDate(2026, 9, 20), QTime(7, 7, 34), QTimeZone.utc())
        )
        assert device.start_time_utc == datetime(2026, 9, 20, 7, 7, 34, tzinfo=UTC)
        assert device.start_time_confirmed  # typed by the user

    def test_disabled_without_a_replay(self, qtbot: QtBot) -> None:
        w, _device = _make_timed_widget(qtbot, None, is_replay=False)
        assert not w._playback_start_edit.isEnabled()
