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
    QComboBox,
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
from sdr.overview import (
    MODE_NARROW,
    MODE_STANDARD,
    MODE_WIDE,
    OverviewData,
    compute_overview,
    remove_background,
    to_rgb,
)

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


def _offset_hz_at(x: int, plot: QRect, sample_rate: float) -> float:
    """Baseband frequency (Hz, relative to the recording's centre) at pixel column *x*."""
    return ((x - plot.left()) / max(1, plot.width()) - 0.5) * sample_rate


# What a click in the image loads into the playback (the dialog's "On click" menu).
CLICK_TIME_ONLY = 0
CLICK_TIME_AND_OFFSET = 1


def _nice_step(span: float, target: int) -> float:
    """A round tick step giving roughly *target* ticks over *span*."""
    raw = span / max(1, target)
    mag = 10 ** np.floor(np.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return float(m * mag)
    return float(10 * mag)


def _max_pool_axis0(rgb: np.ndarray, n: int) -> np.ndarray:
    """Reduce *rgb* (rows, cols, 3) to *n* rows, keeping the brightest pixel of each group.

    Drawing a tall image into a smaller rectangle drops rows, which loses a burst that
    is only one or two rows long. Brightness is the channel sum (the palette brightens
    with level).
    """
    rows = rgb.shape[0]
    if rows <= n:
        return rgb
    lum = rgb.astype(np.int16).sum(axis=2)
    edges = np.linspace(0, rows, n + 1).astype(np.int64)
    cols = np.arange(rgb.shape[1])
    out = np.empty((n, rgb.shape[1], 3), dtype=rgb.dtype)
    for i in range(n):
        lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
        out[i] = rgb[lo:hi][lum[lo:hi].argmax(axis=0), cols]
    return out


def _max_pool_rgb(rgb: np.ndarray, height: int, width: int) -> np.ndarray:
    """Max-pool *rgb* down to at most *height* x *width* (see :func:`_max_pool_axis0`)."""
    out = _max_pool_axis0(rgb, max(1, height))
    out = _max_pool_axis0(out.transpose(1, 0, 2), max(1, width)).transpose(1, 0, 2)
    return np.ascontiguousarray(out)


class _OverviewCanvas(QWidget):
    """Draws the spectrogram image with time / frequency axes."""

    hovered = Signal(str)
    # (seconds from the start of the recording, offset in Hz from the centre)
    clicked_at = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._rgb: np.ndarray | None = None  # keeps the QImage buffer alive
        # The image reduced to the plot size (see _display_image) and its size key.
        self._shown: tuple[int, int, np.ndarray, QImage] | None = None
        self._data: OverviewData | None = None
        self.setMinimumSize(520, 360)
        self.setMouseTracking(True)

    def set_image(self, rgb: np.ndarray, data: OverviewData) -> None:
        """Show *rgb* (rows, cols, 3 uint8) for *data*."""
        self._rgb = rgb
        h, w, _c = rgb.shape
        self._image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        self._data = data
        self._shown = None
        self.update()

    def clear(self) -> None:
        """Remove the image."""
        self._image = None
        self._rgb = None
        self._data = None
        self._shown = None
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

    def _display_image(self, width: int, height: int) -> QImage:
        """The image reduced (max-pooled) to the plot size so short bursts survive."""
        assert self._rgb is not None and self._image is not None
        rows, cols, _c = self._rgb.shape
        if rows <= height and cols <= width:
            return self._image
        if self._shown is None or self._shown[:2] != (width, height):
            small = _max_pool_rgb(self._rgb, height, width)
            h, w, _c = small.shape
            img = QImage(small.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            self._shown = (width, height, small, img)
        return self._shown[3]

    def _plot_rect(self, w: int, h: int) -> QRect:
        return QRect(_MARGIN_L, _MARGIN_T, w - _MARGIN_L - _MARGIN_R, h - _MARGIN_T - _MARGIN_B)

    def _paint(self, p: QPainter, w: int, h: int, fg: QColor) -> None:
        if self._image is None or self._data is None:
            return
        d = self._data
        r = self._plot_rect(w, h)
        p.drawImage(r, self._display_image(r.width(), r.height()))
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
        """Report the clicked time and offset (only for a left click inside the image)."""
        if self._data is None or self._image is None:
            return
        if event.button() != Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            return
        pos: QPoint = event.position().toPoint()  # type: ignore[attr-defined]
        r = self._plot_rect(self.width(), self.height())
        if r.contains(pos):
            frac = (pos.y() - r.top()) / max(1, r.height())
            self.clicked_at.emit(
                float(frac * self._data.duration_s),
                float(_offset_hz_at(pos.x(), r, self._data.sample_rate)),
            )

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
        f = _offset_hz_at(pos.x(), r, d.sample_rate) / 1000.0
        self.hovered.emit(_("Time %(t)s   Offset %(f).2f kHz") % {"t": _fmt_time(t), "f": f})


class SdrOverviewDialog(QDialog):
    """Non-modal window that renders the overview of one IQ recording."""

    _progress = Signal(int)
    _finished = Signal(object)  # OverviewData | None
    _failed = Signal(str)
    # Emitted when the user clicks the image: the time (seconds), the offset (Hz from
    # the recording's centre) and whether the offset is to be loaded as well (the
    # "On click" menu).
    seek_requested = Signal(float, float, bool)
    # The "On click" menu was changed (CLICK_TIME_ONLY / CLICK_TIME_AND_OFFSET).
    click_mode_changed = Signal(int)

    def __init__(
        self,
        path: Path,
        parent: QWidget | None = None,
        click_mode: int = CLICK_TIME_AND_OFFSET,
        show_play_hint: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(_("Pass Overview — %s") % path.name)
        self.resize(760, 640)
        self._path = path
        self._data: OverviewData | None = None
        # Computed images per display mode (switching back is instant) and the
        # cancel flag of the read currently running.
        self._cache: dict[str, OverviewData] = {}
        self._mode = MODE_STANDARD
        self._cancel = threading.Event()

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.addItem(_("Standard"), MODE_STANDARD)
        self._mode_combo.addItem(_("Wide (weak 9k6 bursts)"), MODE_WIDE)
        self._mode_combo.addItem(_("Narrow (CW / carriers)"), MODE_NARROW)
        self._mode_combo.setToolTip(
            _(
                "Standard: quick look, one short FFT per row.\n"
                "Wide: averages every FFT of a row and smooths across ~8 kHz, so weak\n"
                "wideband bursts (e.g. 9k6 FSK) show up. Reads the whole file (slower).\n"
                "Narrow: 15 Hz resolution, for CW beacons and carriers, including\n"
                "weak Doppler-drifting lines. Reads the whole file (slower)."
            )
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(QLabel(_("Mode:")))
        top.addWidget(self._mode_combo)
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

        click_row = QHBoxLayout()
        click_row.addWidget(QLabel(_("On click:")))
        self._click_combo = QComboBox()
        self._click_combo.addItem(
            _("Load the start time of the clicked point only"), CLICK_TIME_ONLY
        )
        self._click_combo.addItem(
            _("Load the start time and offset of the clicked point"), CLICK_TIME_AND_OFFSET
        )
        self._click_combo.setCurrentIndex(max(0, self._click_combo.findData(click_mode)))
        self._click_combo.currentIndexChanged.connect(
            lambda _i: self.click_mode_changed.emit(int(self._click_combo.currentData()))
        )
        click_row.addWidget(self._click_combo, 1)
        v.addLayout(click_row)

        if show_play_hint:
            hint = QLabel(_("Click to start playback."))
            hint.setStyleSheet("font-weight: bold;")
            v.addWidget(hint)
        self._canvas = _OverviewCanvas()
        v.addWidget(self._canvas, 1)
        self._info = QLabel("")
        self._info.setStyleSheet("color: gray; font-size: 11px;")
        self._canvas.hovered.connect(self._info.setText)
        self._canvas.clicked_at.connect(self._on_canvas_clicked)
        self._canvas.setToolTip(_("Click to jump the playback to that time"))
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        v.addWidget(self._info)
        v.addWidget(self._bar)

        self._progress.connect(self._bar.setValue)
        self._finished.connect(self._on_finished)
        self._failed.connect(self._on_failed)
        self._start_read(MODE_STANDARD)

    def _start_read(self, mode: str) -> None:
        """Read the file for *mode* in a background thread (stops any read still running)."""
        self._cancel.set()
        self._cancel = threading.Event()
        self._mode = mode
        self._bar.setValue(0)
        self._bar.show()
        threading.Thread(
            target=self._work, args=(mode, self._cancel), name="sdr-overview", daemon=True
        ).start()

    def _on_mode_changed(self) -> None:
        mode = str(self._mode_combo.currentData())
        if mode == self._mode:
            return
        cached = self._cache.get(mode)
        if cached is not None:
            self._cancel.set()
            self._mode = mode
            self._data = cached
            self._bar.hide()
            self._redraw()
            return
        self._save_btn.setEnabled(False)
        self._info.setText("")
        self._start_read(mode)

    def _on_canvas_clicked(self, position_s: float, offset_hz: float) -> None:
        """Forward a click, saying whether the offset is to be loaded as well."""
        with_offset = self._click_combo.currentData() == CLICK_TIME_AND_OFFSET
        self.seek_requested.emit(position_s, offset_hz, bool(with_offset))

    def _work(self, mode: str, cancel: threading.Event) -> None:
        """Background thread: read the file and build the spectrogram."""
        try:
            data = compute_overview(
                self._path,
                progress=lambda f: None if cancel.is_set() else self._progress.emit(int(f * 100)),
                cancelled=cancel.is_set,
                mode=mode,
            )
        except Exception as exc:
            logger.warning("SdrOverviewDialog: %s failed: %s", self._path, exc)
            if not cancel.is_set():
                self._failed.emit(str(exc))
            return
        if data is not None and not cancel.is_set():
            self._finished.emit(data)

    def _on_finished(self, data: object) -> None:
        assert isinstance(data, OverviewData)
        self._cache[data.mode] = data
        if data.mode != self._mode:
            return
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
        mode = self._data.mode
        db = remove_background(self._data.power_db, mode) if bg else self._data.power_db
        self._canvas.set_image(to_rgb(db, bg, mode), self._data)

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
