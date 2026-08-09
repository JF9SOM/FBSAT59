"""SdrWaterfallDialog — a scrolling spectrum + waterfall popup for the SDR
Control tab, in the style of standalone SDR software (GQRX, SDR#): a live
spectrum trace on top and a scrolling colour waterfall below it, sharing
one frequency axis.

Fed directly from SDRPipeline.spectrum_ready (~10 fps, already-computed
FFT power in dBFS — see sdr/pipeline.py._compute_fft()) rather than
recomputing an FFT of its own. Only accumulates/redraws while visible;
history is cleared on hide so reopening starts fresh rather than
splicing together unrelated stretches of spectrum (same convention as
ft4_waterfall_dialog.py).

Each row is drawn from its own bin array as-is (bin index -> pixel
column); only the latest row's frequency range is used for axis labels
and the centre-frequency marker. Small frequency drift between rows
(e.g. from Doppler-driven retunes) is not re-aligned — this matches how
ordinary SDR waterfalls behave when the receiver is nudged slightly
during a pass, and avoids clearing the whole history on every retune.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QHideEvent, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from i18n import _

# How many spectrum rows to keep scrolling through before the oldest falls
# off the bottom. SDRPipeline emits spectrum_ready at ~10fps, so 300 rows
# is roughly 30s of history.
_HISTORY_ROWS = 300

# Shared frequency-axis width for both the spectrum trace and the
# waterfall image below it.
_PLOT_WIDTH = 760
_SPECTRUM_HEIGHT = 130
_WATERFALL_HEIGHT = 280

_MARGIN_LEFT = 55
_MARGIN_RIGHT = 10
_MARGIN_TOP = 6
_MARGIN_AXIS = 22  # frequency-axis tick strip between spectrum and waterfall
_MARGIN_BOTTOM = 6

_CANVAS_WIDTH = _MARGIN_LEFT + _PLOT_WIDTH + _MARGIN_RIGHT
_CANVAS_HEIGHT = _MARGIN_TOP + _SPECTRUM_HEIGHT + _MARGIN_AXIS + _WATERFALL_HEIGHT + _MARGIN_BOTTOM

_DEFAULT_LOW_DB = -90.0
_DEFAULT_HIGH_DB = 0.0

# Same palette family as ft4_waterfall_dialog.py for visual consistency
# across the app's waterfall popups.
_PALETTE = [
    (0, 0, 40),
    (0, 0, 120),
    (0, 100, 200),
    (0, 200, 200),
    (0, 220, 80),
    (230, 230, 0),
    (255, 140, 0),
    (255, 30, 30),
]


def color_map(norm: NDArray[np.float32]) -> NDArray[np.uint8]:
    """Map a 0..1 float array to an RGB uint8 array via a fixed palette."""
    n_colors = len(_PALETTE)
    idx_f = np.clip(norm, 0.0, 1.0) * (n_colors - 1)
    idx0 = np.floor(idx_f).astype(np.int32)
    idx1 = np.minimum(idx0 + 1, n_colors - 1)
    frac = (idx_f - idx0)[..., None]
    palette = np.array(_PALETTE, dtype=np.float32)
    rgb = palette[idx0] * (1 - frac) + palette[idx1] * frac
    return rgb.astype(np.uint8)


def nice_axis_step(span_hz: float) -> float:
    """Choose a round-number tick step yielding roughly 4-6 ticks across span_hz."""
    if span_hz <= 0:
        return 1.0
    raw = span_hz / 5.0
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for mult in (1, 2, 5, 10):
        step = magnitude * mult
        if step >= raw:
            return step
    return magnitude * 10.0


class SdrWaterfallDialog(QDialog):
    """Non-modal popup: live spectrum trace + scrolling waterfall.

    Call set_pipeline() whenever the owning SdrControlWidget's SDRPipeline
    attaches/detaches (mirrors its own set_pipeline()). Subscribes to
    spectrum_ready/center_freq_changed only while a pipeline is attached;
    rows are only appended to history while the dialog is visible.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("SDR Waterfall"))
        self.setMinimumSize(_CANVAS_WIDTH + 20, _CANVAS_HEIGHT + 70)

        self._pipeline: Any = None  # SDRPipeline | None
        self._history: deque[NDArray[np.float32]] = deque(maxlen=_HISTORY_ROWS)
        self._latest_freqs: NDArray[np.float32] | None = None
        self._latest_powers: NDArray[np.float32] | None = None
        self._center_freq_hz: float | None = None

        layout = QVBoxLayout(self)

        ctrl_row = QHBoxLayout()
        self._auto_chk = QCheckBox(_("Auto Range"))
        self._auto_chk.setChecked(True)
        self._auto_chk.toggled.connect(self._on_auto_toggled)
        ctrl_row.addWidget(self._auto_chk)
        ctrl_row.addWidget(QLabel(_("Low:")))
        self._low_spin = QDoubleSpinBox()
        self._low_spin.setRange(-140.0, 20.0)
        self._low_spin.setSuffix(" dBFS")
        self._low_spin.setValue(_DEFAULT_LOW_DB)
        self._low_spin.setEnabled(False)
        ctrl_row.addWidget(self._low_spin)
        ctrl_row.addWidget(QLabel(_("High:")))
        self._high_spin = QDoubleSpinBox()
        self._high_spin.setRange(-140.0, 20.0)
        self._high_spin.setSuffix(" dBFS")
        self._high_spin.setValue(_DEFAULT_HIGH_DB)
        self._high_spin.setEnabled(False)
        ctrl_row.addWidget(self._high_spin)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        self._image_label = QLabel(_("Waiting for SDR spectrum data…"))
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background:#101010; color:#888;")
        self._image_label.setMinimumSize(_CANVAS_WIDTH, _CANVAS_HEIGHT)
        layout.addWidget(self._image_label, stretch=1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_pipeline(self, pipeline: Any) -> None:  # SDRPipeline | None
        """Attach or detach the active SDRPipeline (mirrors SdrControlWidget).

        Called whenever the SDR (re)connects or disconnects, so the popup
        keeps following whichever SDRPipeline instance is currently live
        instead of holding a stale reference from before a reconnect (the
        same pitfall fixed for the CW/FT4/Q65 tabs — see CLAUDE.md's "CW/
        FT4/Q65 タブ — SDR再接続でaudio_readyが二度と届かなくなるバグ").
        """
        if self._pipeline is not None:
            try:
                self._pipeline.spectrum_ready.disconnect(self._on_spectrum)
                self._pipeline.center_freq_changed.disconnect(self._on_center_freq)
            except Exception:
                pass
        self._pipeline = pipeline
        self._history.clear()
        self._latest_freqs = None
        self._latest_powers = None
        self._center_freq_hz = None
        if pipeline is not None:
            pipeline.spectrum_ready.connect(self._on_spectrum)
            pipeline.center_freq_changed.connect(self._on_center_freq)
        else:
            self._image_label.setPixmap(QPixmap())
            self._image_label.setText(_("Waiting for SDR spectrum data…"))

    # ------------------------------------------------------------------
    # Qt events
    # ------------------------------------------------------------------

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._history.clear()
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(_("Waiting for SDR spectrum data…"))

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_auto_toggled(self, checked: bool) -> None:
        self._low_spin.setEnabled(not checked)
        self._high_spin.setEnabled(not checked)

    def _on_center_freq(self, freq_hz: float) -> None:
        self._center_freq_hz = freq_hz

    def _on_spectrum(self, points: list[tuple[float, float]]) -> None:
        if not self.isVisible() or not points:
            return
        freqs = np.array([p[0] for p in points], dtype=np.float32)
        powers = np.array([p[1] for p in points], dtype=np.float32)
        if self._history and len(powers) != len(self._history[-1]):
            self._history.clear()
        self._history.append(powers)
        self._latest_freqs = freqs
        self._latest_powers = powers
        self._redraw()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _current_range(self) -> tuple[float, float]:
        if self._auto_chk.isChecked() and self._history:
            arr = np.stack(list(self._history))
            lo = float(np.percentile(arr, 5.0))
            hi = float(np.percentile(arr, 99.5))
            if hi - lo < 1.0:
                hi = lo + 1.0
            return lo, hi
        return self._low_spin.value(), self._high_spin.value()

    def _redraw(self) -> None:
        if self._latest_freqs is None or self._latest_powers is None or not self._history:
            return
        freq_lo = float(self._latest_freqs.min())
        freq_hi = float(self._latest_freqs.max())
        if freq_hi <= freq_lo:
            return
        lo_db, hi_db = self._current_range()

        canvas = QPixmap(_CANVAS_WIDTH, _CANVAS_HEIGHT)
        canvas.fill(QColor("#101010"))
        painter = QPainter(canvas)

        self._draw_spectrum(painter, freq_lo, freq_hi, lo_db, hi_db)
        axis_y = _MARGIN_TOP + _SPECTRUM_HEIGHT
        self._draw_freq_axis(painter, freq_lo, freq_hi, axis_y)
        self._draw_waterfall(painter, lo_db, hi_db)
        self._draw_center_marker(painter, freq_lo, freq_hi)

        painter.end()
        self._image_label.setPixmap(canvas)

    def _freq_to_x(self, hz: float, freq_lo: float, freq_hi: float) -> int:
        frac = (hz - freq_lo) / (freq_hi - freq_lo)
        return _MARGIN_LEFT + int(frac * _PLOT_WIDTH)

    def _draw_spectrum(
        self, painter: QPainter, freq_lo: float, freq_hi: float, lo_db: float, hi_db: float
    ) -> None:
        assert self._latest_freqs is not None
        assert self._latest_powers is not None
        top = _MARGIN_TOP
        rng = max(hi_db - lo_db, 1.0)

        painter.setPen(QPen(QColor("#333355"), 1))
        painter.drawRect(_MARGIN_LEFT, top, _PLOT_WIDTH, _SPECTRUM_HEIGHT)

        painter.setPen(QPen(QColor("#cccccc"), 1))
        painter.setFont(QFont("Sans", 7))
        for frac in (0.0, 0.5, 1.0):
            db = hi_db - frac * rng
            y = top + int(frac * _SPECTRUM_HEIGHT)
            painter.drawText(
                0, y - 6, _MARGIN_LEFT - 6, 12, Qt.AlignmentFlag.AlignRight, f"{db:.0f}"
            )

        xs = _MARGIN_LEFT + (self._latest_freqs - freq_lo) / (freq_hi - freq_lo) * _PLOT_WIDTH
        ys = top + _SPECTRUM_HEIGHT - (self._latest_powers - lo_db) / rng * _SPECTRUM_HEIGHT
        ys = np.clip(ys, top, top + _SPECTRUM_HEIGHT)
        painter.setPen(QPen(QColor("#00dcff"), 1))
        polyline = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)])
        painter.drawPolyline(polyline)

    def _draw_freq_axis(self, painter: QPainter, freq_lo: float, freq_hi: float, y: int) -> None:
        painter.setPen(QPen(QColor("#cccccc"), 1))
        painter.setFont(QFont("Sans", 7))
        step = nice_axis_step(freq_hi - freq_lo)
        hz = math.ceil(freq_lo / step) * step
        while hz <= freq_hi:
            x = self._freq_to_x(hz, freq_lo, freq_hi)
            painter.drawLine(x, y, x, y + 4)
            painter.drawText(
                x - 30, y + 5, 60, 12, Qt.AlignmentFlag.AlignHCenter, f"{hz / 1e6:.3f}"
            )
            hz += step

    def _draw_waterfall(self, painter: QPainter, lo_db: float, hi_db: float) -> None:
        arr = np.stack(list(reversed(self._history)))  # newest first (top row)
        norm = (arr - lo_db) / max(hi_db - lo_db, 1e-6)
        rgb = np.ascontiguousarray(color_map(norm.astype(np.float32)))
        n_rows, n_bins = arr.shape
        qimg = QImage(rgb.data, n_bins, n_rows, n_bins * 3, QImage.Format.Format_RGB888)
        wf_top = _MARGIN_TOP + _SPECTRUM_HEIGHT + _MARGIN_AXIS
        pix = QPixmap.fromImage(qimg).scaled(
            _PLOT_WIDTH,
            _WATERFALL_HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        painter.drawPixmap(_MARGIN_LEFT, wf_top, pix)
        painter.setPen(QPen(QColor("#333355"), 1))
        painter.drawRect(_MARGIN_LEFT, wf_top, _PLOT_WIDTH, _WATERFALL_HEIGHT)

    def _draw_center_marker(self, painter: QPainter, freq_lo: float, freq_hi: float) -> None:
        if self._center_freq_hz is None:
            return
        if not (freq_lo <= self._center_freq_hz <= freq_hi):
            return
        x = self._freq_to_x(self._center_freq_hz, freq_lo, freq_hi)
        pen = QPen(QColor("#ff3b30"), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(x, _MARGIN_TOP, x, _MARGIN_TOP + _SPECTRUM_HEIGHT)
        wf_top = _MARGIN_TOP + _SPECTRUM_HEIGHT + _MARGIN_AXIS
        painter.drawLine(x, wf_top, x, wf_top + _WATERFALL_HEIGHT)
