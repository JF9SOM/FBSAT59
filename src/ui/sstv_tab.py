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
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.engine import (
    AX25_BAUD_MODE_CHOICES,
    AX25_BAUD_SETTING_KEY,
    detect_modem_for_transmitter,
    get_aprs_engine,
    resolve_ax25_modem,
)
from comms.sstv.csv_import import CsvExtract, extract_from_csv
from comms.sstv.file_decoder import SOUNDFILE_AVAILABLE, load_audio_mono
from comms.sstv.packet_store import (
    KIND_DSLWP,
    KIND_STANDARD,
    SsdvPacketStore,
    validate_packet,
)
from comms.sstv.sdr_demod import SstvSdrDemod, output_rate_for
from comms.sstv.ssdv import (
    extract_hex_frames,
    find_dslwp_packet,
    find_ssdv_packet,
    format_hex_line,
)
from i18n import _
from ui.sat_search_dialog import SatSearchDialog

# Thumbnail size for history list
_THUMB_W = 120
_THUMB_H = 90

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine). SSDV
# mode starts AX.25 reception itself (SDR or Rig + Sound Card, like the Telemetry
# tab) and registers as an owner so closing another tab never stops it, and this
# tab closing never stops another tab's reception.
_ENGINE_OWNER = "sstv"

# Third mode: ASRTU-1 (AO-123) SSDV, a CCSDS 9k6 BPSK downlink that only gr-satellites
# can demodulate (see comms.sstv.ssdv, "CCSDS / DSLWP path"). The mode runs
# gr_satellites on the SDR's IQ and decodes the VC1 frames it outputs.
_MODE_SSTV = "SSTV"
_MODE_SSDV = "SSDV (AX.25)"
_MODE_GR_SSDV = "SSDV (CCSDS BPSK 9k6)"
# Fourth mode: bare 800 baud 2FSK SSDV (HADES-SA), demodulated by comms.sstv.fsk_ssdv.
_MODE_FSK_SSDV = "SSDV (2FSK 800bps)"
_FAST_DECODE_MODES = (_MODE_GR_SSDV, _MODE_FSK_SSDV)
_FSK_SSDV_NORAD = 68446  # HADES-SA
_GR_SSDV_NORAD = 61781  # ASRTU-1 (AO-123)
# Satellites always offered in the satellite combo: their SSDV downlinks are not (or not
# reliably) described as SSDV in the SatNOGS transmitter list the combo is built from.
_ALWAYS_LISTED_NORADS = (_FSK_SSDV_NORAD, _GR_SSDV_NORAD)
# app_settings key of the satellite picked last (restored at the next launch)
_LAST_SAT_KEY = "sstv_last_satellite"
# app_settings key of the Speed choice (Normal / Fast) for IQ recordings
_DECODE_SPEED_KEY = "sstv_decode_speed"


logger = logging.getLogger(__name__)


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


class _StatusSink(QLabel):
    """The tab's status line: no longer shown (the bottom bar needs the room), but the code
    still reports through it. Warnings and errors (text starting with a warning sign) go to
    the main window's status bar so they are not lost."""

    def setText(self, text: str) -> None:
        super().setText(text)
        if text.startswith("⚠"):
            window = self.window()
            status_bar = getattr(window, "statusBar", None)
            if callable(status_bar):
                bar = status_bar()
                if bar is not None:
                    bar.showMessage(text, 10_000)


