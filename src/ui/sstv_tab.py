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

from PySide6.QtCore import QStandardPaths, Qt, QThread, Signal
from PySide6.QtGui import QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.engine import get_aprs_engine, resolve_ax25_modem
from comms.sstv.file_decoder import SOUNDFILE_AVAILABLE, load_audio_mono
from comms.sstv.ssdv import extract_hex_frames, find_ssdv_packet, format_hex_line
from i18n import _

# Thumbnail size for history list
_THUMB_W = 120
_THUMB_H = 90

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine). SSDV
# mode starts AX.25 reception itself (SDR or Rig + Sound Card, like the Telemetry
# tab) and registers as an owner so closing another tab never stops it, and this
# tab closing never stops another tab's reception.
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


class _RawPacketEdit(QPlainTextEdit):
    """The "Raw Packets" text: one frame per line as hex bytes.

    Received frames are appended live; text can also be pasted (from the
    Telemetry tab, SatNOGS Network ...). ``pasted`` is emitted after a paste so
    the tab can rebuild the image from the whole text.
    """

    pasted: Signal = Signal()

    _MAX_LINES = 5000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setMaximumBlockCount(self._MAX_LINES)

    def append_frame(self, frame: bytes) -> None:
        """Add *frame* as a new hex line (keeps the view at the bottom if it was there)."""
        self.appendPlainText(format_hex_line(frame))

    def insertFromMimeData(self, source: Any) -> None:  # noqa: N802
        """Paste *source* as whole lines: one frame per line must survive the paste.

        A plain paste would glue the text onto the line the cursor is in (and a
        paste without a final newline onto the next one), corrupting frames.
        """
        text = source.text() if source.hasText() else ""
        if not text:
            super().insertFromMimeData(source)
            return
        cursor = self.textCursor()
        if cursor.positionInBlock() != 0:
            text = "\n" + text
        if not text.endswith("\n"):
            text += "\n"
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.pasted.emit()


