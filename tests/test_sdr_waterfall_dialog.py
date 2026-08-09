"""Unit tests for ui/sdr_waterfall_dialog.py.

Covers the pure helper functions (color_map, nice_axis_step) and the
dialog's pipeline-following / history lifecycle using a fake pipeline
object (a plain QObject with the same two signals SDRPipeline exposes —
no real SoapySDR device needed).

Uses pytest-qt's ``qtbot`` fixture + ``qtbot.addWidget()`` per this
project's convention for any test constructing a QWidget/QDialog (see
CLAUDE.md's note on ft4/rig_dialog_sdr tests — manually-managed
QApplication + bare .close() has caused interpreter segfaults at exit
for other widgets in this codebase).
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QObject, Signal
from pytestqt.qtbot import QtBot

from ui.sdr_waterfall_dialog import SdrWaterfallDialog, color_map, nice_axis_step


class _FakePipeline(QObject):
    """Minimal stand-in for SDRPipeline exposing only the two signals
    SdrWaterfallDialog subscribes to."""

    spectrum_ready: Signal = Signal(list)
    center_freq_changed: Signal = Signal(float)


def _emit_spectrum(pipeline: _FakePipeline, center_hz: float, n: int = 256) -> None:
    freqs = center_hz + (np.arange(n) - n / 2) * 100.0
    powers = -80.0 + np.linspace(-5.0, 5.0, n)
    pipeline.spectrum_ready.emit(list(zip(freqs.tolist(), powers.tolist(), strict=True)))


def test_color_map_endpoints_and_midpoint() -> None:
    norm = np.array([0.0, 0.5, 1.0], dtype=np.float32)
    rgb = color_map(norm)
    assert rgb.shape == (3, 3)
    assert tuple(rgb[0]) == (0, 0, 40)  # first palette entry
    assert tuple(rgb[-1]) == (255, 30, 30)  # last palette entry


def test_color_map_clamps_out_of_range() -> None:
    norm = np.array([-1.0, 2.0], dtype=np.float32)
    rgb = color_map(norm)
    assert tuple(rgb[0]) == (0, 0, 40)
    assert tuple(rgb[1]) == (255, 30, 30)


def test_nice_axis_step_zero_span_is_safe() -> None:
    assert nice_axis_step(0.0) == 1.0
    assert nice_axis_step(-5.0) == 1.0


def test_nice_axis_step_yields_round_numbers() -> None:
    span = 2_400_000.0
    step = nice_axis_step(span)
    leading_digit = step / (10.0 ** math.floor(math.log10(step)))
    assert leading_digit in (1.0, 2.0, 5.0)
    # roughly 4-6 ticks across the span
    assert 3 <= span / step <= 8


def test_dialog_ignores_spectrum_while_hidden(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    # Not shown -> isVisible() is False -> _on_spectrum() should no-op.
    _emit_spectrum(pipeline, 435.6e6)
    assert len(dlg._history) == 0


def test_dialog_accumulates_history_while_visible(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    for _ in range(5):
        _emit_spectrum(pipeline, 435.6e6)
    assert len(dlg._history) == 5
    pix = dlg._image_label.pixmap()
    assert not pix.isNull()


def test_hide_clears_history(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    _emit_spectrum(pipeline, 435.6e6)
    assert len(dlg._history) == 1
    dlg.hide()
    assert len(dlg._history) == 0


def test_set_pipeline_disconnects_previous_pipeline(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    old = _FakePipeline()
    dlg.set_pipeline(old)
    _emit_spectrum(old, 435.6e6)
    assert len(dlg._history) == 1

    new = _FakePipeline()
    dlg.set_pipeline(new)
    assert len(dlg._history) == 0  # cleared on re-attach

    # Old pipeline's signal must no longer reach the dialog.
    _emit_spectrum(old, 435.6e6)
    assert len(dlg._history) == 0

    _emit_spectrum(new, 435.6e6)
    assert len(dlg._history) == 1


def test_set_pipeline_none_shows_placeholder(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    _emit_spectrum(pipeline, 435.6e6)
    assert not dlg._image_label.pixmap().isNull()

    dlg.set_pipeline(None)
    assert dlg._image_label.pixmap().isNull()
    assert len(dlg._history) == 0


def test_manual_range_disables_only_when_auto_off(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    assert dlg._auto_chk.isChecked() is True
    assert dlg._low_spin.isEnabled() is False
    assert dlg._high_spin.isEnabled() is False

    dlg._auto_chk.setChecked(False)
    assert dlg._low_spin.isEnabled() is True
    assert dlg._high_spin.isEnabled() is True

    dlg._auto_chk.setChecked(True)
    assert dlg._low_spin.isEnabled() is False
    assert dlg._high_spin.isEnabled() is False
