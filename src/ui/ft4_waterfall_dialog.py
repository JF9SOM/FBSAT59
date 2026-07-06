"""Ft4WaterfallDialog — a WSJT-X-style static spectrogram popup for the FT4 tab.

Renders the just-completed RX period's audio as a time/frequency image so
the user can visually confirm whether real FT4 tone patterns are present
in the passband, independent of whether the decoder actually recognized
any of them. This is a diagnostic aid, not a live/continuous waterfall —
it redraws once per completed ~6s RX period (see
Ft4Tab._on_rx_period_ended), not continuously like WSJT-X's own display.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
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

_IMG_WIDTH = 700
_IMG_HEIGHT = 280

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
    (n_frames, n_bins), one row per hop step.
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


class Ft4WaterfallDialog(QDialog):
    """Non-modal popup showing the last RX period as a static spectrogram.

    Call update_waterfall() once per completed RX period while this dialog
    is visible; the caller (Ft4Tab) skips the call entirely while hidden to
    avoid wasted computation.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("FT4 Waterfall (last RX period)"))
        self.setMinimumSize(_IMG_WIDTH + 20, _IMG_HEIGHT + 60)
        layout = QVBoxLayout(self)
        self._image_label = QLabel(_("Waiting for the next RX period…"))
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background:#000; color:#888;")
        self._image_label.setMinimumHeight(_IMG_HEIGHT)
        layout.addWidget(self._image_label, stretch=1)
        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

    def update_waterfall(self, audio: NDArray[np.float32], decoded: list[Ft4Message]) -> None:
        """Recompute and redraw from one period's audio."""
        spec, bin_hz, min_bin = compute_display_spectrogram(audio)
        if spec.shape[0] == 0:
            return

        # Per-period percentile normalization (like WSJT-X's auto waterfall level)
        lo = float(np.percentile(spec, 5.0))
        hi = float(np.percentile(spec, 99.5))
        norm = (spec - lo) / max(hi - lo, 1e-6)
        rgb = _color_map(norm.astype(np.float32))  # (n_frames, n_bins, 3)
        rgb = np.flip(rgb, axis=1)  # low frequency at the bottom of the image
        img_arr = np.ascontiguousarray(np.transpose(rgb, (1, 0, 2)))  # (n_bins, n_frames, 3)
        h, w, _channels = img_arr.shape
        qimg = QImage(img_arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg).scaled(
            _IMG_WIDTH,
            _IMG_HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        if decoded:
            pix = pix.copy()
            painter = QPainter(pix)
            painter.setPen(QPen(QColor("white"), 2))
            n_bins = h
            for msg in decoded:
                bin_pos = msg.freq_hz / bin_hz - min_bin
                frac_y = 1.0 - (bin_pos / max(n_bins - 1, 1))
                y = int(frac_y * _IMG_HEIGHT)
                painter.drawLine(0, y, 10, y)
            painter.end()

        self._image_label.setPixmap(pix)
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        self._status_label.setText(
            _("Updated {ts} UTC — {n} message(s) decoded").format(ts=ts, n=len(decoded))
        )
