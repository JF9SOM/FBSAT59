"""MeteorWaterfallWidget — a compact scrolling spectrogram for the METEOR tab.

Shown in place of the received-image preview while SatDump is running, so
the operator can see at a glance whether RF is actually being received
during the (potentially many-minutes-long) LRPT/HRPT pass -- SatDump only
writes the decoded image once reception finishes (see satdump.py's
--finish_processing note in CLAUDE.md), so without this the preview area
would otherwise stay black for the whole pass.

Data comes from SatDump's own built-in --fft_enable/--http_server HTTP API
(polled by comms.meteor.fft_waterfall.SatDumpFftPoller), not from FBSAT59's
own SDR pipeline -- SatDump holds the SDR exclusively while running (see
MeteorTab's module docstring), so this cannot reuse SDR Control's waterfall.

Deliberately has no calibrated frequency axis: SatDump's raw fft_values
bin ordering/scaling is not documented, so labelling it with a possibly
wrong Hz scale would be worse than no axis at all. The only goal here is
qualitative "is a signal present", not frequency-accurate diagnostics.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from i18n import _

_PLOT_WIDTH = 400
# ~2 minutes of history at the poller's ~2.5 Hz poll rate (comms.meteor.
# fft_waterfall._POLL_INTERVAL_S) -- a rolling window, not the whole pass.
_HISTORY_ROWS = 240

# Simple black -> blue -> green -> yellow -> red palette, matching the
# style already used by ui.ft4_waterfall_dialog (kept as an independent
# copy rather than a shared import -- see that file's own note about not
# coupling unrelated features together).
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


class MeteorWaterfallWidget(QWidget):
    """Scrolling spectrogram fed by SatDumpFftPoller's callbacks (via MeteorTab)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self._canvas = QLabel(_("Waiting for signal data…"))
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setStyleSheet("border: 1px solid #555; background: #111; color: #888;")
        self._canvas.setMinimumSize(300, 200)
        self._canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._canvas, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #888;")
        layout.addWidget(self._status)

        self._rows: deque[NDArray[np.float32]] = deque(maxlen=_HISTORY_ROWS)

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear history — called at the start of a new reception run."""
        self._rows.clear()
        self._canvas.setPixmap(QPixmap())
        self._canvas.setText(_("Waiting for signal data…"))
        self._status.setText("")

    def add_frame(self, values: object) -> None:
        """Append one FFT snapshot and redraw. *values* is a list[float] of dB magnitudes."""
        arr = np.asarray(values, dtype=np.float32)
        if arr.size == 0:
            return
        x_old = np.linspace(0.0, 1.0, num=arr.size)
        x_new = np.linspace(0.0, 1.0, num=_PLOT_WIDTH)
        resampled = np.interp(x_new, x_old, arr).astype(np.float32)
        self._rows.append(resampled)
        self._redraw()
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        self._status.setText(_("Last update: {ts} UTC").format(ts=ts))

    def show_unavailable(self, message: str) -> None:
        """Called when SatDumpFftPoller can't reach SatDump's HTTP API.

        Keeps any spectrogram already drawn on screen (the API may simply
        be slow to start, or briefly hiccup mid-run) -- only replaces the
        canvas with the message text if nothing has been received yet.
        """
        self._status.setText(message)
        if not self._rows:
            self._canvas.setPixmap(QPixmap())
            self._canvas.setText(message)

    # ------------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._rows:
            self._redraw()

    def _redraw(self) -> None:
        if not self._rows:
            return
        # Newest row first (top of the image), like ui.ft4_waterfall_dialog.
        full = np.stack(list(reversed(self._rows)), axis=0)
        lo = float(np.percentile(full, 5.0))
        hi = float(np.percentile(full, 99.5))
        norm = (full - lo) / max(hi - lo, 1e-6)
        rgb = np.ascontiguousarray(_color_map(norm.astype(np.float32)))
        n_rows, n_cols = full.shape
        qimg = QImage(rgb.data, n_cols, n_rows, n_cols * 3, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg).scaled(
            max(self._canvas.width(), 1),
            max(self._canvas.height(), 1),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._canvas.setPixmap(pix)
