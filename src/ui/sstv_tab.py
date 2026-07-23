"""SSTV / SSDV tab widget — Communications > SSTV / SSDV.

Receives SSTV (analog) or SSDV (digital) images from amateur satellites.

Input sources:
  - SDR connected  → SDR audio output → SstvDecoder (Python)
  - Rig connected  → Sound Card → SstvDecoder (Python)
  - Neither        → shows a "no audio source" notice

Decoded images are displayed progressively (line by line for SSTV) and
saved as PNG files either manually or automatically.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from i18n import _

# Thumbnail size for history list
_THUMB_W = 120
_THUMB_H = 90

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine).
# SSDV mode only taps raw_frame_received off whatever the APRS/Telemetry
# tabs already started (it never starts the pipeline itself), but must
# still register as an owner so closing the APRS tab doesn't silently stop
# reception here too.
_ENGINE_OWNER = "sstv"


class _ThumbnailItem(QListWidgetItem):
    """List item that stores a full-resolution QImage alongside its thumbnail."""

    def __init__(self, image: QImage, label: str) -> None:
        super().__init__()
        self.full_image: QImage = image.copy()
        thumb = image.scaled(
            _THUMB_W,
            _THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        from PySide6.QtGui import QIcon

        self.setIcon(QIcon(QPixmap.fromImage(thumb)))
        self.setText(label)
        self.setSizeHint(
            __import__("PySide6.QtCore", fromlist=["QSize"]).QSize(_THUMB_W + 8, _THUMB_H + 24)
        )


class SstvTab(QWidget):
    """Non-resident tab opened from Communications > SSTV / SSDV.

    Received images are stored in the ``sstv_log`` SQLite table and optionally
    saved automatically to the user's Pictures folder.
    """

    def __init__(
        self,
        conn: Any,
        radio_control: QWidget,
        aprs_engine: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control
        self._aprs_engine: Any | None = aprs_engine

        self._rig_connected: bool = False
        self._sdr_connected: bool = False
        self._decoder: Any | None = None  # SstvDecoder instance
        self._ssdv_decoder: Any | None = None  # SsdvDecoder (persistent across reconnects)
        self._audio_active: bool = False  # soundcard RX subscribed via AudioDeviceManager
        self._audio_device: int | None = None  # device used for the active subscription
        self._current_image: QImage | None = None
        self._current_mode: str = "Robot36"
        self._sat_name: str = ""

        self._ensure_db_table()
        self._setup_ui()
        self._wire_radio_signals()
        self._refresh_input_source()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── top bar ──────────────────────────────────────────────────────
        top = QHBoxLayout()

        top.addWidget(QLabel(_("Mode:")))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["SSTV", "SSDV"])
        self._mode_combo.setFixedWidth(90)
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        top.addWidget(self._mode_combo)

        top.addSpacing(16)
        self._source_label = QLabel(_("Input: —"))
        self._source_label.setStyleSheet("color: gray;")
        top.addWidget(self._source_label)

        _sstv_help = QLabel(" ? ")
        _sstv_help.setStyleSheet(
            "color:white;background:#2980b9;border-radius:8px;font-weight:bold;padding:2px 6px;"
        )
        _sstv_help.setToolTip(
            "SSTV / SSDV is available from:\n"
            "  • ISS (NORAD 25544)  145.800 MHz FM  — PD120  (Mode V)\n"
            "  • ISS (NORAD 25544)  437.550 MHz FM  — Robot36  (Mode U)\n"
            "    (Events are announced at https://ariss.psnc.pl)\n"
            "  • IO-86 (NORAD 39444)  435.880 MHz FM\n"
            "    (Occasional SSDV experiments)\n\n"
            "Select the satellite in Radio Control to get started."
        )
        top.addWidget(_sstv_help)

        top.addStretch()

        self._auto_save_cb = QCheckBox(_("Auto-save PNG"))
        self._auto_save_cb.setChecked(True)
        top.addWidget(self._auto_save_cb)

        root.addLayout(top)

        # ── main splitter: image | history ───────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: live image display
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(320, 240)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image_label.setStyleSheet("background: #111; border: 1px solid #444;")
        self._image_label.setText(_("Waiting for signal…"))
        self._image_label.setStyleSheet("background: #111; border: 1px solid #444; color: #666;")
        left_layout.addWidget(self._image_label)
        splitter.addWidget(left_widget)

        # Right: received image history
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel(_("Received Images:")))

        self._history_list = QListWidget()
        self._history_list.setIconSize(
            __import__("PySide6.QtCore", fromlist=["QSize"]).QSize(_THUMB_W, _THUMB_H)
        )
        self._history_list.setViewMode(QListWidget.ViewMode.IconMode)
        self._history_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._history_list.setMovement(QListWidget.Movement.Static)
        self._history_list.setMinimumWidth(140)
        self._history_list.itemClicked.connect(self._on_history_clicked)
        right_layout.addWidget(self._history_list)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        # ── bottom bar ───────────────────────────────────────────────────
        bottom = QHBoxLayout()

        self._status_label = QLabel(_("Ready"))
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bottom.addWidget(self._status_label)

        self._save_btn = QPushButton(_("💾 Save PNG"))
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save_png)
        bottom.addWidget(self._save_btn)

        self._clear_btn = QPushButton(_("🗑 Clear"))
        self._clear_btn.clicked.connect(self._on_clear)
        bottom.addWidget(self._clear_btn)

        root.addLayout(bottom)

    # ------------------------------------------------------------------ #
    # Signal wiring
    # ------------------------------------------------------------------ #

    def _wire_radio_signals(self) -> None:
        rc = self._radio_control
        if rc is None:
            return
        if hasattr(rc, "rig_connected"):
            rc.rig_connected.connect(self._on_rig_connected)
        if hasattr(rc, "rig_disconnected"):
            rc.rig_disconnected.connect(self._on_rig_disconnected)
        if hasattr(rc, "rig2_connected"):
            rc.rig2_connected.connect(self._on_rig_connected)
        if hasattr(rc, "rig2_disconnected"):
            rc.rig2_disconnected.connect(self._on_rig_disconnected)
        if hasattr(rc, "sdr_connected"):
            rc.sdr_connected.connect(self._on_sdr_connected)
        if hasattr(rc, "sdr_disconnected"):
            rc.sdr_disconnected.connect(self._on_sdr_disconnected)
        if hasattr(rc, "transmitter_changed"):
            rc.transmitter_changed.connect(self._on_transmitter_changed)

    def _on_rig_connected(self) -> None:
        rc = self._radio_control
        for attr in ("_rig1", "_rig2"):
            rig = getattr(rc, attr, None)
            if rig is not None and getattr(rig, "is_sdr", False):
                self._sdr_connected = True
                break
        else:
            self._rig_connected = True
        self._refresh_input_source()
        if self._decoder is not None and self._mode_combo.currentText() == "SSTV":
            self._connect_audio_source()

    def _on_rig_disconnected(self) -> None:
        self._disconnect_audio_source()
        self._rig_connected = False
        self._sdr_connected = False
        self._refresh_input_source()

    def _on_sdr_connected(self) -> None:
        self._sdr_connected = True
        self._refresh_input_source()
        if self._decoder is not None and self._mode_combo.currentText() == "SSTV":
            self._connect_audio_source()

    def _on_sdr_disconnected(self) -> None:
        self._disconnect_audio_source()
        self._sdr_connected = False
        self._refresh_input_source()

    def _on_transmitter_changed(self, xpdr: Any) -> None:
        """Update satellite name when transponder selection changes."""
        if xpdr and isinstance(xpdr, dict):
            self._sat_name = xpdr.get("description", "")

    def _refresh_input_source(self) -> None:
        if self._sdr_connected:
            src = _("SDR (receive only)")
            self._source_label.setStyleSheet("color: #00bcd4;")
        elif self._rig_connected:
            src = _("Sound Card")
            self._source_label.setStyleSheet("color: #4caf50;")
        else:
            src = _("No audio source — connect Rig or SDR in Radio Control")
            self._source_label.setStyleSheet("color: #f44336;")
        self._source_label.setText(_("Input: ") + src)

    # ------------------------------------------------------------------ #
    # Decoder management
    # ------------------------------------------------------------------ #

    def _start_decoder(self) -> None:
        """Instantiate and start SstvDecoder connected to the audio source."""
        if self._decoder is not None:
            return
        from comms.sstv.decoder import SstvDecoder

        self._decoder = SstvDecoder(sample_rate=44100, parent=self)
        self._decoder.line_received.connect(self._on_line_received)
        self._decoder.image_complete.connect(self._on_image_complete)
        self._decoder.mode_detected.connect(self._on_mode_detected)
        self._decoder.status_changed.connect(self._status_label.setText)
        self._decoder.start()
        self._connect_audio_source()
        self._status_label.setText(_("Decoder started — listening for sync…"))

    def _stop_decoder(self) -> None:
        self._disconnect_audio_source()
        if self._decoder is not None:
            self._decoder.stop()
            self._decoder = None

    def _connect_audio_source(self) -> None:
        """Connect the current audio source to the active SSTV decoder."""
        if self._decoder is None:
            return
        pipeline = self._find_sdr_pipeline()
        if pipeline is not None:
            with contextlib.suppress(RuntimeError):
                pipeline.audio_ready.connect(self._decoder.push_samples)
            # Without this, the pipeline never actually demodulates/emits
            # audio_ready unless the user separately presses "Start Audio"
            # in SDR Control — an easy-to-miss, unrelated-looking button in
            # a different tab (GitHub Issue #12 follow-up).
            with contextlib.suppress(Exception):
                pipeline.request_audio(self._AUDIO_OWNER)
        elif self._rig_connected:
            self._start_soundcard_capture()

    def _disconnect_audio_source(self) -> None:
        """Disconnect all audio sources from the decoder."""
        pipeline = self._find_sdr_pipeline()
        if pipeline is not None and self._decoder is not None:
            with contextlib.suppress(RuntimeError):
                pipeline.audio_ready.disconnect(self._decoder.push_samples)
            with contextlib.suppress(Exception):
                pipeline.release_audio(self._AUDIO_OWNER)
        self._stop_soundcard_capture()

    def _find_sdr_pipeline(self) -> Any | None:
        """Return the first SDR pipeline found in Rig 1 or Rig 2 slots."""
        rc = self._radio_control
        for attr in ("_rig1", "_rig2"):
            rig = getattr(rc, attr, None)
            if rig is not None and getattr(rig, "is_sdr", False):
                return getattr(rig, "_pipeline", None)
        return None

    _AUDIO_OWNER = "SSTV/SSDV"
    _SOUNDCARD_SAMPLE_RATE = 44100

    def _start_soundcard_capture(self) -> None:
        """Subscribe to shared soundcard RX audio and feed it to the SSTV decoder."""
        if self._audio_active:
            return
        try:
            import sounddevice as sd  # noqa: F401 — validate availability
        except ImportError:
            self._status_label.setText(_("sounddevice not installed — pip install sounddevice"))
            return
        in_idx = self._load_soundcard_input_device()
        decoder = self._decoder

        def _callback(chunk: Any) -> None:
            if decoder is not None:
                decoder.push_samples(chunk)

        try:
            from comms.audio_device_manager import get_audio_device_manager

            get_audio_device_manager().acquire_input(
                self._AUDIO_OWNER, in_idx, self._SOUNDCARD_SAMPLE_RATE, _callback
            )
            self._audio_active = True
            self._audio_device = in_idx
        except Exception as exc:
            self._status_label.setText(_("Sound card error: ") + str(exc))

    def _stop_soundcard_capture(self) -> None:
        if self._audio_active:
            from comms.audio_device_manager import get_audio_device_manager

            get_audio_device_manager().release_input(self._AUDIO_OWNER, self._audio_device)
            self._audio_active = False

    def _load_soundcard_input_device(self) -> int | None:
        """Read the configured soundcard input device index from app_settings."""
        try:
            import json

            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
            ).fetchone()
            if row:
                data = json.loads(row[0])
                val = data.get("input_device_index")
                return int(val) if val is not None else None
        except Exception:
            pass
        return None

    def _start_ssdv(self) -> None:
        """Create SsdvDecoder and subscribe to the APRS engine's raw AX.25 frames."""
        from comms.sstv.ssdv import SsdvDecoder

        if self._ssdv_decoder is None:
            self._ssdv_decoder = SsdvDecoder(parent=self)
            self._ssdv_decoder.image_updated.connect(self._on_ssdv_image)
            self._ssdv_decoder.status_changed.connect(self._status_label.setText)
            self._ssdv_decoder.error_occurred.connect(self._status_label.setText)

        if self._aprs_engine is not None:
            self._aprs_engine.add_owner(_ENGINE_OWNER)
            self._aprs_engine.raw_frame_received.connect(self._on_ax25_frame)
            self._status_label.setText(_("SSDV: waiting for AX.25 frames…"))
        else:
            self._status_label.setText(_("SSDV: open APRS tab first to enable AX.25 reception"))

    def _stop_ssdv(self) -> None:
        """Disconnect from the AX.25 pipeline and flush any buffered packets."""
        if self._aprs_engine is not None:
            with contextlib.suppress(RuntimeError):
                self._aprs_engine.raw_frame_received.disconnect(self._on_ax25_frame)
            self._aprs_engine.stop(_ENGINE_OWNER)
        if self._ssdv_decoder is not None:
            self._ssdv_decoder.flush()

    def _on_ax25_frame(self, raw: bytes) -> None:
        """Filter raw AX.25 frames for SSDV packets and feed the decoder."""
        # SSDV packets start with sync byte 0x55 followed by type 0x66
        if len(raw) >= 2 and raw[0] == 0x55 and raw[1] == 0x66 and self._ssdv_decoder is not None:
            self._ssdv_decoder.push_packet(raw)

    def _on_ssdv_image(self, qimg: QImage) -> None:
        """Handle a completed SSDV image (same flow as SSTV image_complete)."""
        self._on_image_complete(qimg, "SSDV")

    # ------------------------------------------------------------------ #
    # Decoder signal handlers
    # ------------------------------------------------------------------ #

    def _on_line_received(self, line: int, qimg: QImage) -> None:
        """Update the live image display progressively."""
        self._current_image = qimg
        pix = QPixmap.fromImage(qimg).scaled(
            self._image_label.width(),
            self._image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self._image_label.setPixmap(pix)

    def _on_image_complete(self, qimg: QImage, mode: str) -> None:
        """Store completed image in history and optionally auto-save."""
        self._current_image = qimg
        self._save_btn.setEnabled(True)
        self._current_mode = mode

        now = datetime.now(UTC)
        label = f"{self._sat_name or 'SSTV'}\n{now.strftime('%H:%M UTC')}"
        item = _ThumbnailItem(qimg, label)
        self._history_list.addItem(item)

        self._persist_to_db(qimg, mode, now)

        if self._auto_save_cb.isChecked():
            self._auto_save_image(qimg, mode, now)

        self._status_label.setText(_("Image received: ") + f"{mode} {now.strftime('%H:%M:%S UTC')}")

    def _on_mode_detected(self, mode: str) -> None:
        self._status_label.setText(_("Mode detected: ") + mode)
        idx = self._mode_combo.findText("SSTV")
        if idx >= 0:
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(idx)
            self._mode_combo.blockSignals(False)

    # ------------------------------------------------------------------ #
    # User actions
    # ------------------------------------------------------------------ #

    def _on_mode_changed(self, mode_text: str) -> None:
        """Switch between SSTV and SSDV decoder."""
        self._stop_decoder()
        self._stop_ssdv()
        if mode_text == "SSTV":
            self._start_decoder()
        else:
            self._start_ssdv()

    def _on_history_clicked(self, item: QListWidgetItem) -> None:
        """Show clicked thumbnail at full size in the main view."""
        if not isinstance(item, _ThumbnailItem):
            return
        self._current_image = item.full_image
        self._save_btn.setEnabled(True)
        pix = QPixmap.fromImage(item.full_image).scaled(
            self._image_label.width(),
            self._image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(pix)

    def _on_save_png(self) -> None:
        """Save current image to a user-selected PNG file."""
        if self._current_image is None:
            return
        now = datetime.now(UTC)
        default_name = f"SSTV_{self._sat_name or 'image'}_{now.strftime('%Y%m%d_%H%M%S')}.png"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Save SSTV Image"),
            default_name,
            _("PNG Images (*.png)"),
        )
        if path:
            self._current_image.save(path)
            self._status_label.setText(_("Saved: ") + os.path.basename(path))

    def _on_clear(self) -> None:
        """Clear the live image display."""
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(_("Waiting for signal…"))
        self._current_image = None
        self._save_btn.setEnabled(False)
        self._status_label.setText(_("Ready"))

    # ------------------------------------------------------------------ #
    # Persistence helpers
    # ------------------------------------------------------------------ #

    def _ensure_db_table(self) -> None:
        if not hasattr(self._conn, "execute"):
            return
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sstv_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at  DATETIME NOT NULL,
                norad_sat    INTEGER,
                mode         TEXT NOT NULL,
                file_path    TEXT,
                callsign     TEXT
            )
            """
        )
        self._conn.commit()

    def _persist_to_db(self, qimg: QImage, mode: str, ts: datetime) -> None:
        if not hasattr(self._conn, "execute"):
            return
        file_path = self._auto_save_image(qimg, mode, ts) if True else None
        self._conn.execute(
            """
            INSERT INTO sstv_log (received_at, mode, file_path, callsign)
            VALUES (?, ?, ?, ?)
            """,
            (ts.isoformat(), mode, file_path, self._sat_name or None),
        )
        self._conn.commit()

    def _auto_save_image(self, qimg: QImage, mode: str, ts: datetime) -> str | None:
        """Save image to the user Pictures directory. Returns saved path or None."""
        pics = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        save_dir = Path(pics) / "GPredict-SSTV"
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"SSTV_{self._sat_name or 'image'}_{ts.strftime('%Y%m%d_%H%M%S')}.png"
        path = str(save_dir / filename)
        qimg.save(path)
        return path

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._stop_decoder()  # also calls _disconnect_audio_source -> _stop_soundcard_capture
        self._stop_ssdv()
        super().closeEvent(event)
