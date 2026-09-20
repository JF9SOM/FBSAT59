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
from PySide6.QtCore import QObject, QRect, QSize, Signal
from pytestqt.qtbot import QtBot

from sdr.burst_detector import BurstRow, RowKind
from ui.sdr_waterfall_dialog import (
    SdrWaterfallDialog,
    color_map,
    nice_axis_step,
    top_right_position,
)


class _FakePipeline(QObject):
    """Minimal stand-in for SDRPipeline exposing only what
    SdrWaterfallDialog subscribes to / calls."""

    spectrum_ready: Signal = Signal(list)
    center_freq_changed: Signal = Signal(float)
    burst_row_ready: Signal = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.burst_calls: list[bool] = []

    def set_burst_detection(self, enabled: bool) -> None:
        self.burst_calls.append(enabled)


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


def test_top_right_position_lands_in_the_right_half_of_a_realistic_screen() -> None:
    avail = QRect(0, 0, 1920, 1080)
    size = QSize(847, 498)
    pt = top_right_position(avail, size)
    # QRect.right() is x + width - 1 (Qt's classic off-by-one convention).
    assert pt.x() == avail.right() - size.width() - 24
    assert pt.y() == 24
    assert pt.x() > avail.width() / 2


def test_top_right_position_clamps_when_window_is_wider_than_the_screen() -> None:
    avail = QRect(0, 0, 800, 800)
    size = QSize(847, 498)  # wider than the available screen
    pt = top_right_position(avail, size)
    assert pt.x() == 0
    assert pt.y() == 24


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


def test_partial_history_leaves_unfilled_rows_as_background(qtbot: QtBot) -> None:
    """A fresh waterfall (few rows so far) must not stretch that sparse
    history to fill the whole plot area — only the rows actually received
    should show real data; the rest must stay background. Regression test
    for the "waterfall looks compressed / first signal appears at the
    bottom" bug caused by scaling a growing row count into a fixed
    display height."""
    from ui.sdr_waterfall_dialog import (
        _BACKGROUND_RGB,
        _MARGIN_AXIS,
        _MARGIN_LEFT,
        _MARGIN_TOP,
        _SPECTRUM_HEIGHT,
    )

    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    _emit_spectrum(pipeline, 435.6e6)  # exactly one row of history so far
    assert len(dlg._history) == 1

    wf_top = _MARGIN_TOP + _SPECTRUM_HEIGHT + _MARGIN_AXIS
    img = dlg._image_label.pixmap().toImage()
    top_row_color = img.pixelColor(_MARGIN_LEFT + 5, wf_top)
    next_row_color = img.pixelColor(_MARGIN_LEFT + 5, wf_top + 1)
    assert (top_row_color.red(), top_row_color.green(), top_row_color.blue()) != _BACKGROUND_RGB
    assert (next_row_color.red(), next_row_color.green(), next_row_color.blue()) == _BACKGROUND_RGB


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


def test_position_top_right_is_queued_once_on_first_show_only(qtbot: QtBot) -> None:
    """Regression test for the popup opening centred instead of top-right:
    positioning must actually be applied (not just computed) after the
    window has been mapped, and only on the very first show() — a second
    show()/raise_() (e.g. reopening after the user dragged it elsewhere)
    must not re-snap it back to the corner."""
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    calls = []
    dlg._position_top_right = lambda: calls.append(1)

    assert dlg._positioned_once is False
    dlg.show()
    qtbot.wait(20)  # let the QTimer.singleShot(0, ...) fire
    assert dlg._positioned_once is True
    assert len(calls) == 1

    dlg.hide()
    dlg.show()
    qtbot.wait(20)
    assert len(calls) == 1  # not called again


# ---------------------------------------------------------------------------
# IQ recording playback: relative (not absolute) frequency axis
# ---------------------------------------------------------------------------


