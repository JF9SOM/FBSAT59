"""SdrControlWidget's IQ-recording playback position slider.

Regression: the slider used to seek only from sliderMoved (fired while
dragging), so on macOS -- where a plain click on the bar jumps the handle
straight to the click point -- clicking moved the handle without seeking
and the next position-poll tick snapped it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
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
        self.is_streaming = True  # the pipeline starts a loaded recording playing
        self.starts = 0
        self.stops = 0

    def start_stream(self) -> bool:
        self.is_streaming = True
        self.starts += 1
        return True

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        return None

    def stop_stream(self) -> None:
        self.is_streaming = False
        self.stops += 1

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


class TestOpenPlayStop:
    """ "📂 Open…" chooses a file and plays it; after "■ Stop", "▶ Play" plays the same
    file again without asking for it. The recordings-folder button is gone."""

    def _no_dialog(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        opened: list[str] = []

        def refuse(*args: Any, **kwargs: Any) -> tuple[str, str]:
            opened.append("dialog")
            return "", ""

        monkeypatch.setattr("ui.sdr_control_widget.QFileDialog.getOpenFileName", refuse)
        return opened

    def test_open_asks_for_a_file_and_requests_playback(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = SdrControlWidget()
        qtbot.addWidget(w)
        monkeypatch.setattr(
            "ui.sdr_control_widget.QFileDialog.getOpenFileName",
            lambda *a, **k: ("/rec/0_unknown_20260920T065551Z.iq.wav", ""),
        )
        with qtbot.waitSignal(w.play_recording_requested) as blocker:
            w._open_btn.click()
        assert blocker.args == ["/rec/0_unknown_20260920T065551Z.iq.wav"]

    def test_cancelling_the_file_dialog_requests_nothing(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = SdrControlWidget()
        qtbot.addWidget(w)
        self._no_dialog(monkeypatch)
        requested: list[str] = []
        w.play_recording_requested.connect(requested.append)
        w._open_btn.click()
        assert requested == []

    def test_open_is_usable_without_any_recording_or_sdr(self, qtbot: QtBot) -> None:
        w = SdrControlWidget()
        qtbot.addWidget(w)
        assert w._open_btn.isEnabled()
        assert not w._play_btn.isEnabled()
        assert not w._stop_play_btn.isEnabled()

    def test_a_playing_recording_can_be_stopped_but_not_played(self, qtbot: QtBot) -> None:
        w, device = _make_widget(qtbot)  # loaded, and playing
        assert device.is_streaming
        assert w._stop_play_btn.isEnabled()
        assert not w._play_btn.isEnabled()
        assert w._open_btn.isEnabled()

    def test_stop_pauses_and_swaps_the_buttons(self, qtbot: QtBot) -> None:
        w, device = _make_widget(qtbot)

        w._stop_play_btn.click()

        assert (device.stops, device.is_streaming) == (1, False)
        assert w._play_btn.isEnabled()
        assert not w._stop_play_btn.isEnabled()

    def test_play_after_stop_resumes_without_a_file_dialog(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, device = _make_widget(qtbot)
        opened = self._no_dialog(monkeypatch)
        requested: list[str] = []
        w.play_recording_requested.connect(requested.append)
        device.position_s = 42.0
        w._stop_play_btn.click()

        w._play_btn.click()

        assert opened == [] and requested == []  # no file manager, nothing reloaded
        assert device.is_streaming
        assert device.starts == 1
        assert device.seeks == []  # continues where it stopped
        assert w._stop_play_btn.isEnabled()
        assert not w._play_btn.isEnabled()

    def test_play_after_the_end_starts_over_from_the_beginning(self, qtbot: QtBot) -> None:
        w, device = _make_widget(qtbot)
        device.at_end = True
        w._update_playback_position()  # the timer pauses a recording that reached its end
        assert not device.is_streaming
        assert w._play_btn.isEnabled()

        w._play_btn.click()

        assert device.seeks == [0.0]
        assert device.is_streaming

    def test_the_recordings_folder_button_is_gone(self, qtbot: QtBot) -> None:
        w = SdrControlWidget()
        qtbot.addWidget(w)
        assert not hasattr(w, "_open_folder_btn")
        assert not hasattr(w, "_open_iq_folder")

    def test_a_live_pipeline_has_nothing_to_play_or_stop(self, qtbot: QtBot) -> None:
        w = SdrControlWidget()
        qtbot.addWidget(w)
        pipeline: Any = MagicMock()
        pipeline._device = _FakeFileDevice()
        w.set_pipeline(pipeline, is_replay=False)
        assert not w._play_btn.isEnabled()
        assert not w._stop_play_btn.isEnabled()


def _fake_pipeline() -> MagicMock:
    """Pipeline stand-in: a MagicMock whose recorder.start() returns a path."""
    pipe = MagicMock()
    pipe._device = None
    pipe.recorder.start.return_value = Path("x.iq.wav")
    return pipe


def test_recording_is_stopped_when_the_pipeline_is_replaced(qtbot: QtBot) -> None:
    """Rig Settings OK swaps the SDR pipeline mid-pass: the old recorder must be closed."""
    w = SdrControlWidget()
    qtbot.addWidget(w)
    old, new = _fake_pipeline(), _fake_pipeline()
    w.set_pipeline(old)
    w.start_iq_recording_for_autotrack()
    old.recorder.start.assert_called_once()
    w.set_pipeline(new)
    old.recorder.stop.assert_called_once()
    new.recorder.stop.assert_not_called()


def test_recording_is_stopped_when_the_pipeline_is_detached(qtbot: QtBot) -> None:
    w = SdrControlWidget()
    qtbot.addWidget(w)
    pipe = _fake_pipeline()
    w.set_pipeline(pipe)
    w.start_iq_recording_for_autotrack()
    w.set_pipeline(None)
    pipe.recorder.stop.assert_called_once()
