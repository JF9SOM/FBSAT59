"""Ft4WaterfallDialog — a WSJT-X-style static spectrogram popup for the FT4 tab.

Renders the just-completed RX period's audio as a time/frequency image so
the user can visually confirm whether real FT4 tone patterns are present
in the passband, independent of whether the decoder actually recognized
any of them. This is a diagnostic aid, not a live/continuous waterfall —
it redraws once per completed ~6s RX period (see
Ft4Tab._on_rx_period_ended), not continuously like WSJT-X's own display.

Axis convention matches WSJT-X's own waterfall: frequency runs horizontally
(low on the left), time runs vertically (period start at the top, period
end at the bottom).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from comms.ft4.codec import SAMPLE_RATE, Ft4Message
from i18n import _

# Display-only STFT parameters. Deliberately independent from
# codec.compute_waterfall(), which uses time/frequency oversampling and a
# fixed-point layout tuned specifically for ftx_find_candidates() — coupling
# this display helper to that format would risk breaking decode if this
# file is ever touched carelessly.
_F_MIN = 150.0
_F_MAX = 3100.0
_NFFT = 1024
_HOP = 256

# Plot area (the spectrogram image itself, excluding axis margins)
_PLOT_WIDTH = 620
_PLOT_HEIGHT = 260

# Margins reserved for axis ticks/labels
_MARGIN_LEFT = 55
_MARGIN_BOTTOM = 46
_MARGIN_TOP = 8
_MARGIN_RIGHT = 10

_CANVAS_WIDTH = _MARGIN_LEFT + _PLOT_WIDTH + _MARGIN_RIGHT
_CANVAS_HEIGHT = _MARGIN_TOP + _PLOT_HEIGHT + _MARGIN_BOTTOM

_FREQ_TICK_STEP_HZ = 500.0
_TIME_TICK_STEP_S = 1.0

# Simple black -> blue -> green -> yellow -> red palette, similar in spirit
# to WSJT-X's own waterfall coloring.
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


def compute_display_spectrogram(
    audio: NDArray[np.float32], sample_rate: int = SAMPLE_RATE
) -> tuple[NDArray[np.float32], float, int]:
    """Simple STFT magnitude-in-dB grid for display only (not decode).

    Returns (spec_db, bin_hz, min_bin) where spec_db has shape
    (n_frames, n_bins) — row 0 is the start of the buffer, one row per hop
    step, columns in ascending frequency order.
    """
    n = len(audio)
    if n < _NFFT:
        return np.zeros((0, 1), dtype=np.float32), sample_rate / _NFFT, 0
    bin_hz = sample_rate / _NFFT
    min_bin = max(0, int(_F_MIN / bin_hz))
    max_bin = min(_NFFT // 2, int(_F_MAX / bin_hz) + 1)
    window = np.hanning(_NFFT).astype(np.float32)
    n_frames = (n - _NFFT) // _HOP + 1
    spec = np.empty((n_frames, max_bin - min_bin), dtype=np.float32)
    for i in range(n_frames):
        frame = audio[i * _HOP : i * _HOP + _NFFT] * window
        mag = np.abs(np.fft.rfft(frame))[min_bin:max_bin]
        spec[i] = 20.0 * np.log10(np.maximum(mag, 1e-6))
    return spec, bin_hz, min_bin


def _color_map(norm: NDArray[np.float32]) -> NDArray[np.uint8]:
    """Map a 0..1 float array to an RGB uint8 array via a fixed palette."""
    n_colors = len(_PALETTE)
    idx_f = np.clip(norm, 0.0, 1.0) * (n_colors - 1)
    idx0 = np.floor(idx_f).astype(np.int32)
    idx1 = np.minimum(idx0 + 1, n_colors - 1)
    frac = (idx_f - idx0)[..., None]
    palette = np.array(_PALETTE, dtype=np.float32)
    rgb = palette[idx0] * (1 - frac) + palette[idx1] * frac
    return rgb.astype(np.uint8)


def _nice_ticks(lo: float, hi: float, step: float) -> list[float]:
    """Round-number tick positions in [lo, hi], spaced by `step`."""
    if hi <= lo:
        return []
    start = math.ceil(lo / step) * step
    ticks = []
    v = start
    while v <= hi + 1e-9:
        ticks.append(v)
        v += step
    return ticks


class Ft4WaterfallDialog(QDialog):
    """Non-modal popup showing the last RX period as a static spectrogram.

    Call update_waterfall() once per completed RX period while this dialog
    is visible; the caller (Ft4Tab) skips the call entirely while hidden to
    avoid wasted computation.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("FT4 Waterfall (last RX period)"))
        self.setMinimumSize(_CANVAS_WIDTH + 20, _CANVAS_HEIGHT + 40)
        layout = QVBoxLayout(self)
        self._image_label = QLabel(_("Waiting for the next RX period…"))
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background:#101010; color:#888;")
        self._image_label.setMinimumSize(_CANVAS_WIDTH, _CANVAS_HEIGHT)
        layout.addWidget(self._image_label, stretch=1)
        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

    def update_waterfall(self, audio: NDArray[np.float32], decoded: list[Ft4Message]) -> None:
        """Recompute and redraw from one period's audio."""
        spec, bin_hz, min_bin = compute_display_spectrogram(audio)
        n_frames, n_bins = spec.shape
        if n_frames == 0:
            return

        freq_lo = min_bin * bin_hz
        freq_hi = (min_bin + n_bins) * bin_hz
        duration_s = len(audio) / SAMPLE_RATE

        # Per-period percentile normalization (like WSJT-X's auto waterfall level)
        lo = float(np.percentile(spec, 5.0))
        hi = float(np.percentile(spec, 99.5))
        norm = (spec - lo) / max(hi - lo, 1e-6)
        # rgb: (n_frames, n_bins, 3) — rows already in chronological order
        # (period start at row 0) and columns already in ascending frequency
        # order, i.e. exactly the (time=rows/height, freq=columns/width)
        # layout WSJT-X uses, so no flip/transpose is needed here.
        rgb = np.ascontiguousarray(_color_map(norm.astype(np.float32)))
        qimg = QImage(rgb.data, n_bins, n_frames, n_bins * 3, QImage.Format.Format_RGB888)
        plot_pix = QPixmap.fromImage(qimg).scaled(
            _PLOT_WIDTH,
            _PLOT_HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        canvas = QPixmap(_CANVAS_WIDTH, _CANVAS_HEIGHT)
        canvas.fill(QColor("#101010"))
        painter = QPainter(canvas)
        painter.drawPixmap(_MARGIN_LEFT, _MARGIN_TOP, plot_pix)
        self._draw_axes(painter, freq_lo, freq_hi, duration_s)
        self._draw_decoded_markers(painter, decoded, freq_lo, freq_hi)
        painter.end()

        self._image_label.setPixmap(canvas)
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        self._status_label.setText(
            _("Updated {ts} UTC — {n} message(s) decoded").format(ts=ts, n=len(decoded))
        )

    def _draw_axes(
        self, painter: QPainter, freq_lo: float, freq_hi: float, duration_s: float
    ) -> None:
        painter.setPen(QPen(QColor("#cccccc"), 1))
        painter.setFont(QFont("Sans", 8))
        plot_bottom = _MARGIN_TOP + _PLOT_HEIGHT

        # Frequency ticks along the bottom (X axis)
        for hz in _nice_ticks(freq_lo, freq_hi, _FREQ_TICK_STEP_HZ):
            x = _MARGIN_LEFT + int((hz - freq_lo) / (freq_hi - freq_lo) * _PLOT_WIDTH)
            painter.drawLine(x, plot_bottom, x, plot_bottom + 4)
            painter.drawText(
                x - 18, plot_bottom + 6, 36, 14, Qt.AlignmentFlag.AlignHCenter, str(int(hz))
            )
        painter.drawText(
            _MARGIN_LEFT,
            plot_bottom + 24,
            _PLOT_WIDTH,
            14,
            Qt.AlignmentFlag.AlignHCenter,
            _("Frequency (Hz)"),
        )

        # Time ticks along the left (Y axis) — period start (0s) at the top
        for sec in _nice_ticks(0.0, duration_s, _TIME_TICK_STEP_S):
            y = _MARGIN_TOP + int(sec / duration_s * _PLOT_HEIGHT)
            painter.drawLine(_MARGIN_LEFT - 4, y, _MARGIN_LEFT, y)
            painter.drawText(
                0, y - 7, _MARGIN_LEFT - 6, 14, Qt.AlignmentFlag.AlignRight, f"{sec:.0f}s"
            )
        painter.save()
        painter.translate(12, _MARGIN_TOP + _PLOT_HEIGHT / 2)
        painter.rotate(-90)
        painter.drawText(-60, -6, 120, 14, Qt.AlignmentFlag.AlignHCenter, _("Time (s)"))
        painter.restore()

        # Plot border
        painter.drawRect(_MARGIN_LEFT, _MARGIN_TOP, _PLOT_WIDTH, _PLOT_HEIGHT)

    def _draw_decoded_markers(
        self,
        painter: QPainter,
        decoded: list[Ft4Message],
        freq_lo: float,
        freq_hi: float,
    ) -> None:
        if not decoded or freq_hi <= freq_lo:
            return
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
        for msg in decoded:
            x = _MARGIN_LEFT + int((msg.freq_hz - freq_lo) / (freq_hi - freq_lo) * _PLOT_WIDTH)
            painter.drawLine(x, _MARGIN_TOP, x, _MARGIN_TOP + _PLOT_HEIGHT)
            painter.drawText(x + 2, _MARGIN_TOP + 10, f"{int(msg.freq_hz)}")