def test_format_axis_label_absolute_by_default(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    assert dlg._format_axis_label(437_505_000.0) == "437.505"


def test_format_axis_label_relative_when_replaying(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.set_pipeline(_FakePipeline(), is_replay=True)
    assert dlg._format_axis_label(0.0) == "0 Hz"
    assert dlg._format_axis_label(500.0) == "+500 Hz"
    assert dlg._format_axis_label(-500.0) == "-500 Hz"
    assert dlg._format_axis_label(12_500.0) == "+12.50 kHz"
    assert dlg._format_axis_label(-30_000.0) == "-30.00 kHz"


def test_set_pipeline_is_replay_resets_to_false_on_detach(qtbot: QtBot) -> None:
    """Detaching (set_pipeline(None)) must not leave a stale relative-axis mode
    the next live SDR connection would silently inherit."""
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.set_pipeline(_FakePipeline(), is_replay=True)
    assert dlg._is_replay is True
    dlg.set_pipeline(None)
    assert dlg._is_replay is False


# ---------------------------------------------------------------------------
# Burst mode
# ---------------------------------------------------------------------------

_N_BINS = 1024


def _burst_row(
    kind: RowKind = RowKind.NONE,
    bins: tuple[int, int] | None = None,
    snr_db: float | None = None,
    event_id: int | None = None,
    count: int = 0,
    warming_up: bool = False,
) -> BurstRow:
    return BurstRow(
        freqs_hz=435.6e6 + (np.arange(_N_BINS) - _N_BINS / 2) * 244.0,
        power_dbfs=np.full(_N_BINS, -80.0, dtype=np.float32),
        kind=kind,
        burst_bins=bins,
        snr_db=snr_db,
        event_id=event_id,
        event_count=count,
        warming_up=warming_up,
    )


def _shown_burst_dialog(qtbot: QtBot) -> tuple[SdrWaterfallDialog, _FakePipeline]:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    dlg._burst_chk.setChecked(True)
    return dlg, pipeline


def _pixel(dlg: SdrWaterfallDialog, x: int, y: int) -> tuple[int, int, int]:
    c = dlg._image_label.pixmap().toImage().pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def test_burst_mode_is_off_by_default_and_costs_nothing(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    assert dlg._burst_chk.isChecked() is False
    assert pipeline.burst_calls == []  # detector never switched on
    assert dlg._burst_label.text() == ""
    _emit_spectrum(pipeline, 435.6e6)
    assert len(dlg._history) == 1  # ordinary single-FFT rows as before
    pipeline.burst_row_ready.emit(_burst_row())
    assert len(dlg._history) == 1  # burst rows ignored while the box is off


def test_burst_checkbox_switches_detection_on_the_pipeline(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    pipeline = _FakePipeline()
    dlg.set_pipeline(pipeline)
    dlg._burst_chk.setChecked(True)
    dlg._burst_chk.setChecked(False)
    assert pipeline.burst_calls == [True, False]


def test_burst_setting_follows_the_dialog_to_a_new_pipeline(qtbot: QtBot) -> None:
    dlg = SdrWaterfallDialog()
    qtbot.addWidget(dlg)
    old = _FakePipeline()
    dlg.set_pipeline(old)
    dlg._burst_chk.setChecked(True)

    new = _FakePipeline()
    dlg.set_pipeline(new)
    assert old.burst_calls == [True, False]  # released from the old one
    assert new.burst_calls == [True]  # and switched on for the new one


def test_burst_mode_takes_rows_from_burst_signal_only(qtbot: QtBot) -> None:
    dlg, pipeline = _shown_burst_dialog(qtbot)
    _emit_spectrum(pipeline, 435.6e6)
    assert len(dlg._history) == 0  # single-FFT rows are ignored in burst mode
    pipeline.burst_row_ready.emit(_burst_row())
    assert len(dlg._history) == 1
    assert len(dlg._row_meta) == 1
    assert not dlg._image_label.pixmap().isNull()


def test_burst_counter_label_and_calibration_note(qtbot: QtBot) -> None:
    dlg, pipeline = _shown_burst_dialog(qtbot)
    pipeline.burst_row_ready.emit(_burst_row(count=0, warming_up=True))
    assert "0" in dlg._burst_label.text()
    assert "calibrating" in dlg._burst_label.text()
    pipeline.burst_row_ready.emit(_burst_row(count=3))
    assert dlg._burst_label.text().endswith("3")
    assert "calibrating" not in dlg._burst_label.text()


def test_burst_counter_keeps_counting_while_the_dialog_is_hidden(qtbot: QtBot) -> None:
    dlg, pipeline = _shown_burst_dialog(qtbot)
    dlg.hide()
    pipeline.burst_row_ready.emit(_burst_row(count=4))
    assert dlg._burst_label.text().endswith("4")
    assert len(dlg._history) == 0  # no picture is built while hidden


def test_burst_counter_is_cleared_by_new_pipeline_and_by_toggling(qtbot: QtBot) -> None:
    dlg, pipeline = _shown_burst_dialog(qtbot)
    pipeline.burst_row_ready.emit(_burst_row(count=5))
    assert dlg._burst_label.text().endswith("5")

    dlg.set_pipeline(_FakePipeline())
    assert dlg._burst_label.text().endswith("0")

    dlg._burst_chk.setChecked(False)
    assert dlg._burst_label.text() == ""
    dlg._burst_chk.setChecked(True)
    assert dlg._burst_label.text().endswith("0")


def test_burst_row_is_painted_red_with_a_white_outline(qtbot: QtBot) -> None:
    from ui.sdr_waterfall_dialog import _MARGIN_AXIS, _MARGIN_LEFT, _MARGIN_TOP, _SPECTRUM_HEIGHT

    dlg, pipeline = _shown_burst_dialog(qtbot)
    for _ in range(3):
        pipeline.burst_row_ready.emit(_burst_row())
    pipeline.burst_row_ready.emit(
        _burst_row(RowKind.BURST, bins=(500, 540), snr_db=6.5, event_id=1)
    )
    pipeline.burst_row_ready.emit(_burst_row())  # let the burst scroll down one row

    # (The frame line is drawn over the very top pixel row, so look one lower.)
    wf_top = _MARGIN_TOP + _SPECTRUM_HEIGHT + _MARGIN_AXIS
    y_burst = wf_top + 1
    x_burst = _MARGIN_LEFT + int(520 * 760 / _N_BINS)
    assert _pixel(dlg, x_burst, y_burst) == (255, 0, 0)
    assert _pixel(dlg, x_burst, y_burst + 1) == (255, 255, 255)  # outline below it
    x_away = _MARGIN_LEFT + int(100 * 760 / _N_BINS)
    assert _pixel(dlg, x_away, y_burst) != (255, 0, 0)  # rest of the row untouched


def test_burst_snr_label_keeps_the_best_row_of_an_event(qtbot: QtBot) -> None:
    dlg, pipeline = _shown_burst_dialog(qtbot)
    for snr in (2.0, 6.5, 4.0):
        pipeline.burst_row_ready.emit(
            _burst_row(RowKind.BURST, bins=(500, 540), snr_db=snr, event_id=1)
        )
    assert dlg._event_snr == {1: 6.5}


def test_impulse_rows_are_tinted_not_red(qtbot: QtBot) -> None:
    from ui.sdr_waterfall_dialog import _MARGIN_AXIS, _MARGIN_LEFT, _MARGIN_TOP, _SPECTRUM_HEIGHT

    dlg, pipeline = _shown_burst_dialog(qtbot)
    pipeline.burst_row_ready.emit(_burst_row())
    pipeline.burst_row_ready.emit(_burst_row(RowKind.IMPULSE))
    pipeline.burst_row_ready.emit(_burst_row())

    wf_top = _MARGIN_TOP + _SPECTRUM_HEIGHT + _MARGIN_AXIS
    x = _MARGIN_LEFT + 200
    tinted = _pixel(dlg, x, wf_top + 1)  # the impulse, one row below the newest
    plain = _pixel(dlg, x, wf_top + 2)
    assert tinted != plain
    assert tinted != (255, 0, 0)


def test_burst_history_and_events_are_dropped_on_hide(qtbot: QtBot) -> None:
    dlg, pipeline = _shown_burst_dialog(qtbot)
    pipeline.burst_row_ready.emit(_burst_row(RowKind.BURST, (500, 540), 5.0, 1))
    assert len(dlg._row_meta) == 1
    dlg.hide()
    assert len(dlg._row_meta) == 0
    assert dlg._event_snr == {}