class _FileDecodeWorker(QThread):
    """Feeds a recorded audio file's PCM into a (throwaway) SstvDecoder.

    Runs off the UI thread since loading/resampling a multi-minute recording
    and running SstvDecoder's Hilbert-transform-based sync search over it can
    take a couple of seconds. The decoder itself is constructed on the UI
    thread by the caller (so its signals reach SstvTab's slots via Qt's
    normal auto-queued cross-thread delivery) — this worker only calls into
    it from a background thread, which SstvDecoder.push_samples() already
    documents as being safe to do.
    """

    finished_ok: Signal = Signal()
    failed: Signal = Signal(str)

    def __init__(self, decoder: Any, path: str, target_rate: int) -> None:
        super().__init__()
        self._decoder = decoder
        self._path = path
        self._target_rate = target_rate

    def run(self) -> None:
        try:
            audio = load_audio_mono(self._path, self._target_rate)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self._decoder.start()
        # The whole file is available up front (unlike a live stream), so a
        # single push_samples() call lets SstvDecoder search for sync pulses
        # across the entire recording at once rather than being artificially
        # chunked — chunking would make it re-start line 0 on every chunk
        # boundary instead of finding the one continuous sync train.
        self._decoder.push_samples(audio)
        self._decoder.stop()
        self.finished_ok.emit()


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
        self._ssdv_engine_signals: bool = False  # raw_frame_received connected to us
        self._ssdv_reception: bool = False  # we hold an owner claim on the AX.25 pipeline
        self._ssdv_frames: int = 0  # live frames received this session
        self._ssdv_packets: int = 0  # ... of which contained an SSDV packet
        self._ssdv_pending: QImage | None = None  # latest SSDV image, not yet in the history

        self._rig_connected: bool = False
        self._sdr_connected: bool = False
        self._decoder: Any | None = None  # SstvDecoder instance
        self._ssdv_decoder: Any | None = None  # SsdvDecoder (persistent across reconnects)
        self._audio_active: bool = False  # soundcard RX subscribed via AudioDeviceManager
        self._audio_device: int | None = None  # device used for the active subscription
        self._current_image: QImage | None = None
        self._current_mode: str = "Robot36"
        self._sat_name: str = ""

        # "Decode Recording…" — batch-decodes a saved MP3/WAV via a throwaway
        # SstvDecoder, independent of any live decoder/audio source above.
        self._file_decode_worker: _FileDecodeWorker | None = None
        self._file_decode_sat_name: str = ""

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

        # Left: sub-tabs -- "Raw Packets" (hex, live + paste) and "Image"
        self._view_tabs = QTabWidget()

        self._raw_page = QWidget()
        raw_layout = QVBoxLayout(self._raw_page)
        raw_layout.setContentsMargins(4, 4, 4, 4)
        raw_hint = QLabel(
            _(
                "Received AX.25 frames appear here live, one per line, as hex. Paste hex "
                "from another tab (Telemetry) or from SatNOGS Network to build the image "
                "from it."
            )
        )
        raw_hint.setWordWrap(True)
        raw_hint.setStyleSheet("color: #888;")
        raw_layout.addWidget(raw_hint)
        self._raw_edit = _RawPacketEdit()
        self._raw_edit.setPlaceholderText(_("94 A6 62 B2 9C AA 60 …  (one frame per line)"))
        self._raw_edit.pasted.connect(self._on_hex_pasted)
        raw_layout.addWidget(self._raw_edit, stretch=1)
        raw_buttons = QHBoxLayout()
        self._raw_count_label = QLabel("")
        self._raw_count_label.setStyleSheet("color: #888;")
        raw_buttons.addWidget(self._raw_count_label)
        raw_buttons.addStretch()
        self._decode_hex_btn = QPushButton(_("Build image from hex"))
        self._decode_hex_btn.clicked.connect(self._on_hex_pasted)
        raw_buttons.addWidget(self._decode_hex_btn)
        self._clear_raw_btn = QPushButton(_("Clear packets"))
        self._clear_raw_btn.clicked.connect(self._on_clear_raw)
        raw_buttons.addWidget(self._clear_raw_btn)
        raw_layout.addLayout(raw_buttons)
        self._view_tabs.addTab(self._raw_page, _("Raw Packets"))

        self._image_page = QWidget()
        image_layout = QVBoxLayout(self._image_page)
        image_layout.setContentsMargins(0, 0, 0, 0)
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(320, 240)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image_label.setText(_("Waiting for signal…"))
        self._image_label.setStyleSheet("background: #111; border: 1px solid #444; color: #666;")
        image_layout.addWidget(self._image_label)
        self._view_tabs.addTab(self._image_page, _("Image"))
        self._view_tabs.setCurrentWidget(self._image_page)  # SSTV is the default mode
        splitter.addWidget(self._view_tabs)

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
        # Without word wrap, a long error string (e.g. a sound-card
        # exception message) forces QLabel's minimumSizeHint to fit the
        # whole line, widening the window and blocking shrinking it back.
        self._status_label.setWordWrap(True)
        bottom.addWidget(self._status_label)

        # Fixed height + matching padding so these three differently-worded
        # buttons don't end up with slightly different vertical sizes (Qt's
        # default sizeHint varies a little with text/emoji glyph metrics).
        _bottom_btn_style = "padding: 4px 10px;"

        self._save_btn = QPushButton(_("💾 Save PNG"))
        self._save_btn.setEnabled(False)
        self._save_btn.setFixedHeight(30)
        self._save_btn.setStyleSheet(_bottom_btn_style)
        self._save_btn.clicked.connect(self._on_save_png)
        bottom.addWidget(self._save_btn)

        self._decode_file_btn = QPushButton(_("📂 Decode Recording…"))
        self._decode_file_btn.setEnabled(SOUNDFILE_AVAILABLE)
        self._decode_file_btn.setFixedHeight(30)
        self._decode_file_btn.setStyleSheet(_bottom_btn_style)
        if not SOUNDFILE_AVAILABLE:
            self._decode_file_btn.setToolTip(_("soundfile not installed — pip install soundfile"))
        self._decode_file_btn.clicked.connect(self._on_decode_file)
        bottom.addWidget(self._decode_file_btn)

        self._clear_btn = QPushButton(_("🗑 Clear"))
        self._clear_btn.setFixedHeight(30)
        # The 🗑 glyph itself renders with a taller bounding box than 💾/📂
        # in most emoji fonts (extends further below the text baseline),
        # which visually pushes the whole "🗑 Clear" label upward inside an
        # otherwise identically-padded button — nudge it down slightly to
        # compensate.
        self._clear_btn.setStyleSheet("padding: 7px 10px 1px 10px;")
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
        self._resume_ssdv_reception()

    def _on_rig_disconnected(self) -> None:
        self._disconnect_audio_source()
        self._rig_connected = False
        self._sdr_connected = False
        self._refresh_input_source()
        self._stop_ssdv_reception()

    def _on_sdr_connected(self) -> None:
        self._sdr_connected = True
        self._refresh_input_source()
        if self._decoder is not None and self._mode_combo.currentText() == "SSTV":
            self._connect_audio_source()
        self._resume_ssdv_reception()

    def _on_sdr_disconnected(self) -> None:
        self._disconnect_audio_source()
        self._sdr_connected = False
        self._refresh_input_source()
        self._stop_ssdv_reception()

    def _resume_ssdv_reception(self) -> None:
        """In SSDV mode, (re)start AX.25 reception when an input has just connected."""
        if self._mode_combo.currentText() == "SSDV" and self._ssdv_engine_signals:
            self._start_ssdv_reception()

    def _on_transmitter_changed(self, xpdr: Any) -> None:
        """Update satellite name when transponder selection changes."""
        if xpdr and isinstance(xpdr, dict):
            self._sat_name = xpdr.get("description", "")
        if self._ssdv_reception:
            # a different transponder can mean a different AX.25 baud rate
            modem = resolve_ax25_modem(self._conn, self._radio_control)
            engine = self._engine()
            engine.restart_if_modem_changed(modem)
            pipeline = self._find_sdr_pipeline()
            if pipeline is not None:
                engine.sync_sdr_baud(pipeline, modem, satellite=True)

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

    def _ensure_ssdv_decoder(self) -> Any:
        """The SsdvDecoder, created on first use (it outlives reconnects)."""
        from comms.sstv.ssdv import SsdvDecoder

        if self._ssdv_decoder is None:
            self._ssdv_decoder = SsdvDecoder(parent=self)
            self._ssdv_decoder.image_updated.connect(self._on_ssdv_image)
            self._ssdv_decoder.status_changed.connect(self._status_label.setText)
            self._ssdv_decoder.error_occurred.connect(self._status_label.setText)
        return self._ssdv_decoder

    def _engine(self) -> Any:
        """The process-wide AprsEngine (started/stopped by owner, shared with other tabs)."""
        if self._aprs_engine is None:
            self._aprs_engine = get_aprs_engine(self._conn)
        return self._aprs_engine

    def _start_ssdv(self) -> None:
        """SSDV mode: start AX.25 reception and feed the frames to the SSDV decoder.

        Reception is started here (SDR or Rig + Sound Card, like the Telemetry
        tab) -- no other tab needs to be open. Without an audio source the tab
        still works for pasted hex.
        """
        self._ensure_ssdv_decoder()
        engine = self._engine()
        if not self._ssdv_engine_signals:
            engine.raw_frame_received.connect(self._on_ax25_frame)
            self._ssdv_engine_signals = True
        self._ssdv_frames = 0
        self._ssdv_packets = 0
        self._start_ssdv_reception()

    def _start_ssdv_reception(self) -> None:
        """Claim the AX.25 pipeline for the connected input (no-op without one)."""
        if self._ssdv_reception:
            return
        engine = self._engine()
        modem = resolve_ax25_modem(self._conn, self._radio_control)
        pipeline = self._find_sdr_pipeline()
        if pipeline is not None:
            ok, err = engine.start_sdr_direwolf(
                _ENGINE_OWNER, pipeline, modem=modem, satellite=True
            )
        elif self._rig_connected:
            ok, err = engine.start_rig(_ENGINE_OWNER, "N0CALL", 0, "", modem=modem)
        else:
            self._status_label.setText(
                _("SSDV: no audio source — connect Rig or SDR in Radio Control (pasting hex works)")
            )
            return
        if not ok:
            self._status_label.setText(f"⚠ {err}")
            return
        self._ssdv_reception = True
        self._status_label.setText(_("SSDV: waiting for AX.25 frames…"))

    def _stop_ssdv_reception(self) -> None:
        """Release this tab's claim on the AX.25 pipeline."""
        if self._ssdv_reception:
            self._engine().stop(_ENGINE_OWNER)
            self._ssdv_reception = False

    def _stop_ssdv(self) -> None:
        """Leave SSDV mode: disconnect from the AX.25 pipeline, decode what is buffered."""
        if self._aprs_engine is not None:
            if self._ssdv_engine_signals:
                with contextlib.suppress(RuntimeError, TypeError):
                    self._aprs_engine.raw_frame_received.disconnect(self._on_ax25_frame)
                self._ssdv_engine_signals = False
            self._stop_ssdv_reception()
        if self._ssdv_decoder is not None:
            self._ssdv_decoder.flush()
        self._commit_pending_ssdv()

    def _on_ax25_frame(self, raw: bytes) -> None:
        """A live AX.25 (or bare HDLC) frame: show it as hex, feed any SSDV packet in it."""
        self._raw_edit.append_frame(raw)
        self._ssdv_frames += 1
        packet = find_ssdv_packet(raw)
        if (
            packet is not None
            and self._ssdv_decoder is not None
            and self._ssdv_decoder.push_packet(packet)
        ):
            self._ssdv_packets += 1
        self._update_raw_count()

    def _update_raw_count(self) -> None:
        self._raw_count_label.setText(
            _("Frames: ")
            + str(self._ssdv_frames)
            + "   "
            + _("SSDV packets: ")
            + str(self._ssdv_packets)
        )

    def _on_hex_pasted(self) -> None:
        """Build the image from the whole Raw Packets text (after a paste, or the button)."""
        frames = extract_hex_frames(self._raw_edit.toPlainText())
        if not frames:
            self._status_label.setText(_("No hex frames found in the text."))
            return
        decoder = self._ensure_ssdv_decoder()
        decoder.reset()
        packets = 0
        for frame in frames:
            packet = find_ssdv_packet(frame)
            if packet is not None and decoder.push_packet(packet):
                packets += 1
        if packets == 0:
            self._status_label.setText(
                _(
                    "{n} frames, but none contains an SSDV packet "
                    "(sync 55 66 / 55 67 and a valid CRC)."
                ).format(n=len(frames))
            )
            return
        self._status_label.setText(
            _("{n} frames, {m} SSDV packets — decoding…").format(n=len(frames), m=packets)
        )
        if decoder.decode_now():
            self._commit_pending_ssdv()
            self._view_tabs.setCurrentWidget(self._image_page)

    def _on_clear_raw(self) -> None:
        """Empty the Raw Packets text and forget the buffered SSDV packets."""
        self._raw_edit.clear()
        if self._ssdv_decoder is not None:
            self._ssdv_decoder.reset()
        self._ssdv_frames = 0
        self._ssdv_packets = 0
        self._raw_count_label.setText("")

    def _on_ssdv_image(self, qimg: QImage) -> None:
        """Show the (progressively better) SSDV image; it enters the history on commit."""
        self._ssdv_pending = qimg
        self._current_image = qimg
        self._save_btn.setEnabled(True)
        self._show_scaled(qimg)

    def _commit_pending_ssdv(self) -> None:
        """Put the latest SSDV image into the history / DB / auto-save, once."""
        if self._ssdv_pending is not None:
            qimg, self._ssdv_pending = self._ssdv_pending, None
            self._on_image_complete(qimg, "SSDV")

    def _show_scaled(self, qimg: QImage) -> None:
        self._image_label.setPixmap(
            QPixmap.fromImage(qimg).scaled(
                self._image_label.width(),
                self._image_label.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

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
        """Store completed image in history and optionally auto-save (live decode)."""
        self._record_completed_image(qimg, mode)

    def _on_file_image_complete(self, qimg: QImage, mode: str) -> None:
        """Same as _on_image_complete, but for a "Decode Recording…" result.

        Labeled/saved under the source recording's filename stem instead of
        self._sat_name — a file being decoded may well be from a different
        satellite/pass than whatever is currently selected in Radio Control.
        """
        self._record_completed_image(qimg, mode, sat_name_override=self._file_decode_sat_name)

    def _record_completed_image(
        self, qimg: QImage, mode: str, sat_name_override: str | None = None
    ) -> None:
        self._current_image = qimg
        self._save_btn.setEnabled(True)
        self._current_mode = mode
        sat_name = sat_name_override if sat_name_override is not None else self._sat_name

        now = datetime.now(UTC)
        label = f"{sat_name or 'SSTV'}\n{now.strftime('%H:%M UTC')}"
        item = _ThumbnailItem(qimg, label)
        self._history_list.addItem(item)

        self._persist_to_db(qimg, mode, now, sat_name=sat_name)

        if self._auto_save_cb.isChecked():
            self._auto_save_image(qimg, mode, now, sat_name=sat_name)

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
            self._view_tabs.setCurrentWidget(self._image_page)
            self._start_decoder()
        else:
            self._view_tabs.setCurrentWidget(self._raw_page)
            self._start_ssdv()

    def _on_decode_file(self) -> None:
        """Batch-decode an SSTV image from a previously recorded MP3/WAV file."""
        if self._file_decode_worker is not None:
            return
        start_dir = str(Path.home() / "audio_recordings")
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Select Recorded Audio"),
            start_dir,
            _("Audio Files (*.mp3 *.wav)"),
        )
        if not path:
            return
        if not SOUNDFILE_AVAILABLE:
            QMessageBox.warning(
                self,
                _("Decode Recording"),
                _("soundfile not installed — pip install soundfile"),
            )
            return

        from comms.sstv.decoder import SstvDecoder

        self._file_decode_sat_name = Path(path).stem
        decoder = SstvDecoder(sample_rate=44100, parent=self)
        decoder.line_received.connect(self._on_line_received)
        decoder.image_complete.connect(self._on_file_image_complete)
        decoder.mode_detected.connect(self._on_mode_detected)
        decoder.status_changed.connect(self._status_label.setText)

        self._decode_file_btn.setEnabled(False)
        self._status_label.setText(_("Decoding: ") + Path(path).name)

        self._file_decode_worker = _FileDecodeWorker(decoder, path, 44100)
        self._file_decode_worker.finished_ok.connect(self._on_file_decode_finished)
        self._file_decode_worker.failed.connect(self._on_file_decode_failed)
        self._file_decode_worker.start()

    def _on_file_decode_finished(self) -> None:
        self._decode_file_btn.setEnabled(SOUNDFILE_AVAILABLE)
        self._file_decode_worker = None

    def _on_file_decode_failed(self, message: str) -> None:
        QMessageBox.warning(self, _("Decode Recording"), message)
        self._status_label.setText(_("Decode failed"))
        self._decode_file_btn.setEnabled(SOUNDFILE_AVAILABLE)
        self._file_decode_worker = None

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
        self._ssdv_pending = None
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

    def _persist_to_db(
        self, qimg: QImage, mode: str, ts: datetime, sat_name: str | None = None
    ) -> None:
        if not hasattr(self._conn, "execute"):
            return
        name = sat_name if sat_name is not None else self._sat_name
        file_path = self._auto_save_image(qimg, mode, ts, sat_name=name) if True else None
        self._conn.execute(
            """
            INSERT INTO sstv_log (received_at, mode, file_path, callsign)
            VALUES (?, ?, ?, ?)
            """,
            (ts.isoformat(), mode, file_path, name or None),
        )
        self._conn.commit()

    def _auto_save_image(
        self, qimg: QImage, mode: str, ts: datetime, sat_name: str | None = None
    ) -> str | None:
        """Save image to the user Pictures directory. Returns saved path or None."""
        name = sat_name if sat_name is not None else self._sat_name
        pics = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        save_dir = Path(pics) / "GPredict-SSTV"
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"SSTV_{name or 'image'}_{ts.strftime('%Y%m%d_%H%M%S')}.png"
        path = str(save_dir / filename)
        qimg.save(path)
        return path

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._stop_decoder()  # also calls _disconnect_audio_source -> _stop_soundcard_capture
        self._stop_ssdv()
        if self._file_decode_worker is not None:
            # Batch file-decode is a short, self-contained CPU job (no rig/
            # subprocess/hardware to release) — just wait for it rather than
            # tearing it down mid-run, same reasoning as METEOR/HRPT's
            # SatDumpProcess.stop() waiting instead of destroying a running
            # QThread out from under itself.
            self._file_decode_worker.wait(5000)
            self._file_decode_worker = None
        super().closeEvent(event)