class _CsvExtractWorker(QThread):
    """Reads a SatNOGS frames CSV off the GUI thread (CRC / Reed-Solomon checks take a while)."""

    done: Signal = Signal(object)
    failed: Signal = Signal(str)

    def __init__(self, path: str, default_norad: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._path = path
        self._norad = default_norad

    def run(self) -> None:
        try:
            self.done.emit(extract_from_csv(self._path, self._norad))
        except Exception as exc:
            self.failed.emit(str(exc))


class SstvTab(QWidget):
    """Non-resident tab opened from Communications > SSTV / SSDV.

    Received images are stored in the ``sstv_log`` SQLite table and optionally
    saved automatically to the user's Pictures folder.
    """

    # Emitted when the user picks a satellite in the satellite combo.
    satellite_selected = Signal(int)

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
        self._sdr_demod: SstvSdrDemod | None = None  # private FM demodulator on the SDR's I/Q
        self._sdr_demod_pipeline: Any | None = None  # the pipeline it is subscribed to
        self._decoder: Any | None = None  # SstvDecoder instance
        self._ssdv_decoder: Any | None = None  # SsdvDecoder (persistent across reconnects)
        self._gr_ssdv_decoder: Any | None = None  # SsdvDecoder(dslwp=True) for the gr mode
        self._gr_backend: Any | None = None  # GrSatellitesBackend (gr mode only)
        self._fsk_demod: Any | None = None  # FskSsdvSdrDemod (2FSK mode only)
        self._fsk_demod_pipeline: Any | None = None  # the pipeline it is subscribed to
        # Every valid SSDV packet of every mode is kept in the database, so a picture can
        # be built from several passes (comms.sstv.packet_store).
        self._store: SsdvPacketStore | None = (
            SsdvPacketStore(conn) if hasattr(conn, "execute") else None
        )
        self._norad: int = 0  # satellite of the selected transmitter (0: unknown)
        self._csv_worker: _CsvExtractWorker | None = None
        self._fast_job: Any | None = None  # IQ-file decode worker (Speed "Fast")
        self._active_group: dict[str, int] = {}  # packet kind -> group the decoder holds
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
        # SSTV is the mode shown at start: listen right away (it used to start only after
        # switching to SSDV and back, so a freshly opened tab never received anything)
        self._start_decoder()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── input source notice (own row above the top bar) ──────────────
        self._source_label = QLabel(_("Input: —"))
        self._source_label.setStyleSheet("color: gray;")
        # Wrap a long message instead of widening the whole window.
        self._source_label.setWordWrap(True)
        root.addWidget(self._source_label)

        # ── top bar ──────────────────────────────────────────────────────
        top = QHBoxLayout()

        top.addWidget(QLabel(_("Mode:")))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems([_MODE_SSTV, _MODE_SSDV, _MODE_GR_SSDV, _MODE_FSK_SSDV])
        self._mode_combo.setFixedWidth(170)
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        top.addWidget(self._mode_combo)

        # Satellite picker (same idea as the Telemetry tab's): picking one selects it in
        # the satellite list and Radio Control, so it is clear who sends SSTV / SSDV.
        top.addSpacing(8)
        self._sat_combo = QComboBox()
        self._sat_combo.setMinimumWidth(280)
        self._sat_combo.setToolTip(
            _("Satellites that send SSTV / SSDV. Selecting one also selects it in Radio Control.")
        )
        self._populate_sat_combo()
        QTimer.singleShot(0, self._announce_restored_satellite)
        self._sat_combo.currentIndexChanged.connect(self._on_sat_changed)
        top.addWidget(self._sat_combo)
        self._sat_search_btn = QPushButton("🔍")
        self._sat_search_btn.setToolTip(_("Search satellites…"))
        self._sat_search_btn.setFixedWidth(28)
        self._sat_search_btn.clicked.connect(self._on_sat_search_clicked)
        top.addWidget(self._sat_search_btn)

        # AX.25 baud for the SSDV (AX.25) mode only. Shares the APRS tab's
        # stored setting (ax25_baud_mode), so both tabs stay in step.
        top.addSpacing(8)
        top.addWidget(QLabel(_("Baud:")))
        self._baud_combo = QComboBox()
        for baud in AX25_BAUD_MODE_CHOICES:
            self._baud_combo.addItem(baud, baud)
        self._baud_combo.setToolTip(
            _(
                "AX.25 baud rate for SSDV (AX.25) reception.\n"
                "Selecting a satellite or transponder sets this from the baud\n"
                "rate in its name (e.g. 1k2 / 4k8 / 9k6); 1200 if it names none.\n"
                "You can still change it by hand. Shared with the APRS tab.\n"
                "Not used by the other modes."
            )
        )
        self._load_baud_combo()
        self._baud_combo.currentIndexChanged.connect(self._on_baud_changed)
        self._baud_combo.setEnabled(self._mode_combo.currentText() == _MODE_SSDV)
        top.addWidget(self._baud_combo)

        top.addSpacing(16)

        _sstv_help = QLabel(" ? ")
        _sstv_help.setStyleSheet(
            "color:white;background:#2980b9;border-radius:8px;font-weight:bold;padding:2px 6px;"
        )
        _sstv_help.setToolTip(
            _(
                "Receives images from amateur satellites. Pick the mode below, then the "
                "satellite in Radio Control (or the input box at the right).\n"
                "\n"
                "SSTV (analog picture over FM; Robot36 and PD120 only):\n"
                "  • ISS (NORAD 25544) 145.800 MHz FM — PD120 (Mode V)\n"
                "  • ISS 437.550 MHz FM — Robot36 (Mode U); events are announced at "
                "https://ariss.psnc.pl\n"
                "  • UmKA-1, JAMX01, AHMAT-1, GALAPAGOS-UTE-SWSU, TeikyoSat-4, SEEDS II, "
                "WREN, UTMN2, Monitor-3, QMR-KWT 2 … (Mode U, occasional)\n"
                "  Martin / Scottie / Robot72 are not supported (e.g. SONATE-2 Martin M1, "
                "VIZARD-meteo Robot72).\n"
                "\n"
                "SSDV (AX.25): 1200 / 4800 / 9600 baud AX.25 packets through Direwolf "
                "(Rig + sound card, or SDR). Listed with SSDV: ERMINAZ-2U (9k6 GMSK), "
                "LAPAN-A2 (1200 baud AFSK). Not yet verified with a real reception.\n"
                "\n"
                "SSDV (CCSDS BPSK 9k6): ASRTU-1 / AO-123 (NORAD 61781) 436.210 MHz, "
                "decoded by gr-satellites. Needs an SDR.\n"
                "\n"
                "SSDV (2FSK 800bps): HADES-SA / SO-127 (NORAD 68446) 436.875 MHz, raw "
                "800 baud 2FSK (AMSAT-EA format). Needs an SDR.\n"
                "\n"
                "Selecting HADES-SA or ASRTU-1 switches the mode automatically."
            )
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
                "Received frames appear here live, one per line, as hex. Paste hex "
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
        self._image_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._image_label.customContextMenuRequested.connect(self._on_image_context_menu)
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
        self._history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._history_list.customContextMenuRequested.connect(self._on_history_context_menu)
        right_layout.addWidget(self._history_list)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        # ── bottom bar ───────────────────────────────────────────────────
        bottom = QHBoxLayout()

        # The status line is not shown any more (see _StatusSink)
        self._status_label = _StatusSink(self)
        self._status_label.hide()

        # Fixed height + matching padding so these three differently-worded
        # buttons don't end up with slightly different vertical sizes (Qt's
        # default sizeHint varies a little with text/emoji glyph metrics).
        _bottom_btn_style = "padding: 4px 10px;"

        self._stored_btn = QPushButton(_("📚 Stored Images…"))
        self._stored_btn.setFixedHeight(30)
        self._stored_btn.setStyleSheet(_bottom_btn_style)
        self._stored_btn.setToolTip(
            _(
                "SSDV packets of every mode are stored, so a picture can be built from "
                "several passes."
            )
        )
        self._stored_btn.setEnabled(self._store is not None)
        self._stored_btn.clicked.connect(self._on_stored_images)
        bottom.addWidget(self._stored_btn)

        self._open_image_btn = QPushButton(_("🖼 Open…"))
        self._open_image_btn.setFixedHeight(30)
        self._open_image_btn.setStyleSheet(_bottom_btn_style)
        self._open_image_btn.clicked.connect(self._on_open_image)
        bottom.addWidget(self._open_image_btn)

        self._decode_file_btn = QPushButton(_("📂 Decode MP3…"))
        self._decode_file_btn.setEnabled(SOUNDFILE_AVAILABLE)
        self._decode_file_btn.setFixedHeight(30)
        self._decode_file_btn.setStyleSheet(_bottom_btn_style)
        if not SOUNDFILE_AVAILABLE:
            self._decode_file_btn.setToolTip(_("soundfile not installed — pip install soundfile"))
        self._decode_file_btn.clicked.connect(self._on_decode_file)
        bottom.addWidget(self._decode_file_btn)

        self._decode_iq_btn = QPushButton(_("📂 Decode IQ Recording…"))
        self._decode_iq_btn.setFixedHeight(30)
        self._decode_iq_btn.setStyleSheet(_bottom_btn_style)
        self._decode_iq_btn.setToolTip(
            _(
                "Open a recorded .iq.wav file and play it into the decoder (the same as "
                "SDR Control's Open…).\nA whole-recording spectrogram opens; click it to "
                "start from any point."
            )
        )
        self._decode_iq_btn.clicked.connect(self._on_decode_iq)
        bottom.addWidget(self._decode_iq_btn)

        # SSDV (CCSDS BPSK 9k6) / (2FSK) only: decode the IQ recording at once instead of
        # playing it in real time.
        self._lbl_decode_speed = QLabel(_("Speed:"))
        self._combo_decode_speed = QComboBox()
        self._combo_decode_speed.addItem(_("Normal (real time)"), "normal")
        self._combo_decode_speed.addItem(_("Fast"), "fast")
        self._combo_decode_speed.setFixedHeight(30)
        self._combo_decode_speed.setToolTip(
            _(
                "Normal: play the recording through the SDR like a live reception and\n"
                "decode it as it goes (pick the start on the spectrogram).\n"
                "Fast: decode the whole recording file at once, in seconds."
            )
        )
        self._load_decode_speed()
        self._combo_decode_speed.currentIndexChanged.connect(self._on_decode_speed_changed)
        bottom.addWidget(self._lbl_decode_speed)
        bottom.addWidget(self._combo_decode_speed)
        self._update_decode_speed_visibility(self._mode_combo.currentText())

        self._import_csv_btn = QPushButton(_("📥 Import CSV…"))
        self._import_csv_btn.setFixedHeight(30)
        self._import_csv_btn.setStyleSheet(_bottom_btn_style)
        self._import_csv_btn.setToolTip(
            _(
                "Extract SSDV pictures from a CSV of frames downloaded from SatNOGS DB "
                "(the satellite page's frame download). The packets are added to the "
                "Stored Images."
            )
        )
        self._import_csv_btn.setEnabled(self._store is not None)
        self._import_csv_btn.clicked.connect(self._on_import_csv)
        bottom.addWidget(self._import_csv_btn)

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
        bottom.addStretch()  # the buttons sit at the left edge

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
        mode = self._mode_combo.currentText()
        if mode == _MODE_SSDV and self._ssdv_engine_signals:
            self._start_ssdv_reception()
        elif mode == _MODE_GR_SSDV:
            self._start_gr_ssdv()
        elif mode == _MODE_FSK_SSDV:
            self._start_fsk_ssdv()

    def _load_baud_combo(self) -> None:
        """Show the stored / transmitter-derived baud without applying it."""
        modem = resolve_ax25_modem(self._conn, self._radio_control)
        idx = self._baud_combo.findData(modem)
        self._baud_combo.blockSignals(True)
        self._baud_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._baud_combo.blockSignals(False)

    def _on_baud_changed(self, _index: int) -> None:
        """Persist the chosen baud (shared with the APRS tab) and apply it to a running session."""
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (AX25_BAUD_SETTING_KEY, self._baud_combo.currentData()),
            )
            self._conn.commit()
        self._apply_ssdv_baud()

    def _apply_ssdv_baud(self) -> None:
        """Restart a running SSDV (AX.25) session if its baud no longer matches."""
        if not self._ssdv_reception:
            return
        modem = resolve_ax25_modem(self._conn, self._radio_control)
        engine = self._engine()
        engine.restart_if_modem_changed(modem)
        pipeline = self._find_sdr_pipeline()
        if pipeline is not None:
            engine.sync_sdr_baud(pipeline, modem, satellite=True)

    def _on_transmitter_changed(self, xpdr: Any) -> None:
        """Update satellite name when transponder selection changes."""
        if xpdr and isinstance(xpdr, dict):
            self._sat_name = xpdr.get("description", "")
            self._norad = int(xpdr.get("norad_cat_id") or 0)
            self._auto_select_ssdv_mode(xpdr.get("norad_cat_id"))
            # Follow the transmitter's baud (1200 if it names none); changing the
            # combo persists it and restarts a running session via _on_baud_changed().
            idx = self._baud_combo.findData(detect_modem_for_transmitter(xpdr))
            if idx >= 0 and idx != self._baud_combo.currentIndex():
                self._baud_combo.setCurrentIndex(idx)
        # a different transponder can mean a different AX.25 baud rate
        self._apply_ssdv_baud()

    def _auto_select_ssdv_mode(self, norad: Any) -> None:
        """Pick the SSDV mode that matches a satellite known to need one (HADES-SA, ASRTU-1)."""
        wanted = {_FSK_SSDV_NORAD: _MODE_FSK_SSDV, _GR_SSDV_NORAD: _MODE_GR_SSDV}.get(norad)
        if wanted is not None and self._mode_combo.currentText() != wanted:
            self._mode_combo.setCurrentText(wanted)

    # ------------------------------------------------------------------ #
    # Satellite combo
    # ------------------------------------------------------------------ #

    def _mode_norads(self, mode: str) -> set[int]:
        """NORAD ids of the satellites that provide *mode*.

        SSTV / SSDV (AX.25) are found from the transmitter descriptions in the DB; the
        CCSDS BPSK and 2FSK modes each belong to exactly one satellite (ASRTU-1, HADES-SA).
        """
        if mode == _MODE_GR_SSDV:
            return {_GR_SSDV_NORAD}
        if mode == _MODE_FSK_SSDV:
            return {_FSK_SSDV_NORAD}
        if not hasattr(self._conn, "execute"):
            return set()
        keyword = "SSTV" if mode == _MODE_SSTV else "SSDV"

        def matches(xpdr: dict[str, Any]) -> bool:
            desc = str(xpdr.get("description") or "").upper()
            return keyword in desc or (
                mode == _MODE_SSTV and str(xpdr.get("mode") or "").upper() == "SSTV"
            )

        norads: set[int] = set()
        with contextlib.suppress(Exception):
            from comms.mode_detection import get_norads_matching

            norads = set(get_norads_matching(self._conn, matches))
        if mode == _MODE_SSDV:
            # These two have their own modes
            norads -= {_GR_SSDV_NORAD, _FSK_SSDV_NORAD}
        return norads

    def _populate_sat_combo(self, prefer_norad: int = 0) -> None:
        """Fill the satellite combo with the satellites that provide the current mode.

        The previous pick for this mode (or *prefer_norad*, when it is in the list) is
        selected silently.
        """
        mode = self._mode_combo.currentText()
        self._sat_combo.blockSignals(True)
        self._sat_combo.clear()
        entries: dict[int, str] = {}
        norads = self._mode_norads(mode)
        if norads and hasattr(self._conn, "execute"):
            with contextlib.suppress(Exception):
                placeholders = ",".join("?" * len(norads))
                rows = self._conn.execute(
                    f"SELECT norad_cat_id, name FROM satellites "
                    f"WHERE norad_cat_id IN ({placeholders})",
                    tuple(sorted(norads)),
                ).fetchall()
                for row in rows:
                    entries[int(row["norad_cat_id"])] = str(row["name"])
        for norad, name in sorted(entries.items(), key=lambda kv: kv[1].upper()):
            self._sat_combo.addItem(f"{name}  ({norad})", userData=norad)
        idx = self._sat_combo.findData(prefer_norad) if prefer_norad else -1
        if idx < 0:
            idx = self._sat_combo.findData(self._load_last_satellite(mode))
        if idx >= 0:
            self._sat_combo.setCurrentIndex(idx)
        self._sat_combo.blockSignals(False)

    def _load_last_satellite(self, mode: str) -> int:
        """The satellite picked last time in *mode* (0: none)."""
        if not hasattr(self._conn, "execute"):
            return 0
        with contextlib.suppress(Exception):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (f"{_LAST_SAT_KEY}:{mode}",)
            ).fetchone()
            if row:
                return int(row["value"])
        return 0

    def _announce_restored_satellite(self) -> None:
        norad = self._sat_combo.currentData()
        if norad is not None:
            self.satellite_selected.emit(int(norad))

    def _save_last_satellite(self, norad: int) -> None:
        """Remember the picked satellite of the current mode for the next launch."""
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (f"{_LAST_SAT_KEY}:{self._mode_combo.currentText()}", str(int(norad))),
            )
            self._conn.commit()

    def _on_sat_changed(self, _index: int) -> None:
        norad = self._sat_combo.currentData()
        if norad is not None:
            self._save_last_satellite(int(norad))
            self.satellite_selected.emit(int(norad))

    def _on_sat_search_clicked(self) -> None:
        """Pick a satellite from a searchable list of what the combo offers."""
        entries = []
        for i in range(self._sat_combo.count()):
            norad = int(self._sat_combo.itemData(i))
            name = self._sat_combo.itemText(i).rsplit(f"({norad})", 1)[0].strip()
            entries.append((norad, name))
        if not entries:
            return
        dlg = SatSearchDialog(entries, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected_norad is None:
            return
        idx = self._sat_combo.findData(dlg.selected_norad)
        if idx >= 0:
            self._sat_combo.setCurrentIndex(idx)

    def set_satellite(self, norad: int | None, name: str = "") -> None:
        """Follow the satellite chosen elsewhere (satellite list): show it in the combo."""
        if not norad:
            return
        idx = self._sat_combo.findData(norad)
        if idx >= 0 and idx != self._sat_combo.currentIndex():
            self._sat_combo.blockSignals(True)
            self._sat_combo.setCurrentIndex(idx)
            self._sat_combo.blockSignals(False)
            self._save_last_satellite(norad)

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

        self._decoder = SstvDecoder(sample_rate=self._audio_rate(), parent=self)
        self._decoder.line_received.connect(self._on_line_received)
        self._decoder.image_complete.connect(self._on_image_complete)
        self._decoder.mode_detected.connect(self._on_mode_detected)
        self._decoder.status_changed.connect(self._status_label.setText)
        self._decoder.start()
        self._connect_audio_source()
        self._status_label.setText("")

    def _stop_decoder(self) -> None:
        self._disconnect_audio_source()
        if self._decoder is not None:
            self._decoder.stop()
            self._decoder = None

    def _audio_rate(self) -> int:
        """Sample rate of the audio the decoder is fed: the private SDR demodulator's output
        rate (about 50 kHz, set by the SDR's sample rate), or 44.1 kHz for the shared
        sound-card input.

        Decoding audio at the wrong rate scales every tone and every duration (48 kHz
        read as 44.1 kHz is 8.8 % off), so nothing would decode.
        """
        pipeline = self._find_sdr_pipeline()
        if pipeline is not None:
            try:
                return output_rate_for(int(pipeline._device.sample_rate))
            except Exception:
                logger.exception("SSTV: could not read the SDR sample rate")
        return self._SOUNDCARD_SAMPLE_RATE

    def _connect_audio_source(self) -> None:
        """Connect the current audio source to the active SSTV decoder."""
        if self._decoder is None:
            return
        rate = self._audio_rate()
        if self._decoder.sample_rate != rate:
            self._decoder.set_sample_rate(rate)
        pipeline = self._find_sdr_pipeline()
        if pipeline is not None:
            self._attach_sdr_demod(pipeline)
        elif self._rig_connected:
            self._start_soundcard_capture()

    def _attach_sdr_demod(self, pipeline: Any) -> None:
        """Feed the decoder from a private FM demodulator on the pipeline's raw I/Q.

        The pipeline's shared audio comes from the voice-tuned narrow-FM chain of the
        listening path (loose IF filter, de-emphasis), which left a satellite SSTV picture
        with a speckled lower half; this demodulator is built for the SSTV tones, runs
        whether or not SDR Control is unmuted, and leaves every other consumer alone.
        """
        if self._decoder is None or self._sdr_demod is not None:
            return
        try:
            demod = SstvSdrDemod(int(pipeline._device.sample_rate))
            # DirectConnection: decode on the demodulator's thread, not the GUI thread
            demod.audio_ready.connect(
                self._decoder.push_samples, Qt.ConnectionType.DirectConnection
            )
            demod.start()
            # Hold the demodulator before subscribing so a failure below is still cleaned up
            self._sdr_demod = demod
            pipeline.subscribe(demod.push_samples)
            self._sdr_demod_pipeline = pipeline
        except Exception:
            logger.exception("SSTV: could not attach to the SDR pipeline")
            self._detach_sdr_demod()

    def _detach_sdr_demod(self) -> None:
        """Undo _attach_sdr_demod() (no-op when nothing is attached)."""
        if self._sdr_demod_pipeline is not None and self._sdr_demod is not None:
            with contextlib.suppress(Exception):
                self._sdr_demod_pipeline.unsubscribe(self._sdr_demod.push_samples)
        self._sdr_demod_pipeline = None
        if self._sdr_demod is not None:
            with contextlib.suppress(Exception):
                self._sdr_demod.audio_ready.disconnect()
            self._sdr_demod.stop()
            self._sdr_demod = None

    def _disconnect_audio_source(self) -> None:
        """Disconnect all audio sources from the decoder."""
        self._detach_sdr_demod()
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
            # The red "Input:" notice at the top already says this.
            self._status_label.setText("")
            return
        if not ok:
            self._status_label.setText(f"⚠ {err}")
            return
        self._ssdv_reception = True
        self._status_label.setText(_("SSDV: waiting for AX.25 frames…"))

    def _stop_ssdv_reception(self) -> None:
        """Release this tab's claim on the AX.25 pipeline (and stop gr_satellites)."""
        self._stop_gr_ssdv_reception()
        self._stop_fsk_ssdv_reception()
        if self._ssdv_reception:
            self._engine().stop(_ENGINE_OWNER)
            self._ssdv_reception = False

    # ------------------------------------------------------------------ #
    # SSDV (CCSDS BPSK 9k6) mode (ASRTU-1)
    # ------------------------------------------------------------------ #

    def _ensure_gr_decoder(self) -> Any:
        """The DSLWP-mode SsdvDecoder for the gr-satellites mode, created on first use."""
        from comms.sstv.ssdv import SsdvDecoder

        if self._gr_ssdv_decoder is None:
            self._gr_ssdv_decoder = SsdvDecoder(parent=self, dslwp=True)
            self._gr_ssdv_decoder.image_updated.connect(self._on_ssdv_image)
            self._gr_ssdv_decoder.status_changed.connect(self._status_label.setText)
            self._gr_ssdv_decoder.error_occurred.connect(self._status_label.setText)
        return self._gr_ssdv_decoder

    def _ensure_gr_backend(self) -> Any:
        """The gr_satellites subprocess manager, created on first use."""
        from comms.telemetry.gr_satellites_backend import GrSatellitesBackend

        if self._gr_backend is None:
            self._gr_backend = GrSatellitesBackend(self)
            self._gr_backend.raw_frame_received.connect(self._on_gr_frame)
            self._gr_backend.status_changed.connect(self._status_label.setText)
        return self._gr_backend

    def _start_gr_ssdv(self) -> None:
        """gr-satellites mode: run gr_satellites for ASRTU-1 on the SDR's IQ.

        Needs an SDR (gr_satellites works on IQ) and the gr-satellites install; without
        either the tab still decodes pasted hex frames.
        """
        self._ensure_gr_decoder()
        backend = self._ensure_gr_backend()
        if backend.is_running:
            return
        pipeline = self._find_sdr_pipeline()
        if pipeline is None:
            # The red "Input:" notice at the top already says this.
            self._status_label.setText("")
            return
        try:
            samp_rate = int(pipeline._device.sample_rate)
        except AttributeError:
            samp_rate = 2_400_000
        self._ssdv_frames = 0
        self._ssdv_packets = 0
        ok, err = backend.start(_GR_SSDV_NORAD, samp_rate, pipeline)
        if not ok:
            self._status_label.setText(f"⚠ {err}")
            return
        if not backend.kiss_supported:
            self._status_label.setText(
                _("⚠ This gr_satellites build has no --kiss_server; SSDV frames cannot be read.")
            )
            backend.stop()
            return
        self._status_label.setText(_("SSDV (CCSDS BPSK 9k6): waiting for ASRTU-1 frames…"))

    def _stop_gr_ssdv_reception(self) -> None:
        """Stop the gr_satellites subprocess, if running."""
        if self._gr_backend is not None and self._gr_backend.is_running:
            self._gr_backend.stop()

    def _on_gr_frame(self, raw: bytes) -> None:
        """A frame from gr_satellites: show it as hex, feed any VC1 SSDV packet in it."""
        self._raw_edit.append_frame(raw)
        self._ssdv_frames += 1
        packet = find_dslwp_packet(raw)
        if (
            packet is not None
            and self._gr_ssdv_decoder is not None
            and self._store_and_feed(KIND_DSLWP, packet, _GR_SSDV_NORAD, self._gr_ssdv_decoder)
        ):
            self._ssdv_packets += 1
        self._update_raw_count()

    # ------------------------------------------------------------------ #
    # SSDV (2FSK 800bps) mode (HADES-SA)
    # ------------------------------------------------------------------ #

    def _start_fsk_ssdv(self) -> None:
        """2FSK mode: demodulate the SDR's I/Q with FskSsdvSdrDemod (needs an SDR)."""
        if self._fsk_demod is not None:
            return
        self._ensure_ssdv_decoder()
        pipeline = self._find_sdr_pipeline()
        if pipeline is None:
            # The red "Input:" notice at the top already says this.
            self._status_label.setText("")
            return
        from comms.sstv.fsk_ssdv import FskSsdvSdrDemod

        try:
            demod = FskSsdvSdrDemod(int(pipeline._device.sample_rate), parent=self)
            demod.packet_received.connect(self._on_fsk_packet)
            demod.start()
            self._fsk_demod = demod  # held before subscribing so a failure is cleaned up
            pipeline.subscribe(demod.push_samples)
            self._fsk_demod_pipeline = pipeline
        except Exception:
            logger.exception("SSDV (2FSK): could not attach to the SDR pipeline")
            self._stop_fsk_ssdv_reception()
            self._status_label.setText(_("⚠ Could not start the 2FSK demodulator."))
            return
        self._ssdv_frames = 0
        self._ssdv_packets = 0
        self._update_raw_count()
        self._status_label.setText(_("SSDV (2FSK 800bps): waiting for packets…"))

    def _stop_fsk_ssdv_reception(self) -> None:
        """Detach and stop the 2FSK demodulator, if running."""
        if self._fsk_demod_pipeline is not None and self._fsk_demod is not None:
            with contextlib.suppress(Exception):
                self._fsk_demod_pipeline.unsubscribe(self._fsk_demod.push_samples)
        self._fsk_demod_pipeline = None
        if self._fsk_demod is not None:
            with contextlib.suppress(Exception):
                self._fsk_demod.packet_received.disconnect()
            self._fsk_demod.stop()
            self._fsk_demod = None

    def _on_fsk_packet(self, packet: bytes, verified: bool) -> None:
        """A 256-byte SSDV packet from the 2FSK demodulator: show it, keep it, feed the decoder.

        Only packets that are good (CRC-32 right, or right after Reed-Solomon repair) are
        used, so a bit-errored copy can never replace a good one.
        """
        self._raw_edit.append_frame(packet)
        self._ssdv_frames += 1
        valid = packet if verified else validate_packet(packet)
        if valid is not None and self._store_and_feed(
            KIND_STANDARD, valid, _FSK_SSDV_NORAD, self._ensure_ssdv_decoder()
        ):
            self._ssdv_packets += 1
        self._update_raw_count()

    # ------------------------------------------------------------------ #
    # Stored packets (pictures built from several passes)
    # ------------------------------------------------------------------ #

    def _store_and_feed(self, kind: str, packet: bytes, norad: int, decoder: Any) -> bool:
        """Keep a valid packet in the database and hand it to *decoder*.

        The decoder always holds the whole stored picture the packet belongs to (packets of
        earlier passes included): when the packet starts or joins a different group than
        the one the decoder was given, the decoder is reloaded from the store.
        """
        if self._store is None:
            return bool(decoder.push_packet(packet))
        try:
            result = self._store.add(norad, kind, packet)
        except Exception:
            logger.exception("SSDV: could not store a packet")
            return bool(decoder.push_packet(packet))
        if self._active_group.get(kind) != result.group_id:
            decoder.reset()
            decoder.load_packets(self._store.packets(result.group_id))
            self._active_group[kind] = result.group_id
        else:
            decoder.push_packet(packet)
        return True

    def _satellite_name(self, norad: int) -> str:
        """Name of a satellite for the Stored Images list."""
        if norad:
            try:
                row = self._conn.execute(
                    "SELECT name FROM satellites WHERE norad_cat_id = ?", (norad,)
                ).fetchone()
            except Exception:
                row = None
            if row:
                return f"{row[0]} ({norad})"
            return str(norad)
        return _("Unknown")

    def _on_stored_images(self) -> None:
        """Open the list of stored pictures."""
        if self._store is None:
            return
        from ui.ssdv_store_dialog import SsdvStoreDialog

        dialog = SsdvStoreDialog(self._store, self._satellite_name, self)
        dialog.build_requested.connect(self._on_build_stored)
        dialog.exec()

    def _on_build_stored(self, group_id: int) -> None:
        """Build the picture of a stored group from all its packets."""
        if self._store is None:
            return
        info = self._store.group(group_id)
        if info is None:
            return
        packets = self._store.packets(group_id)
        dslwp = info.kind == KIND_DSLWP
        decoder = self._ensure_gr_decoder() if dslwp else self._ensure_ssdv_decoder()
        decoder.reset()
        decoder.load_packets(packets)
        self._active_group[info.kind] = group_id
        # the Raw Packets tab shows the picture's packets too, in packet-number order
        self._raw_edit.clear()
        for packet in packets:
            self._raw_edit.append_frame(packet)
        self._ssdv_frames = self._ssdv_packets = len(packets)
        self._update_raw_count()
        if decoder.decode_now():
            self._commit_pending_ssdv(f"{self._satellite_name(info.norad)} image {info.image_id}")
            self._view_tabs.setCurrentWidget(self._image_page)
            self._status_label.setText(
                _("Stored image {i}: {n} packets built.").format(i=info.image_id, n=len(packets))
            )

    def _on_import_csv(self) -> None:
        """Extract SSDV pictures from a CSV downloaded from SatNOGS DB."""
        if self._store is None or self._csv_worker is not None:
            return
        start = Path.home() / "Downloads"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Import SatNOGS CSV"),
            str(start if start.is_dir() else Path.home()),
            _("CSV files (*.csv);;All files (*)"),
        )
        if not path:
            return
        self._import_csv_btn.setEnabled(False)
        self._csv_worker = _CsvExtractWorker(path, self._norad, self)
        self._csv_worker.done.connect(self._on_csv_extracted)
        self._csv_worker.failed.connect(self._on_csv_failed)
        self._csv_worker.finished.connect(self._on_csv_worker_finished)
        self._csv_worker.start()
        self._status_label.setText(_("Reading the CSV…"))

    def _on_csv_failed(self, message: str) -> None:
        QMessageBox.warning(self, _("Import SatNOGS CSV"), message)

    def _on_csv_worker_finished(self) -> None:
        if self._csv_worker is not None:
            self._csv_worker.deleteLater()
            self._csv_worker = None
        self._import_csv_btn.setEnabled(self._store is not None)

    def _on_csv_extracted(self, extract: CsvExtract) -> None:
        """Add the CSV's valid packets to the store, then show the Stored Images."""
        if self._store is None:
            return
        if not extract.packets:
            QMessageBox.information(
                self,
                _("Import SatNOGS CSV"),
                _("No SSDV packets were found in this file ({n} frames).").format(n=extract.frames),
            )
            return
        new = 0
        groups: set[int] = set()
        for item in extract.packets:
            result = self._store.add(item.norad, item.kind, item.packet, item.when, commit=False)
            groups.add(result.group_id)
            new += int(result.is_new)
        self._store.commit()
        self._active_group.clear()  # the decoders reload from the store on the next packet
        QMessageBox.information(
            self,
            _("Import SatNOGS CSV"),
            _(
                "{frames} frames read: {packets} valid SSDV packets ({new} new), "
                "{pictures} pictures. Select one in the list and press Build Image."
            ).format(
                frames=extract.frames,
                packets=len(extract.packets),
                new=new,
                pictures=len(groups),
            ),
        )
        self._on_stored_images()

    def _stop_ssdv(self) -> None:
        """Leave SSDV mode: disconnect from the AX.25 pipeline, decode what is buffered."""
        self._stop_fast_decode()
        self._active_group.clear()  # flush() empties the decoders below
        self._stop_gr_ssdv_reception()
        self._stop_fsk_ssdv_reception()
        if self._gr_ssdv_decoder is not None:
            self._gr_ssdv_decoder.flush()
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
        if packet is not None and self._ssdv_decoder is not None:
            valid = validate_packet(packet)
            if valid is not None:
                accepted = self._store_and_feed(
                    KIND_STANDARD, valid, self._norad, self._ssdv_decoder
                )
            else:
                accepted = self._ssdv_decoder.push_packet(packet)  # ssdv may still repair it
            if accepted:
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
        gr_mode = self._mode_combo.currentText() == _MODE_GR_SSDV
        decoder = self._ensure_gr_decoder() if gr_mode else self._ensure_ssdv_decoder()
        finder = find_dslwp_packet if gr_mode else find_ssdv_packet
        decoder.reset()
        self._active_group.clear()
        packets = 0
        for frame in frames:
            packet = finder(frame)
            if packet is not None and decoder.push_packet(packet):
                packets += 1
        if packets == 0:
            hint = (
                _("(223-byte CCSDS frames on virtual channel 1 with a valid CRC)")
                if gr_mode
                else _("(sync 55 66 / 55 67 and a valid CRC)")
            )
            self._status_label.setText(
                _("{n} frames, but none contains an SSDV packet ").format(n=len(frames)) + hint
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
        if self._gr_ssdv_decoder is not None:
            self._gr_ssdv_decoder.reset()
        self._active_group.clear()
        self._ssdv_frames = 0
        self._ssdv_packets = 0
        self._raw_count_label.setText("")

    def _on_ssdv_image(self, qimg: QImage) -> None:
        """Show the (progressively better) SSDV image; it enters the history on commit."""
        self._ssdv_pending = qimg
        self._current_image = qimg
        self._show_scaled(qimg)

    def _commit_pending_ssdv(self, sat_name: str | None = None) -> None:
        """Put the latest SSDV image into the history / DB / auto-save, once.

        *sat_name* names the picture when it is not the one the selected transmitter is for
        (a stored picture built from the Stored Images list).
        """
        if self._ssdv_pending is not None:
            qimg, self._ssdv_pending = self._ssdv_pending, None
            if sat_name is None:
                self._on_image_complete(qimg, "SSDV")
            else:
                self._record_completed_image(qimg, "SSDV", sat_name_override=sat_name)

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
        self._current_mode = mode
        sat_name = sat_name_override if sat_name_override is not None else self._sat_name

        now = datetime.now(UTC)
        label = f"{sat_name or 'SSTV'}\n{now.strftime('%H:%M UTC')}"
        item = _ThumbnailItem(qimg, label)
        self._history_list.addItem(item)

        self._persist_to_db(qimg, mode, now, sat_name=sat_name)

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
        """Switch between the SSTV, SSDV (AX.25), SSDV (CCSDS BPSK 9k6) and SSDV (2FSK) decoders."""
        self._stop_decoder()
        self._stop_ssdv()
        self._update_decode_speed_visibility(mode_text)
        # The baud only matters for SSDV (AX.25); grey it out for every other mode.
        self._baud_combo.setEnabled(mode_text == _MODE_SSDV)
        if mode_text == _MODE_SSDV:
            self._load_baud_combo()
        # Each mode has its own satellites; show them and follow the new pick
        # (unless the mode change came from selecting that satellite's transmitter)
        previous = self._sat_combo.currentData()
        self._populate_sat_combo(prefer_norad=self._norad)
        now = self._sat_combo.currentData()
        if now is not None and now != previous and now != self._norad:
            self._announce_restored_satellite()
        if mode_text == _MODE_SSTV:
            self._view_tabs.setCurrentWidget(self._image_page)
            self._start_decoder()
        elif mode_text == _MODE_GR_SSDV:
            self._view_tabs.setCurrentWidget(self._raw_page)
            self._start_gr_ssdv()
        elif mode_text == _MODE_FSK_SSDV:
            self._view_tabs.setCurrentWidget(self._raw_page)
            self._start_fsk_ssdv()
        else:
            self._view_tabs.setCurrentWidget(self._raw_page)
            self._start_ssdv()

    def _on_decode_iq(self) -> None:
        """Open an IQ recording exactly as SDR Control's "Open…" does.

        The recording is loaded as a replay SDR in a rig slot and played in real time; the
        tab picks it up like a live SDR (see _on_sdr_connected), so the picture is decoded as
        it plays and the whole-recording spectrogram lets the user start from any point.
        """
        if (
            self._mode_combo.currentText() in _FAST_DECODE_MODES
            and self._combo_decode_speed.currentData() == "fast"
        ):
            self._on_fast_decode()
            return
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        if sdr_ctrl is None or not hasattr(sdr_ctrl, "open_recording"):
            QMessageBox.information(
                self,
                _("Decode IQ Recording"),
                _("SDR Control is not available."),
            )
            return
        sdr_ctrl.open_recording()

    # -- Fast decode of an IQ recording file (CCSDS BPSK 9k6 / 2FSK, no SDR pipeline) ---- #

    def _update_decode_speed_visibility(self, mode_text: str) -> None:
        """Show the Speed choice only in the modes that can decode a file at once."""
        visible = mode_text in _FAST_DECODE_MODES
        self._lbl_decode_speed.setVisible(visible)
        self._combo_decode_speed.setVisible(visible)

    def _load_decode_speed(self) -> None:
        """Restore the Speed choice (Normal / Fast) from app_settings."""
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_DECODE_SPEED_KEY,)
            ).fetchone()
            if row:
                idx = self._combo_decode_speed.findData(str(row["value"]))
                if idx >= 0:
                    self._combo_decode_speed.setCurrentIndex(idx)

    def _on_decode_speed_changed(self, _index: int) -> None:
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_DECODE_SPEED_KEY, str(self._combo_decode_speed.currentData())),
            )
            self._conn.commit()

    def _on_fast_decode(self) -> None:
        """Ask for an IQ recording and decode all of it at once."""
        if self._fast_job is not None:
            return
        start_dir = Path.home() / "iq_recordings"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Decode IQ Recording"),
            str(start_dir if start_dir.is_dir() else Path.home()),
            "IQ WAV (*.wav);;All files (*)",
        )
        if path:
            self._start_fast_decode(Path(path))

    def _start_fast_decode(self, path: Path) -> bool:
        """Start decoding *path* in a worker thread; False (reason on screen) if it cannot."""
        if self._mode_combo.currentText() == _MODE_GR_SSDV:
            from comms.telemetry.gr_satellites_backend import detect_gr_satellites
            from comms.telemetry.gr_satellites_file import GrFileDecoder

            if self._gr_backend is not None and self._gr_backend.is_running:
                self._status_label.setText("⚠ " + _("Stop the running decoder first."))
                return False
            if not detect_gr_satellites():
                self._status_label.setText(
                    "⚠ " + _("gr-satellites is not installed — see Help > gr-satellites…")
                )
                return False
            self._ensure_gr_decoder()
            job: Any = GrFileDecoder(path, _GR_SSDV_NORAD, parent=self)
            job.frame_found.connect(self._on_gr_fast_frame)
        else:
            from comms.sstv.fsk_ssdv import FskSsdvFileDecoder

            if self._fsk_demod is not None:
                self._status_label.setText("⚠ " + _("Stop the running decoder first."))
                return False
            self._ensure_ssdv_decoder()
            job = FskSsdvFileDecoder(path, parent=self)
            job.packet_found.connect(self._on_fsk_packet)
        job.progress.connect(self._on_fast_progress)
        job.decoded.connect(self._on_fast_done)
        job.failed.connect(self._on_fast_failed)
        self._fast_job = job
        self._ssdv_frames = 0
        self._ssdv_packets = 0
        self._decode_iq_btn.setEnabled(False)
        self._combo_decode_speed.setEnabled(False)
        self._status_label.setText(_("Fast decode: starting…"))
        job.start()
        return True

    def _on_gr_fast_frame(self, _text: str, _seconds: float, raw: bytes) -> None:
        """One frame of a gr_satellites fast decode (empty when its bytes were not paired)."""
        if self._fast_job is not None and raw:
            self._on_gr_frame(raw)

    def _on_fast_progress(self, fraction: float, frames: int) -> None:
        if self._fast_job is None:
            return
        if fraction < 0:
            self._status_label.setText(_("Fast decode: running — {n} frame(s)").format(n=frames))
            return
        self._status_label.setText(
            _("Fast decode: {pct}% — {n} frame(s)").format(pct=int(fraction * 100), n=frames)
        )

    def _on_fast_done(self, duration_s: float, elapsed_s: float, frames: int) -> None:
        self._finish_fast_decode()
        for decoder in (self._gr_ssdv_decoder, self._ssdv_decoder):
            if decoder is not None:
                decoder.flush()  # decode what is buffered
        self._active_group.clear()
        self._status_label.setText(
            _("Decoded {dur} s of recording in {el} s — {n} frame(s)").format(
                dur=round(duration_s), el=round(elapsed_s, 1), n=frames
            )
        )

    def _on_fast_failed(self, message: str) -> None:
        self._finish_fast_decode()
        self._status_label.setText("⚠ " + _("Could not decode the recording: ") + message)

    def _finish_fast_decode(self) -> None:
        """The worker is done (or gone): release it and restore the buttons."""
        job = self._fast_job
        self._fast_job = None
        if job is not None:
            for signal in (job.progress, job.decoded, job.failed):
                with contextlib.suppress(Exception):
                    signal.disconnect()
            with contextlib.suppress(Exception):
                (job.frame_found if hasattr(job, "frame_found") else job.packet_found).disconnect()
            job.wait(3000)
        self._decode_iq_btn.setEnabled(True)
        self._combo_decode_speed.setEnabled(True)

    def _stop_fast_decode(self) -> None:
        """Cancel a running fast decode (mode change, close)."""
        if self._fast_job is None:
            return
        self._fast_job.cancel()
        self._finish_fast_decode()

    def _on_decode_file(self) -> None:
        """Batch-decode an SSTV image from a previously recorded MP3/WAV file."""
        if self._file_decode_worker is not None:
            return
        from core.data_paths import KIND_AUDIO, get_dir

        start_dir = str(get_dir(KIND_AUDIO))
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

    def _image_menu(self, qimg: QImage | None) -> QMenu:
        """The right-click menu of a picture: save it by hand."""
        menu = QMenu(self)
        action = menu.addAction(_("💾 Save Image As…"))
        action.setEnabled(qimg is not None)
        if qimg is not None:
            action.triggered.connect(lambda _checked=False, img=qimg: self._save_image_as(img))
        return menu

    def _on_image_context_menu(self, pos: Any) -> None:
        self._image_menu(self._current_image).exec(self._image_label.mapToGlobal(pos))

    def _on_history_context_menu(self, pos: Any) -> None:
        item = self._history_list.itemAt(pos)
        image = item.full_image if isinstance(item, _ThumbnailItem) else None
        self._image_menu(image).exec(self._history_list.viewport().mapToGlobal(pos))

    def _save_image_as(self, qimg: QImage) -> None:
        """Ask for a file name and save *qimg* as a PNG (default: the image save folder)."""
        mode = self._save_mode_name(self._mode_combo.currentText())
        folder = self._image_save_dir(mode)
        with contextlib.suppress(OSError):
            folder.mkdir(parents=True, exist_ok=True)
        name = self._safe_name(self._sat_name) or "image"
        default = folder / f"{mode}_{name}_{datetime.now(UTC):%Y%m%d_%H%M%S}.png"
        path, _filter = QFileDialog.getSaveFileName(
            self, _("Save Image"), str(default), _("PNG Images (*.png)")
        )
        if path:
            self._save_image_to(qimg, path)

    def _save_image_to(self, qimg: QImage, path: str) -> bool:
        """Write *qimg* to *path*; a failure is reported in the status bar."""
        if not qimg.save(path):
            self._status_label.setText(_("⚠ Could not save the image: {path}").format(path=path))
            return False
        self._status_label.setText(_("Saved: ") + os.path.basename(path))
        return True

    def _on_history_clicked(self, item: QListWidgetItem) -> None:
        """Show clicked thumbnail at full size in the main view."""
        if not isinstance(item, _ThumbnailItem):
            return
        self._current_image = item.full_image
        pix = QPixmap.fromImage(item.full_image).scaled(
            self._image_label.width(),
            self._image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(pix)

    def _on_open_image(self) -> None:
        """Open a saved image file and show it in the main view (and the history)."""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Open SSTV Image"),
            str(self._image_save_dir(self._save_mode_name(self._mode_combo.currentText()))),
            _("Images (*.png *.jpg *.jpeg *.bmp)"),
        )
        if not path:
            return
        qimg = QImage(path)
        if qimg.isNull():
            QMessageBox.warning(self, _("Open SSTV Image"), _("Could not read the image file."))
            return
        self._current_image = qimg
        self._history_list.addItem(_ThumbnailItem(qimg, Path(path).stem))
        pix = QPixmap.fromImage(qimg).scaled(
            self._image_label.width(),
            self._image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(pix)
        self._status_label.setText(_("Opened: ") + os.path.basename(path))

    def _on_clear(self) -> None:
        """Clear the live image display."""
        self._ssdv_pending = None
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(_("Waiting for signal…"))
        self._current_image = None
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
        # "Auto-save PNG" off: the picture is logged but no file is written
        file_path = (
            self._auto_save_image(qimg, mode, ts, sat_name=name)
            if self._auto_save_cb.isChecked()
            else None
        )
        self._conn.execute(
            """
            INSERT INTO sstv_log (received_at, mode, file_path, callsign)
            VALUES (?, ?, ?, ?)
            """,
            (ts.isoformat(), mode, file_path, name or None),
        )
        self._conn.commit()

    @staticmethod
    def _save_mode_name(mode_text: str) -> str:
        """ "SSDV" for both SSDV modes (they share a folder), else "SSTV"."""
        return "SSDV" if mode_text in (_MODE_SSDV, _MODE_GR_SSDV, _MODE_FSK_SSDV) else "SSTV"

    @staticmethod
    def _image_save_dir(mode: str = "SSTV") -> Path:
        """Default folder for saved images: <Pictures>/FBSAT59-SSTV, or
        <Pictures>/FBSAT59-SSDV for SSDV images."""
        from core.data_paths import KIND_SSDV, KIND_SSTV, get_dir

        return get_dir(KIND_SSDV if mode == "SSDV" else KIND_SSTV)

    @staticmethod
    def _safe_name(name: str) -> str:
        """*name* made fit for a file name: characters that are not allowed (a transmitter
        called "Mode V/U ..." would otherwise name a folder that does not exist) become "-"."""
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", name)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
        return cleaned[:80]

    def _auto_save_image(
        self, qimg: QImage, mode: str, ts: datetime, sat_name: str | None = None
    ) -> str | None:
        """Save image to the user Pictures directory. Returns saved path or None."""
        name = self._safe_name(sat_name if sat_name is not None else self._sat_name)
        save_dir = self._image_save_dir(mode)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._status_label.setText(f"⚠ {exc}")
            return None
        prefix = "SSDV" if mode == "SSDV" else "SSTV"
        filename = f"{prefix}_{name or 'image'}_{ts.strftime('%Y%m%d_%H%M%S')}.png"
        path = str(save_dir / filename)
        if not qimg.save(path):
            self._status_label.setText(_("⚠ Could not save the image: {path}").format(path=path))
            return None
        return path

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._stop_decoder()  # also calls _disconnect_audio_source -> _stop_soundcard_capture
        self._stop_ssdv()
        if self._csv_worker is not None:
            self._csv_worker.wait(5000)
        if self._file_decode_worker is not None:
            # Batch file-decode is a short, self-contained CPU job (no rig/
            # subprocess/hardware to release) — just wait for it rather than
            # tearing it down mid-run, same reasoning as METEOR/HRPT's
            # SatDumpProcess.stop() waiting instead of destroying a running
            # QThread out from under itself.
            self._file_decode_worker.wait(5000)
            self._file_decode_worker = None
        super().closeEvent(event)
