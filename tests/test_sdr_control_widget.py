"""SdrControlWidget's IQ-recording playback position slider.

Regression: the slider used to seek only from sliderMoved (fired while
dragging), so on macOS -- where a plain click on the bar jumps the handle
straight to the click point -- clicking moved the handle without seeking
and the next position-poll tick snapped it back.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from sdr.pipeline import SDRPipeline
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
    pipeline: Any = SDRPipeline(device)  # never started
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
