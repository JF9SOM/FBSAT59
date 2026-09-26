"""SdrOverviewDialog — whole-recording spectrogram of an IQ WAV file.

Shows the entire pass at a glance (time downwards, frequency across) instead
of having to play the recording through the waterfall from start to end. The
file is read once in a background thread (sdr/overview.py); the image is drawn
with Qt only. "Remove background" subtracts each frequency's median over time
so constant lines vanish and short bursts / drifting carriers stand out.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from i18n import _
from sdr.overview import OverviewData, compute_overview, remove_background, to_rgb

logger = logging.getLogger(__name__)

_MARGIN_L = 56
_MARGIN_B = 24
_MARGIN_T = 6
_MARGIN_R = 8


def _fmt_time(seconds: float) -> str:
    """Format *seconds* as M:SS (or H:MM:SS for long recordings)."""
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _nice_step(span: float, target: int) -> float:
    """A round tick step giving roughly *target* ticks over *span*."""
    raw = span / max(1, target)
    mag = 10 ** np.floor(np.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return float(m * mag)
    return float(10 * mag)


class _OverviewCanvas(QWidget):
    """Draws the spectrogram image with time / frequency axes."""

    hovered = Signal(str)
    clicked_at = Signal(float)  # seconds from the start of the recording

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._rgb: np.ndarray | None = None  # keeps the QImage buffer alive
        self._data: OverviewData | None = None
        self.setMinimumSize(520, 360)
        self.setMouseTracking(True)

    def set_image(self, rgb: np.ndarray, data: OverviewData) -> None:
        """Show *rgb* (rows, cols, 3 uint8) for *data*."""
        self._rgb = rgb
        h, w, _c = rgb.shape
        self._image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        self._data = data
        self.update()

    def clear(self) -> None:
        """Remove the image."""
        self._image = None
        self._rgb = None
        self._data = None
        self.update()

    def image(self) -> QImage | None:
        """The current image with axes rendered (for saving), or None."""
        if self._image is None:
            return None
        out = QImage(max(self.width(), 900), max(self.height(), 700), QImage.Format.Format_RGB32)
        out.fill(QColor("white"))
        painter = QPainter(out)
        self._paint(painter, out.width(), out.height(), QColor("black"))
        painter.end()
        return out

    def _plot_rect(self, w: int, h: int) -> QRect:
        return QRect(_MARGIN_L, _MARGIN_T, w - _MARGIN_L - _MARGIN_R, h - _MARGIN_T - _MARGIN_B)

    def _paint(self, p: QPainter, w: int, h: int, fg: QColor) -> None:
        if self._image is None or self._data is None:
            return
        d = self._data
        r = self._plot_rect(w, h)
        p.drawImage(r, self._image)
        p.setPen(fg)
        p.drawRect(r)
        # Frequency axis (kHz relative to the recording's centre).
        half = d.sample_rate / 2.0 / 1000.0
        step = _nice_step(2 * half, 8)
        f = np.ceil(-half / step) * step
        while f <= half:
            x = r.left() + int((f + half) / (2 * half) * r.width())
            p.drawLine(x, r.bottom(), x, r.bottom() + 4)
            p.drawText(
                QRect(x - 30, r.bottom() + 5, 60, 16), Qt.AlignmentFlag.AlignCenter, f"{f:g}"
            )
            f += step
        p.drawText(
            QRect(r.right() - 90, r.bottom() + 5, 90, 16),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "kHz",
        )
        # Time axis.
        tstep = _nice_step(d.duration_s, 8)
        t = 0.0
        while t <= d.duration_s:
            y = r.top() + int(t / d.duration_s * r.height())
            p.drawLine(r.left() - 4, y, r.left(), y)
            p.drawText(
                QRect(0, y - 8, r.left() - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt_time(t),
            )
            t += tstep

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().window())
        self._paint(p, self.width(), self.height(), self.palette().text().color())
        p.end()

    def mousePressEvent(self, event: object) -> None:  # noqa: N802
        """Report the clicked time (only for a left click inside the image)."""
        if self._data is None or self._image is None:
            return
        if event.button() != Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            return
        pos: QPoint = event.position().toPoint()  # type: ignore[attr-defined]
        r = self._plot_rect(self.width(), self.height())
        if r.contains(pos):
            frac = (pos.y() - r.top()) / max(1, r.height())
            self.clicked_at.emit(float(frac * self._data.duration_s))

    def mouseMoveEvent(self, event: object) -> None:  # noqa: N802
        if self._data is None or self._image is None:
            return
        pos: QPoint = event.position().toPoint()  # type: ignore[attr-defined]
        r = self._plot_rect(self.width(), self.height())
        if not r.contains(pos):
            self.hovered.emit("")
            return
        d = self._data
        t = (pos.y() - r.top()) / r.height() * d.duration_s
        f = ((pos.x() - r.left()) / r.width() - 0.5) * d.sample_rate / 1000.0
        self.hovered.emit(_("Time %(t)s   Offset %(f).2f kHz") % {"t": _fmt_time(t), "f": f})


class SdrOverviewDialog(QDialog):
    """Non-modal window that renders the overview of one IQ recording."""

    _progress = Signal(int)
    _finished = Signal(object)  # OverviewData | None
    _failed = Signal(str)
    # Emitted with the time (seconds) the user clicked in the image.
    seek_requested = Signal(float)

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(_("Pass Overview — %s") % path.name)
        self.resize(760, 640)
        self._path = path
        self._data: OverviewData | None = None
        self._cancel = threading.Event()

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self._bg_check = QCheckBox(_("Remove background"))
        self._bg_check.setChecked(True)
        self._bg_check.setToolTip(
            _(
                "Subtract each frequency's typical level over the whole recording,\n"
                "so constant lines disappear and short bursts stand out."
            )
        )
        self._bg_check.toggled.connect(self._redraw)
        self._save_btn = QPushButton(_("Save PNG…"))
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        top.addWidget(self._bg_check)
        top.addStretch()
        top.addWidget(self._save_btn)
        v.addLayout(top)

        self._canvas = _OverviewCanvas()
        v.addWidget(self._canvas, 1)
        self._info = QLabel("")
        self._info.setStyleSheet("color: gray; font-size: 11px;")
        self._canvas.hovered.connect(self._info.setText)
        self._canvas.clicked_at.connect(self.seek_requested)
        self._canvas.setToolTip(_("Click to jump the playback to that time"))
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        v.addWidget(self._info)
        v.addWidget(self._bar)

        self._progress.connect(self._bar.setValue)
        self._finished.connect(self._on_finished)
        self._failed.connect(self._on_failed)
        threading.Thread(target=self._work, name="sdr-overview", daemon=True).start()

    def _work(self) -> None:
        """Background thread: read the file and build the spectrogram."""
        try:
            data = compute_overview(
                self._path,
                progress=lambda f: self._progress.emit(int(f * 100)),
                cancelled=self._cancel.is_set,
            )
        except Exception as exc:
            logger.warning("SdrOverviewDialog: %s failed: %s", self._path, exc)
            if not self._cancel.is_set():
                self._failed.emit(str(exc))
            return
        if data is not None and not self._cancel.is_set():
            self._finished.emit(data)

    def _on_finished(self, data: object) -> None:
        assert isinstance(data, OverviewData)
        self._data = data
        self._bar.hide()
        self._save_btn.setEnabled(True)
        self._redraw()

    def _on_failed(self, message: str) -> None:
        self._bar.hide()
        self._info.setText(_("Could not read the recording: %s") % message)

    def _redraw(self) -> None:
        if self._data is None:
            return
        bg = self._bg_check.isChecked()
        db = remove_background(self._data.power_db) if bg else self._data.power_db
        self._canvas.set_image(to_rgb(db, bg), self._data)

    def _on_save(self) -> None:
        img = self._canvas.image()
        if img is None:
            return
        default = str(self._path.with_suffix("").with_suffix(".overview.png"))
        name, _f = QFileDialog.getSaveFileName(self, _("Save PNG…"), default, "PNG (*.png)")
        if name:
            img.save(name, b"PNG")

    def done(self, result: int) -> None:
        """Stop the background read when the window closes."""
        self._cancel.set()
        super().done(result)

    def closeEvent(self, event: object) -> None:  # noqa: N802
        """Stop the background read when the window is closed with the title-bar ×."""
        self._cancel.set()
        super().closeEvent(event)  # type: ignore[arg-type]
