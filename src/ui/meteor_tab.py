"""METEOR / HRPT reception tab — Communications > METEOR / HRPT.

Uses SatDump as a subprocess to receive and decode LRPT imagery from
METEOR-M satellites.  While SatDump is running it holds exclusive access
to the SDR device, so the SDR Control tab is greyed out.

Lifecycle
---------
* User opens the tab via Communications > METEOR / HRPT.
* User selects a satellite / pipeline from the combo box.
* User clicks [SDR Connect] to verify the configured SDR is reachable.
* User clicks [▶ Start]:
    - If an SDR is active, it is disconnected automatically.
    - The SDR Control tab is disabled.
    - SatDumpProcess is launched in a background QThread.
    - ImageWatcher polls the output directory for new PNGs.
* User clicks [■ Stop] (or the process ends on its own):
    - SatDump is terminated.
    - SDR Control tab is re-enabled.
* Tab × closes the tab and stops any running process.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QIcon,
    QImage,
    QPixmap,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from comms.meteor.cities_overlay import CitiesOverlayProcess, find_product_cbor
from comms.meteor.fft_waterfall import SatDumpFftPoller, find_free_port
from comms.meteor.image_watcher import ImageWatcher
from comms.meteor.satdump import METEOR_PIPELINES, SatDumpProcess, find_satdump
from i18n import _
from ui.meteor_waterfall import MeteorWaterfallWidget

_THUMB_W = 160
_THUMB_H = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _image_priority(filename: str) -> int:
    """Rank a SatDump output PNG by how good an at-a-glance preview it makes.

    A single pass writes roughly a dozen PNGs (raw per-channel grayscale,
    several false-color composites with/without color correction, and --
    only when enough of the swath was captured -- a map-overlaid and/or
    fully reprojected/georeferenced version of one composite). Used to pick
    which file becomes the main preview, both live (see _on_new_image) and
    when loading a past reception's folder (see _load_images_from_folder).

    "*_map*" files keep the swath's native crop (same frame as the plain
    composites) with a coastline overlay drawn directly on the received
    data, so the whole preview area is filled with real imagery -- the
    best quick look, more so when also color-corrected. "*projected*"
    files are warped onto a whole-globe canvas so the satellite ends up
    geographically correct, but only a narrow strip of that canvas
    actually has data; the rest renders as a mostly-black world map, which
    makes a poor default preview even though it is arguably the most
    "complete" product. Confirmed by inspecting actual pixel dimensions:
    *_map* files match the plain swath size (e.g. 1568x1376), while
    *_projected* is a fixed whole-globe canvas (4096x2048) regardless of
    how much of it has real data.
    """
    lower = filename.lower()
    if "projected" in lower:
        return 0
    has_map = "_map" in lower
    has_corrected = "corrected" in lower
    if has_map and has_corrected:
        return 4
    if has_map:
        return 3
    if has_corrected:
        return 2
    return 1


def _default_output_dir() -> Path:
    from PySide6.QtCore import QStandardPaths

    pics = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    base = Path(pics) if pics else Path.home() / "Pictures"
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return base / "fbsat59_meteor" / ts


def _load_sdr_settings() -> dict[str, Any]:
    """Load SDR settings saved by Rig Settings dialog from app_settings DB."""
    try:
        from data.database import get_db_path

        db_path = get_db_path()
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'sdr_settings'").fetchone()
        conn.close()
        if row and row["value"]:
            return dict(json.loads(row["value"]))
    except Exception:
        pass
    return {}


def _load_meteor_settings() -> dict[str, Any]:
    """Load METEOR-tab-local gain settings (independent of Rig Settings' SDR Settings).

    Returns {} if the user has never touched the gain controls in this tab,
    so the caller can fall back to seeding from the shared sdr_settings once.
    """
    try:
        from data.database import get_db_path

        db_path = get_db_path()
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'meteor_settings'"
        ).fetchone()
        conn.close()
        if row and row["value"]:
            return dict(json.loads(row["value"]))
    except Exception:
        pass
    return {}


def _save_meteor_settings(data: dict[str, Any]) -> None:
    """Persist METEOR-tab-local gain settings, separate from sdr_settings."""
    try:
        from data.database import get_db_path

        db_path = get_db_path()
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('meteor_settings', ?)",
            (json.dumps(data),),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _sdr_source_from_settings(sdr: dict[str, Any]) -> str:
    """Extract a SoapySDR source driver string from saved SDR settings."""
    args: dict[str, str] = sdr.get("device_args") or {}
    driver = args.get("driver", "")
    if driver:
        return driver
    label: str = sdr.get("device_label") or ""
    for token in label.lower().split():
        if token in ("rtlsdr", "hackrf", "airspy", "sdrplay", "plutosdr", "limesdr"):
            return token
    return "rtlsdr"


# ---------------------------------------------------------------------------
# Floating log window
# ---------------------------------------------------------------------------


class _LogWindow(QDialog):
    """Modeless floating window that shows SatDump stdout/stderr."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(_("SatDump Log"))
        self.resize(640, 320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(2000)
        self._view.setStyleSheet("font-family: monospace; font-size: 10px;")
        layout.addWidget(self._view)
        btn_row = QHBoxLayout()
        btn_save = QPushButton(_("💾 Save…"))
        btn_save.clicked.connect(self._on_save)
        btn_clear = QPushButton(_("Clear"))
        btn_clear.clicked.connect(self._view.clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_clear)
        layout.addLayout(btn_row)

    def append(self, line: str) -> None:
        self._view.appendPlainText(line)
        self._view.ensureCursorVisible()

    def _on_save(self) -> None:
        from PySide6.QtCore import QStandardPaths

        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        default_dir = desktop if desktop else str(Path.home())
        default_name = f"satdump_log_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.txt"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Save Log"),
            str(Path(default_dir) / default_name),
            _("Text files (*.txt);;All files (*)"),
        )
        if not path:
            return
        try:
            Path(path).write_text(self._view.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, _("Save Failed"), str(exc))

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        # Hide rather than destroy so log content is preserved
        event.ignore()
        self.hide()


# ---------------------------------------------------------------------------
# Thumbnail list item
# ---------------------------------------------------------------------------


class _ThumbItem(QListWidgetItem):
    def __init__(self, image: QImage, label: str, path: Path | None = None) -> None:
        super().__init__()
        self.full_image = image.copy()
        self.path = path  # source PNG path, if known — used to locate product.cbor
        thumb = image.scaled(
            _THUMB_W,
            _THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setIcon(QIcon(QPixmap.fromImage(thumb)))
        self.setText(label)
        self.setSizeHint(QSize(_THUMB_W + 8, _THUMB_H + 28))


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------


class MeteorTab(QWidget):
    """Non-resident tab opened from Communications > METEOR / HRPT."""

    # Emitted when the user changes the pipeline combo so main_window can
    # sync the satellite list and Radio Control transponder selection.
    satellite_selection_requested: Signal = Signal(int, int)  # norad, downlink_hz

    # Bridge SatDumpFftPoller's background-thread callbacks (see _on_start)
    # into this QObject's own thread -- the poller itself is a plain
    # threading.Thread and must not touch widgets directly.
    _fft_frame_received: Signal = Signal(object)  # list[float]
    _fft_unavailable: Signal = Signal(str)

    def __init__(
        self,
        sdr_control_tab: QWidget | None = None,
        sdr_widget: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sdr_control_tab = sdr_control_tab
        self._sdr_widget = sdr_widget  # SdrControlWidget instance for disconnect
        self._process: SatDumpProcess | None = None
        self._watcher: ImageWatcher | None = None
        self._fft_poller: SatDumpFftPoller | None = None
        self._output_dir: Path | None = None
        self._preview_priority: int = -1  # see _image_priority()
        self._suppress_sync: bool = False  # prevents feedback loop during Radio Control sync
        self._log_window: _LogWindow | None = None
        self._log_buffer: deque[str] = deque(maxlen=2000)
        # Display state for the current preview image (see _show_image /
        # _rescale_and_display). _current_original_image is always the
        # unrotated source -- rotation is applied fresh on every
        # display/save so toggling Flip 180° doesn't need to "undo" a
        # previous rotation.
        self._current_original_image: QImage | None = None
        self._image_rotated: bool = False
        self._fit_mode: str = "fit"  # "fit" (both, default) | "width" | "height"
        # None = follow _fit_mode; a float = an explicit mouse-wheel zoom
        # level that overrides it until a Fit menu item is picked again or
        # a different image is shown (see _on_image_wheel()).
        self._zoom_factor: float | None = None
        self._cities_overlay_process: CitiesOverlayProcess | None = None
        self._setup_ui()
        self._fft_frame_received.connect(self._waterfall_widget.add_frame)
        self._fft_unavailable.connect(self._waterfall_widget.show_unavailable)
        self._check_satdump()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # --- Warning banner (fixed at top, hidden when SatDump is found) ---
        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "background:#c0392b; color:white; padding:6px; border-radius:4px;"
        )
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        # --- Control row (compact single group box) ---
        ctrl_box = QGroupBox(_("Reception Control"))
        ctrl_layout = QVBoxLayout(ctrl_box)
        ctrl_layout.setContentsMargins(6, 4, 6, 4)
        ctrl_layout.setSpacing(3)

        # Row 1: pipeline combo + action buttons
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(QLabel(_("Pipeline:")))
        self._combo_sat = QComboBox()
        for p in METEOR_PIPELINES:
            self._combo_sat.addItem(str(p["label"]), p)
        self._combo_sat.currentIndexChanged.connect(self._on_pipeline_changed)
        row1.addWidget(self._combo_sat, 1)

        self._btn_sdr_connect = QPushButton(_("SDR Connect"))
        self._btn_sdr_connect.setToolTip(
            _("Verify the SDR configured in Rig Settings > SDR Settings is reachable")
        )
        self._btn_sdr_connect.clicked.connect(self._on_sdr_connect)
        row1.addWidget(self._btn_sdr_connect)

        self._btn_start = QPushButton(_("▶  Start"))
        self._btn_start.clicked.connect(self._on_start)
        row1.addWidget(self._btn_start)

        self._btn_stop = QPushButton(_("■  Stop"))
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        row1.addWidget(self._btn_stop)

        self._btn_log = QPushButton(_("📋 Log"))
        self._btn_log.setToolTip(_("Show SatDump output log"))
        self._btn_log.clicked.connect(self._on_show_log)
        row1.addWidget(self._btn_log)

        btn_help = QPushButton("?")
        btn_help.setFixedSize(22, 22)
        btn_help.setToolTip(_("About SatDump installation"))
        btn_help.clicked.connect(self._on_help)
        row1.addWidget(btn_help)

        ctrl_layout.addLayout(row1)

        # Row 2: status + lock + progress
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self._lbl_lock = QLabel(_("Lock: —"))
        self._lbl_lock.setMinimumWidth(70)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        self._progress.setMaximumWidth(160)
        self._lbl_status = QLabel(_("Ready.  Select a pipeline and press Start."))
        row2.addWidget(self._lbl_lock)
        row2.addWidget(self._progress)
        row2.addWidget(self._lbl_status, 1)
        ctrl_layout.addLayout(row2)

        root.addWidget(ctrl_box)

        # --- Horizontal splitter: main image | thumbnail history ---
        h_split = QSplitter(Qt.Orientation.Horizontal)

        image_widget = QWidget()
        image_layout = QVBoxLayout(image_widget)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(2)

        # Image / Waterfall tabs share this area: SatDump only writes the
        # decoded image once reception finishes (see satdump.py's
        # --finish_processing note), so a live-updating waterfall fills
        # the gap during the (often many-minutes-long) pass instead of
        # leaving the preview area black the whole time.
        self._preview_tabs = QTabWidget()
        self._image_label = QLabel(_("No image received yet."))
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(300, 200)
        self._image_label.setStyleSheet("background: #111;")
        self._image_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._image_label.customContextMenuRequested.connect(self._on_image_context_menu)
        # Mouse-wheel zoom (see eventFilter/_on_image_wheel) — installed on
        # the label itself, inside the QScrollArea, so it's caught before
        # the scroll area's own default wheel-scroll behavior.
        self._image_label.installEventFilter(self)
        # Wrapped in a QScrollArea (rather than relying on the label's own
        # sizeHint) so "Fit Width"/"Fit Height" can deliberately let the
        # image overflow the viewport in one direction and be scrolled to,
        # instead of always being squeezed to fit both dimensions like the
        # previous plain-QLabel display did.
        self._image_scroll = QScrollArea()
        self._image_scroll.setWidgetResizable(False)
        self._image_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_scroll.setWidget(self._image_label)
        self._image_scroll.setStyleSheet("QScrollArea { border: 1px solid #555; }")
        self._preview_tabs.addTab(self._image_scroll, _("Image"))
        self._waterfall_widget = MeteorWaterfallWidget()
        self._preview_tabs.addTab(self._waterfall_widget, _("Waterfall"))
        image_layout.addWidget(self._preview_tabs, 1)

        btn_row2 = QHBoxLayout()
        btn_row2.setContentsMargins(0, 0, 0, 0)
        self._btn_open_folder = QPushButton(_("📁 Open Folder"))
        self._btn_open_folder.clicked.connect(self._on_open_folder)
        self._btn_open_past = QPushButton(_("📂 Open Past Reception…"))
        self._btn_open_past.setToolTip(
            _("Load a previous reception's images into the preview and history below.")
        )
        self._btn_open_past.clicked.connect(self._on_open_past)
        self._btn_clear = QPushButton(_("🗑 Clear"))
        self._btn_clear.clicked.connect(self._on_clear_history)
        btn_row2.addWidget(self._btn_open_folder)
        btn_row2.addWidget(self._btn_open_past)
        btn_row2.addWidget(self._btn_clear)

        # METEOR-local RF gain override (independent of Rig Settings > SDR
        # Settings' shared gain, which is tuned for other uses like FM/SDR
        # Control listening and may not suit 137 MHz LRPT reception). Manual
        # only -- AGC has been confirmed to produce spurious Viterbi "sync"
        # on pure noise for METEOR reception, so Auto is intentionally not
        # offered here. See CLAUDE.md "METEOR受信専用のRF Gain設定" for the
        # background.
        btn_row2.addSpacing(12)
        btn_row2.addWidget(QLabel(_("Gain:")))
        self._gain_spin = QSpinBox()
        self._gain_spin.setRange(0, 80)
        self._gain_spin.setSuffix(" dB")
        self._gain_spin.valueChanged.connect(self._on_gain_setting_changed)
        btn_row2.addWidget(self._gain_spin)
        self._load_gain_settings()

        btn_row2.addStretch()
        image_layout.addLayout(btn_row2)
        h_split.addWidget(image_widget)

        # Flip 180° / Fit Width / Fit Height live in the image's right-click
        # menu (_on_image_context_menu) rather than as buttons here -- a
        # third button row didn't fit alongside the file-management row
        # without either crowding a single row or eating into the preview's
        # vertical space, and these are toggles most naturally reached from
        # the image itself anyway.
        history_widget = QWidget()
        hl = QVBoxLayout(history_widget)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(QLabel(_("Received Images:")))
        self._history_list = QListWidget()
        self._history_list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self._history_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._history_list.currentItemChanged.connect(self._on_history_selection)
        hl.addWidget(self._history_list)

        # Placed under the history list (rather than under the image) so it
        # doesn't compete with the preview for vertical space. Hidden once a
        # cities-overlay image already exists for the selected reception —
        # see _update_cities_overlay_button_visibility().
        self._btn_cities_overlay = QPushButton(_("🏙️ Add Cities Overlay"))
        self._btn_cities_overlay.setToolTip(
            _(
                "Re-render the selected image with SatDump's own city-label "
                "overlay (needs product.cbor from the same reception)."
            )
        )
        self._btn_cities_overlay.clicked.connect(self._on_add_cities_overlay)
        hl.addWidget(self._btn_cities_overlay)
        h_split.addWidget(history_widget)

        h_split.setSizes([680, 180])
        root.addWidget(h_split, 1)

    # ------------------------------------------------------------------
    # METEOR-local RF gain (independent of Rig Settings > SDR Settings)
    # ------------------------------------------------------------------

    def _load_gain_settings(self) -> None:
        """Populate the gain spinbox, seeding from sdr_settings on first use.

        meteor_settings is only ever written once the user actually touches
        this widget (see _on_gain_setting_changed), so an empty result here
        means "never customized" -- fall back to whatever Rig Settings > SDR
        Settings currently has, purely as a starting point. Later changes to
        that shared setting do not affect this tab once meteor_settings
        exists. Gain here is always manual (see the comment where the
        widget is built), so any leftover gain_auto flag from before that
        change, or from the shared sdr_settings, is ignored.
        """
        meteor = _load_meteor_settings()
        if meteor:
            gain_db = int(meteor.get("gain_db") or 40)
        else:
            sdr = _load_sdr_settings()
            gain_db = int(sdr.get("gain_db") or 40)
        self._gain_spin.blockSignals(True)
        self._gain_spin.setValue(gain_db)
        self._gain_spin.blockSignals(False)

    def _on_gain_setting_changed(self) -> None:
        _save_meteor_settings(
            {
                "gain_auto": False,
                "gain_db": self._gain_spin.value(),
            }
        )

    # ------------------------------------------------------------------
    # SatDump availability check
    # ------------------------------------------------------------------

    def _check_satdump(self) -> None:
        if find_satdump() is None:
            self._banner.setText(
                _(
                    "⚠  SatDump is not installed.  "
                    "Go to Help > SatDump… for installation instructions."
                )
            )
            self._banner.setVisible(True)
            self._btn_start.setEnabled(False)
        else:
            self._banner.setVisible(False)
            self._btn_start.setEnabled(True)

    # ------------------------------------------------------------------
    # Pipeline combo → Radio Control sync
    # ------------------------------------------------------------------

    def _on_pipeline_changed(self, index: int) -> None:
        """Emit satellite_selection_requested so main_window can sync Radio Control."""
        if self._suppress_sync:
            return
        p = self._combo_sat.itemData(index)
        if p:
            self.satellite_selection_requested.emit(int(p["norad"]), int(p["xpdr_freq"]))

    def select_pipeline_by_norad_and_freq(self, norad: int, downlink_hz: int) -> None:
        """Select the combo entry matching *norad* and closest *downlink_hz*.

        Called by main_window when Radio Control selects a METEOR transponder so
        this tab mirrors the selection without triggering a feedback loop.
        """
        best_idx = -1
        best_diff = float("inf")
        for i in range(self._combo_sat.count()):
            p = self._combo_sat.itemData(i)
            if p and int(p["norad"]) == norad:
                diff = abs(int(p["xpdr_freq"]) - downlink_hz)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = i
        if best_idx >= 0 and best_idx != self._combo_sat.currentIndex():
            self._suppress_sync = True
            self._combo_sat.setCurrentIndex(best_idx)
            self._suppress_sync = False

    def current_rx_frequency_mhz(self) -> float | None:
        """Return the selected pipeline's fixed RX frequency in MHz, or None.

        SatDump always tunes to METEOR_PIPELINES' own fixed frequency —
        independent of whatever transponder happens to be selected in Radio
        Control — so the Comms Quick Panel mirrors this instead of Radio
        Control's Doppler-corrected DL/UL (see comms.mode_detection.COMMS_TAB_CONFIG,
        freq_source="satdump").
        """
        p = self._combo_sat.currentData()
        if not p:
            return None
        freq_hz = p.get("frequency")
        return float(freq_hz) / 1e6 if freq_hz else None

    # ------------------------------------------------------------------
    # SDR Connect (reads Rig Settings SDR config)
    # ------------------------------------------------------------------

    def _on_sdr_connect(self) -> None:
        """Check that SDR settings are configured and report to the user."""
        sdr = _load_sdr_settings()
        if not sdr or not sdr.get("enabled"):
            self._lbl_status.setText(
                _("⚠  No SDR configured.  Open Radio > Rig Settings > SDR Settings.")
            )
            return
        driver = _sdr_source_from_settings(sdr)
        label: str = sdr.get("device_label") or driver
        gain = self._gain_spin.value()
        self._lbl_status.setText(
            _("SDR: {label}  gain {gain} dB — ready.").format(label=label, gain=gain)
        )

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        # Resolve SDR source/PPM from Rig Settings > SDR Settings, but gain
        # comes from this tab's own control (see "METEOR-local RF gain"
        # above) -- 137 MHz LRPT reception can need a different gain than
        # whatever the shared SDR setting is tuned for (e.g. FM listening).
        # Always manual: AGC has been confirmed to produce spurious Viterbi
        # "sync" on pure noise for METEOR reception.
        sdr = _load_sdr_settings()
        gain = self._gain_spin.value()
        if sdr and sdr.get("enabled"):
            source = _sdr_source_from_settings(sdr)
            ppm = int(sdr.get("ppm") or 0)
        else:
            # Fallback: try rtlsdr
            source = "rtlsdr"
            ppm = 0
            self._lbl_status.setText(_("⚠  SDR not configured — attempting rtlsdr."))

        # Disconnect SDR if active
        self._disconnect_sdr()

        pipeline_data: dict[str, Any] = self._combo_sat.currentData()

        self._output_dir = _default_output_dir()

        fft_port = find_free_port()
        self._process = SatDumpProcess(
            pipeline=str(pipeline_data["pipeline"]),
            source=source,
            frequency=int(pipeline_data["frequency"]),
            samplerate=int(pipeline_data["samplerate"]),
            output_dir=self._output_dir,
            gain=gain,
            ppm=ppm,
            agc=False,
            fft_http_port=fft_port,
            parent=self,
        )
        self._process.log_line.connect(self._on_log_line)
        self._process.progress.connect(self._on_progress)
        self._process.lock_status.connect(self._on_lock_status)
        self._process.finished_ok.connect(self._on_finished_ok)
        self._process.finished_err.connect(self._on_finished_err)
        self._process.start()

        # Start image watcher
        self._watcher = ImageWatcher(self._output_dir, parent=self)
        self._watcher.new_image.connect(self._on_new_image)
        self._watcher.start()

        # Start FFT waterfall poller and switch to it -- the Image tab
        # would otherwise stay blank/stale for the whole pass (see
        # _preview_tabs' construction comment). A later manual switch back
        # to Image by the user is respected: this is the only place (other
        # than completion, below) that changes the current tab.
        self._waterfall_widget.reset()
        self._fft_poller = SatDumpFftPoller(
            fft_port,
            on_frame=self._fft_frame_received.emit,
            on_unavailable=self._fft_unavailable.emit,
        )
        self._fft_poller.start()
        self._preview_tabs.setCurrentWidget(self._waterfall_widget)

        self._preview_priority = -1
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_sdr_connect.setEnabled(False)
        self._btn_open_past.setEnabled(False)
        self._combo_sat.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._lbl_status.setText(_("Receiving…"))
        self._lbl_lock.setText(_("Lock: —"))

    def _on_stop(self) -> None:
        if self._process is not None:
            self._process.stop()
        # Do NOT stop self._watcher here: SatDump keeps writing PNGs for
        # several more seconds after this point (--finish_processing runs
        # its image-compositing pass on stop), so the watcher must keep
        # polling until the process actually finishes -- see
        # _on_finished_ok()/_on_finished_err().
        self._lbl_status.setText(_("Stopping…"))
        self._btn_stop.setEnabled(False)

    def _disconnect_sdr(self) -> None:
        """Disconnect the SDR and grey out the SDR Control tab."""
        if self._sdr_widget is not None:
            try:
                if hasattr(self._sdr_widget, "disconnect_sdr"):
                    self._sdr_widget.disconnect_sdr()
                elif hasattr(self._sdr_widget, "_on_disconnect"):
                    self._sdr_widget._on_disconnect()
            except Exception:
                pass
        if self._sdr_control_tab is not None:
            self._sdr_control_tab.setEnabled(False)

    def _reenable_sdr_tab(self) -> None:
        if self._sdr_control_tab is not None:
            self._sdr_control_tab.setEnabled(True)

    # ------------------------------------------------------------------
    # Log window
    # ------------------------------------------------------------------

    def _on_show_log(self) -> None:
        if self._log_window is None:
            self._log_window = _LogWindow(self)
            for line in self._log_buffer:
                self._log_window.append(line)
        if self._log_window.isVisible():
            self._log_window.raise_()
            self._log_window.activateWindow()
        else:
            self._log_window.show()

    def _on_help(self) -> None:
        QMessageBox.information(
            self,
            _("SatDump Required"),
            _(
                "To receive satellite images in this tab, SatDump must be installed.\n\n"
                "Please refer to Help → SatDump… in the menu bar for\n"
                "installation instructions."
            ),
        )

    # ------------------------------------------------------------------
    # Process signal handlers
    # ------------------------------------------------------------------

    def _on_log_line(self, line: str) -> None:
        self._log_buffer.append(line)
        if self._log_window is not None:
            self._log_window.append(line)

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)

    def _on_lock_status(self, locked: bool) -> None:
        if locked:
            lock_label = _("Lock!")
            self._lbl_lock.setText(f"<b style='color:#2ecc71'>{lock_label}</b>")
        else:
            self._lbl_lock.setText("<b style='color:#e74c3c'>Lock: ✗</b>")
        self._lbl_lock.setTextFormat(Qt.TextFormat.RichText)

    def _on_finished_ok(self) -> None:
        self._lbl_status.setText(_("Reception finished."))
        self._progress.setVisible(False)
        self._stop_watcher_after_final_poll()
        self._stop_fft_poller()
        self._preview_tabs.setCurrentWidget(self._image_label)
        self._reset_controls()
        self._reenable_sdr_tab()

    def _on_finished_err(self, msg: str) -> None:
        self._lbl_status.setText(_("Error: ") + msg)
        err_line = _("[ERROR] ") + msg
        self._log_buffer.append(err_line)
        if self._log_window is not None:
            self._log_window.append(err_line)
        self._progress.setVisible(False)
        self._stop_watcher_after_final_poll()
        self._stop_fft_poller()
        self._preview_tabs.setCurrentWidget(self._image_label)
        self._reset_controls()
        self._reenable_sdr_tab()

    def _stop_fft_poller(self) -> None:
        """Stop the FFT waterfall poller, if one is running.

        Kept running through _on_stop() (SatDump's own HTTP API stays up
        until the process actually exits, same reasoning as
        _stop_watcher_after_final_poll() for the image watcher) and only
        stopped here, once SatDump has genuinely finished.
        """
        if self._fft_poller is not None:
            self._fft_poller.stop()
            self._fft_poller = None

    def _stop_watcher_after_final_poll(self) -> None:
        """Catch images written between the last timer tick and process exit.

        SatDump's --finish_processing pass writes PNGs right up until the
        process actually terminates, which can land in the gap between the
        watcher's last scheduled poll and this finished signal. Poll once
        more before stopping so nothing written in that window is missed.
        """
        if self._watcher is not None:
            self._watcher.poll_now()
            self._watcher.stop()

    def _reset_controls(self) -> None:
        self._btn_start.setEnabled(find_satdump() is not None)
        self._btn_stop.setEnabled(False)
        self._btn_sdr_connect.setEnabled(True)
        self._btn_open_past.setEnabled(True)
        self._combo_sat.setEnabled(True)
        self._lbl_lock.setText(_("Lock: —"))

    # ------------------------------------------------------------------
    # Autotrack integration (called from main_window)
    # ------------------------------------------------------------------

    def autotrack_start(self, norad: int) -> None:
        """Called by main_window at AOS when METEOR/HRPT reception checkbox is on.

        Selects the first pipeline matching *norad* and starts reception
        if not already running.
        """
        # Select the first pipeline entry matching norad
        for i in range(self._combo_sat.count()):
            p = self._combo_sat.itemData(i)
            if p and int(p["norad"]) == norad:
                self._suppress_sync = True
                self._combo_sat.setCurrentIndex(i)
                self._suppress_sync = False
                break
        if self._process is None or not self._process.isRunning():
            self._on_start()
            self._lbl_status.setText(_("Autotrack: reception started at AOS."))

    def autotrack_stop(self) -> None:
        """Called by main_window at LOS when METEOR/HRPT reception checkbox is on."""
        if self._process is not None and self._process.isRunning():
            self._on_stop()
            self._lbl_status.setText(_("Autotrack: reception stopped at LOS."))

    # ------------------------------------------------------------------
    # Image display
    # ------------------------------------------------------------------

    def _on_new_image(self, path: object) -> None:
        from pathlib import Path as _Path

        p = _Path(str(path))
        image = QImage(str(p))
        if image.isNull():
            return

        label = p.name
        item = _ThumbItem(image, label, path=p)
        self._history_list.addItem(item)

        # A single pass produces roughly a dozen PNGs of varying
        # completeness (see _image_priority docstring). Only promote the
        # main preview when this one is at least as "finished" as whatever
        # is already shown, so e.g. a fully reprojected composite arriving
        # after a plain per-channel image sticks, rather than the preview
        # ending on whichever file the OS/SatDump happened to write last.
        priority = _image_priority(label)
        if priority >= self._preview_priority:
            self._preview_priority = priority
            self._show_image(image)
            self._history_list.setCurrentItem(item)

        self._lbl_status.setText(_("Image received: ") + label)

    def _show_image(self, image: QImage) -> None:
        """Set *image* as the current preview source and (re)display it.

        *image* is always stored unrotated — Flip 180° is applied fresh in
        _rescale_and_display() every time, rather than mutating the stored
        copy, so toggling the button doesn't need to "undo" a previous
        rotation. Any mouse-wheel zoom from a previously-shown image is
        reset -- an extreme zoom level carrying over to an unrelated newly
        selected image would be confusing.
        """
        self._current_original_image = image
        self._zoom_factor = None
        self._rescale_and_display()

    def _apply_rotation(self, image: QImage) -> QImage:
        if not self._image_rotated:
            return image
        return image.transformed(QTransform().rotate(180))

    def _rescale_and_display(self) -> None:
        """Recompute the displayed pixmap from _current_original_image.

        Called whenever the source image, the Flip 180° state, the fit
        mode, the wheel-zoom level, or the viewport size changes (see
        resizeEvent). Fit Width/Fit Height deliberately let the image
        overflow the *other* dimension so the surrounding QScrollArea can
        scroll to it, instead of always squeezing both dimensions to fit
        like the plain "Fit" default. An explicit _zoom_factor (see
        _on_image_wheel) overrides the fit mode entirely until a Fit menu
        item is picked again.
        """
        if self._current_original_image is None:
            return
        image = self._apply_rotation(self._current_original_image)
        iw, ih = image.width(), image.height()
        if iw <= 0 or ih <= 0:
            return
        if self._zoom_factor is not None:
            target_w = max(1, round(iw * self._zoom_factor))
            target_h = max(1, round(ih * self._zoom_factor))
        else:
            viewport = self._image_scroll.viewport().size()
            vw, vh = viewport.width(), viewport.height()
            if vw <= 0 or vh <= 0:
                return
            if self._fit_mode == "width":
                target_w, target_h = vw, max(1, round(ih * vw / iw))
            elif self._fit_mode == "height":
                target_w, target_h = max(1, round(iw * vh / ih)), vh
            else:
                target_w, target_h = vw, vh
        pixmap = QPixmap.fromImage(image).scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(pixmap)
        self._image_label.resize(pixmap.size())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Only re-fit on resize while following the fit mode -- an explicit
        # wheel zoom level is a deliberate user choice that a window resize
        # (or the fit-mode branch's own resize() call above) shouldn't
        # silently discard.
        if self._zoom_factor is None:
            self._rescale_and_display()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._image_label and event.type() == QEvent.Type.Wheel:
            self._on_image_wheel(event)  # type: ignore[arg-type]
            return True
        return super().eventFilter(watched, event)

    def _on_image_wheel(self, event: QWheelEvent) -> None:
        """Mouse-wheel zoom in/out around the currently displayed size.

        Requested by a user (GitHub Issue #27) after trying Flip 180°/Fit
        Width/Fit Height. Consumes the event (see eventFilter) so
        _image_scroll's own default wheel-scroll behavior doesn't also
        fire -- scrolling to see the rest of an overflowing Fit Width/
        Height image still works via its scrollbars.
        """
        if self._current_original_image is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if self._zoom_factor is None:
            # Establish a baseline from whatever is currently on screen
            # rather than jumping to 100%, so the very first wheel tick
            # zooms smoothly from the current Fit view instead of snapping.
            image = self._apply_rotation(self._current_original_image)
            pixmap = self._image_label.pixmap()
            if not pixmap.isNull() and image.width() > 0:
                self._zoom_factor = pixmap.width() / image.width()
            else:
                self._zoom_factor = 1.0
        step = 1.1 if delta > 0 else 1 / 1.1
        self._zoom_factor = max(0.05, min(10.0, self._zoom_factor * step))
        self._rescale_and_display()

    def _on_history_selection(self, current: QListWidgetItem | None, _: Any) -> None:
        self._update_cities_overlay_button_visibility(current)
        if current is None or not isinstance(current, _ThumbItem):
            return
        self._show_image(current.full_image)

    # ------------------------------------------------------------------
    # Right-click: save / Flip 180° / Fit Width / Fit Height
    # ------------------------------------------------------------------
    #
    # These live in the image's context menu rather than as buttons —
    # a third button row alongside Open Folder/Open Past/Clear/Gain
    # didn't fit in one row without crowding it, and eating a whole
    # extra row of vertical space just for occasional display toggles
    # wasn't worth it (see _setup_ui()'s comment where the row was
    # removed).

    def _on_image_context_menu(self, pos: QPoint) -> None:
        if self._current_original_image is None:
            return
        menu, actions = self._build_image_context_menu()
        chosen = menu.exec(self._image_label.mapToGlobal(pos))
        self._handle_image_context_menu_choice(chosen, actions)

    def _build_image_context_menu(self) -> tuple[QMenu, dict[str, QAction]]:
        """Build the right-click menu and return it with its actions keyed
        by role, so _handle_image_context_menu_choice() (and tests) can
        dispatch on the dict without going through the modal menu.exec()
        call itself -- see that method's docstring for why this is split
        out this way.
        """
        menu = QMenu(self)
        actions: dict[str, QAction] = {}
        actions["save"] = menu.addAction(_("💾 Save Image As…"))
        menu.addSeparator()
        act_flip = menu.addAction(_("🔃 Flip 180°"))
        act_flip.setCheckable(True)
        act_flip.setChecked(self._image_rotated)
        actions["flip"] = act_flip
        menu.addSeparator()
        fit_group = QActionGroup(menu)
        fit_group.setExclusive(True)
        act_fit_both = menu.addAction(_("Fit (Both)"))
        act_fit_both.setCheckable(True)
        act_fit_both.setChecked(self._fit_mode == "fit")
        fit_group.addAction(act_fit_both)
        actions["fit_both"] = act_fit_both
        act_fit_width = menu.addAction(_("↔ Fit Width"))
        act_fit_width.setCheckable(True)
        act_fit_width.setChecked(self._fit_mode == "width")
        fit_group.addAction(act_fit_width)
        actions["fit_width"] = act_fit_width
        act_fit_height = menu.addAction(_("↕ Fit Height"))
        act_fit_height.setCheckable(True)
        act_fit_height.setChecked(self._fit_mode == "height")
        fit_group.addAction(act_fit_height)
        actions["fit_height"] = act_fit_height
        return menu, actions

    def _handle_image_context_menu_choice(
        self, chosen: QAction | None, actions: dict[str, QAction]
    ) -> None:
        """Dispatch on which context-menu action was picked.

        Split out from _on_image_context_menu() (rather than inlined after
        menu.exec()) so tests can call _build_image_context_menu() and
        this method directly, without going through the modal exec() call
        itself -- patching QMenu.exec is unreliable for a PySide6 C++-bound
        method and was observed to hang a test run rather than actually
        substituting the fake return value.
        """
        if chosen is None:
            return
        if chosen is actions["save"]:
            self._on_save_image_as()
        elif chosen is actions["flip"]:
            self._image_rotated = not self._image_rotated
            self._rescale_and_display()
        elif chosen is actions["fit_both"]:
            self._fit_mode = "fit"
            self._zoom_factor = None
            self._rescale_and_display()
        elif chosen is actions["fit_width"]:
            self._fit_mode = "width"
            self._zoom_factor = None
            self._rescale_and_display()
        elif chosen is actions["fit_height"]:
            self._fit_mode = "height"
            self._zoom_factor = None
            self._rescale_and_display()

    def _on_save_image_as(self) -> None:
        if self._current_original_image is None:
            return
        image = self._apply_rotation(self._current_original_image)
        current = self._history_list.currentItem()
        default_name = current.text() if isinstance(current, _ThumbItem) else "meteor_image.png"
        # Default to the reception's own folder (e.g.
        # ~/Pictures/fbsat59_meteor/{timestamp}/MSU-MR/) rather than the
        # Desktop, so it lands alongside the rest of that pass's images —
        # requested on GitHub Issue #27 (a saved image on the Desktop was
        # unexpected when everything else SatDump writes goes to Pictures).
        if isinstance(current, _ThumbItem) and current.path is not None:
            default_dir = current.path.parent
        else:
            default_dir = _default_output_dir().parent  # ~/Pictures/fbsat59_meteor
        default_path = str(default_dir / default_name)
        path, _filter = QFileDialog.getSaveFileName(
            self, _("Save Image"), default_path, "PNG (*.png)"
        )
        if not path:
            return
        if not image.save(path):
            QMessageBox.warning(self, _("Save Failed"), _("Could not save the image."))

    # ------------------------------------------------------------------
    # Cities overlay (via SatDump's own "project" CLI tool)
    # ------------------------------------------------------------------

    def _on_add_cities_overlay(self) -> None:
        current = self._history_list.currentItem()
        if not isinstance(current, _ThumbItem) or current.path is None:
            QMessageBox.information(
                self,
                _("Cities Overlay"),
                _("Select a received image from the history list first."),
            )
            return
        product_cbor = find_product_cbor(current.path.parent)
        if product_cbor is None:
            QMessageBox.warning(
                self,
                _("Cities Overlay"),
                _(
                    "Could not find product.cbor for this reception "
                    "— the cities overlay needs it to know where the pass was."
                ),
            )
            return
        if self._cities_overlay_process is not None and self._cities_overlay_process.isRunning():
            return
        output_path = current.path.parent / f"{current.path.stem}_cities.png"
        self._btn_cities_overlay.setEnabled(False)
        self._lbl_status.setText(_("Generating cities overlay…"))
        self._cities_overlay_process = CitiesOverlayProcess(product_cbor, output_path, parent=self)
        self._cities_overlay_process.finished_ok.connect(self._on_cities_overlay_ok)
        self._cities_overlay_process.finished_err.connect(self._on_cities_overlay_err)
        self._cities_overlay_process.start()

    def _on_cities_overlay_ok(self, path: str) -> None:
        self._btn_cities_overlay.setEnabled(True)
        p = Path(path)
        image = QImage(str(p))
        if image.isNull():
            self._lbl_status.setText(_("Cities overlay generated, but the image failed to load."))
            return
        item = _ThumbItem(image, p.name, path=p)
        self._history_list.addItem(item)
        self._history_list.setCurrentItem(item)
        self._show_image(image)
        self._lbl_status.setText(_("Cities overlay generated: ") + p.name)

    def _on_cities_overlay_err(self, msg: str) -> None:
        self._btn_cities_overlay.setEnabled(True)
        self._lbl_status.setText(_("Cities overlay failed."))
        QMessageBox.warning(self, _("Cities Overlay Failed"), msg)

    def _update_cities_overlay_button_visibility(self, current: QListWidgetItem | None) -> None:
        """Hide the button once a cities-overlay image already exists for
        the selected reception, or the selection *is* one — generating it
        again would be redundant (and for the latter, would produce an
        ever-growing "..._cities_cities.png" chain).
        """
        if not isinstance(current, _ThumbItem) or current.path is None:
            self._btn_cities_overlay.setVisible(True)
            return
        if current.path.stem.endswith("_cities"):
            self._btn_cities_overlay.setVisible(False)
            return
        output_path = current.path.parent / f"{current.path.stem}_cities.png"
        self._btn_cities_overlay.setVisible(not output_path.exists())

    # ------------------------------------------------------------------
    # Misc slots
    # ------------------------------------------------------------------

    def _on_open_folder(self) -> None:
        folder = self._output_dir or _default_output_dir().parent.parent
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def _on_open_past(self) -> None:
        base_dir = _default_output_dir().parent  # ~/Pictures/fbsat59_meteor
        base_dir.mkdir(parents=True, exist_ok=True)
        chosen = QFileDialog.getExistingDirectory(
            self,
            _("Open Past Reception"),
            str(base_dir),
        )
        if not chosen:
            return
        self._load_images_from_folder(Path(chosen))

    def _load_images_from_folder(self, folder: Path) -> None:
        """Replace the preview/history with every PNG found under *folder*.

        Used to browse a previous pass's output (see _on_open_past) with
        the same display the tab shows right after a live reception --
        image and priority handling mirror _on_new_image().
        """
        pngs = sorted(folder.rglob("*.png"))
        if not pngs:
            QMessageBox.information(
                self,
                _("No Images Found"),
                _("No PNG images were found in:\n{folder}").format(folder=folder),
            )
            return

        self._history_list.clear()
        self._preview_priority = -1
        best_item: _ThumbItem | None = None
        loaded = 0
        for p in pngs:
            image = QImage(str(p))
            if image.isNull():
                continue
            loaded += 1
            item = _ThumbItem(image, p.name, path=p)
            self._history_list.addItem(item)
            priority = _image_priority(p.name)
            if priority >= self._preview_priority:
                self._preview_priority = priority
                best_item = item

        if best_item is not None:
            self._history_list.setCurrentItem(best_item)

        self._lbl_status.setText(
            _("Loaded {n} image(s) from {folder}").format(n=loaded, folder=folder.name)
        )

    def _on_clear_history(self) -> None:
        self._preview_priority = -1
        self._current_original_image = None
        self._history_list.clear()
        self._image_label.clear()
        self._image_label.setText(_("No image received yet."))
        self._btn_cities_overlay.setVisible(True)

    # ------------------------------------------------------------------
    # Cleanup on tab close
    # ------------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self._on_stop()
        # Wait for satdump to actually exit before this widget (which
        # parents the QThread) is torn down — Qt aborts the app if a
        # QThread is destroyed while still running. Give it a grace period
        # to flush files and release the SDR cleanly, then force-kill so
        # shutdown is never blocked indefinitely.
        if self._process is not None and self._process.isRunning() and not self._process.wait(3000):
            self._process.stop(force=True)
            self._process.wait(2000)
        # CitiesOverlayProcess is a short one-shot subprocess.run() call
        # (see cities_overlay.py) with no cooperative stop() of its own --
        # just wait for it, same QThread-destroyed-while-running hazard as
        # above but with no force-kill path since there's nothing to signal.
        if self._cities_overlay_process is not None and self._cities_overlay_process.isRunning():
            self._cities_overlay_process.wait(5000)
        self._stop_fft_poller()
        self._reenable_sdr_tab()
        if self._log_window is not None:
            self._log_window.destroy()
        super().closeEvent(event)
