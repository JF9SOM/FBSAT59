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

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from comms.meteor.image_watcher import ImageWatcher
from comms.meteor.satdump import METEOR_PIPELINES, SatDumpProcess, find_satdump
from i18n import _

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
    def __init__(self, image: QImage, label: str) -> None:
        super().__init__()
        self.full_image = image.copy()
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
        self._output_dir: Path | None = None
        self._preview_priority: int = -1  # see _image_priority()
        self._suppress_sync: bool = False  # prevents feedback loop during Radio Control sync
        self._log_window: _LogWindow | None = None
        self._log_buffer: deque[str] = deque(maxlen=2000)
        self._setup_ui()
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
        image_layout.setSpacing(3)
        self._image_label = QLabel(_("No image received yet."))
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(300, 200)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image_label.setStyleSheet("border: 1px solid #555; background: #111;")
        image_layout.addWidget(self._image_label, 1)

        btn_row2 = QHBoxLayout()
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

        history_widget = QWidget()
        hl = QVBoxLayout(history_widget)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(QLabel(_("Received Images:")))
        self._history_list = QListWidget()
        self._history_list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self._history_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._history_list.currentItemChanged.connect(self._on_history_selection)
        hl.addWidget(self._history_list)
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

        self._process = SatDumpProcess(
            pipeline=str(pipeline_data["pipeline"]),
            source=source,
            frequency=int(pipeline_data["frequency"]),
            samplerate=int(pipeline_data["samplerate"]),
            output_dir=self._output_dir,
            gain=gain,
            ppm=ppm,
            agc=False,
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
        self._reset_controls()
        self._reenable_sdr_tab()

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
        item = _ThumbItem(image, label)
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
        w = self._image_label.width()
        h = self._image_label.height()
        pixmap = QPixmap.fromImage(image).scaled(
            w,
            h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(pixmap)

    def _on_history_selection(self, current: QListWidgetItem | None, _: Any) -> None:
        if current is None or not isinstance(current, _ThumbItem):
            return
        self._show_image(current.full_image)

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
            item = _ThumbItem(image, p.name)
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
        self._history_list.clear()
        self._image_label.clear()
        self._image_label.setText(_("No image received yet."))

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
        self._reenable_sdr_tab()
        if self._log_window is not None:
            self._log_window.destroy()
        super().closeEvent(event)
