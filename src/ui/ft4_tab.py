"""Communications > FT4 tab.

Provides FT4 TX/RX for satellite QSOs using:
  - ft8_lib (ctypes) for message encode/decode
  - sounddevice for audio I/O
  - RigController for PTT (CAT)

Rig + Sound Card configuration (Rig Settings > Sound Card) is required.
SDR-only mode is not supported because FT4 requires TX capability.
If a second rig slot is an SDR, it can optionally be used for RX audio.

Tab is non-resident: opened via Communications > FT4 and closed with ×.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager
from comms.ft4.codec import (
    SAMPLE_RATE,
    Ft4Codec,
    Ft4Message,
    get_user_ft8lib_dir,
)
from comms.ft4.decode_log import get_ft4_decode_logger
from comms.ft4.qso import Ft4QsoManager, QsoState
from comms.ft4.rx_capture import Ft4RxCaptureWorker
from comms.ft4.scheduler import Ft4Scheduler
from i18n import _
from ui.ft4_waterfall_dialog import Ft4WaterfallDialog

UTC = UTC

# Columns in the decoded-messages table
_COL_UTC = 0
_COL_DB = 1
_COL_DT = 2
_COL_FREQ = 3
_COL_MSG = 4
_COL_COUNT = 5

_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")


def _parse_cq_call_grid(words: list[str]) -> tuple[str, str]:
    """Extract (callsign, grid) from the words following "CQ" in a decoded message.

    Directed CQs insert a keyword before the callsign (e.g. "CQ WWA <call>
    <grid>", "CQ DX <call>", "CQ POTA <call> <grid>"), so the callsign is not
    always the first word — it is always the token immediately before the
    grid, or the last token when no grid is present.
    """
    if not words:
        return "", ""
    if len(words) >= 2 and _GRID_RE.match(words[-1]):
        return words[-2], words[-1]
    return words[-1], ""


_FT4_SETTINGS_KEY = "ft4_settings"
_DEFAULT_AUDIO_FREQ = 1000.0  # Hz — base tone within SSB passband
_AUDIO_OWNER = "FT4"


# ---------------------------------------------------------------------------
# Worker: TX audio output (runs in daemon thread to avoid blocking the UI)
# ---------------------------------------------------------------------------


class _TxWorker(QObject):
    """Plays FT4 audio through sounddevice and controls PTT.

    Lives in a plain Python thread (not QThread) because sounddevice.play()
    is blocking and we do not need Qt event loop inside the worker.
    """

    finished: Signal = Signal()
    error: Signal = Signal(str)

    def __init__(
        self,
        audio: NDArray[np.float32],
        out_device: int | None,
        rig: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._audio = audio
        self._out_device = out_device
        self._rig = rig

    def run(self) -> None:
        """Emits exactly one of `error` or `finished` — never both, so a
        failure status is never clobbered by an immediately-following
        "TX done" (see finished/error handlers in Ft4Tab)."""
        mgr = get_audio_device_manager()
        if not mgr.acquire_output(_AUDIO_OWNER, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            self.error.emit(_("Sound card output is in use by {other}").format(other=other))
            return
        try:
            import sounddevice as sd  # optional dep

            if self._rig is not None:
                if not self._rig.set_ptt(True):
                    self.error.emit(_("PTT command failed — check Rig 1 connection"))
                    return
                time.sleep(0.15)  # PTT lead time

            sd.play(self._audio, samplerate=SAMPLE_RATE, device=self._out_device, blocking=False)
            mgr.pin_active_output(_AUDIO_OWNER)
            sd.wait()

            if self._rig is not None:
                time.sleep(0.10)  # PTT tail time
                self._rig.set_ptt(False)

            self.finished.emit()
        except Exception as exc:
            if self._rig is not None:
                with contextlib.suppress(Exception):
                    self._rig.set_ptt(False)
            self.error.emit(str(exc))
        finally:
            mgr.release_output(_AUDIO_OWNER, self._out_device)


class _RxDecodeWorker(QObject):
    """Runs Ft4Codec.decode_audio() off the Qt main thread.

    libft4wsjt's full 3-pass subtract+BP/OSD decode over a crowded band can
    take a large fraction of a 6s period (measured ~0.4s on a fast desktop
    for ~28 overlapping stations; a low-power field PC can take several
    times longer). Calling it directly from the scheduler's QTimer-driven
    slot blocked the Qt event loop for that whole time, which stalled the
    scheduler's own timer and desynced RX period boundaries from real UTC
    time for every period after the first slow decode — this is what let a
    single strong station decode while WSJT-X, running as a separate
    process, decoded many more from the same audio (2026-07-09).

    libft4wsjt's C bridge keeps its state in Fortran module-level `save`
    variables (see scripts/wsjtx_bridge/ft4wsjt_bridge.f90) and is not
    reentrant, so Ft4Tab must never have two of these running at once — see
    `_decode_busy` in `_on_capture_period()`.
    """

    done: Signal = Signal(object, object)  # (messages: list[Ft4Message], audio: NDArray)

    def __init__(
        self,
        codec: Ft4Codec,
        audio: NDArray[np.float32],
        my_call: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._codec = codec
        self._audio = audio
        self._my_call = my_call

    def run(self) -> None:
        t0 = time.monotonic()
        try:
            messages = self._codec.decode_audio(self._audio, my_call=self._my_call)
        except Exception:
            messages = []
        elapsed = time.monotonic() - t0
        get_ft4_decode_logger().info(
            "decode audio_len=%.2fs duration=%.2fs messages=%d",
            len(self._audio) / SAMPLE_RATE,
            elapsed,
            len(messages),
        )
        self.done.emit(messages, self._audio)


# ---------------------------------------------------------------------------
# FT4 Tab
# ---------------------------------------------------------------------------


class Ft4Tab(QWidget):
    """FT4 QSO tab.

    Requires a rig connected and Sound Card settings configured.
    Opens via Communications > FT4 or when a FT4 transponder is selected.
    """

    #: Emitted from the audio callback thread (soundcard or SDR) with the
    #: current chunk's peak level in dBFS; Qt auto-queues this to the UI
    #: thread. Lets the RX Level meter confirm audio is actually reaching
    #: this tab, independent of whether anything decodes.
    level_updated: Signal = Signal(float)

    #: Emitted from Ft4RxCaptureWorker's own thread (see _on_capture_period)
    #: when a period's audio was captured but decode was skipped (busy or
    #: unavailable) — Qt auto-queues this to the UI thread so the waterfall
    #: can still be updated safely (it does QPainter/QPixmap work, which
    #: must run on the Qt main thread).
    period_skipped: Signal = Signal(object)

    _LEVEL_MIN_INTERVAL_S = 0.05  # ~20fps UI update cap

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control

        self._codec = Ft4Codec()
        self._scheduler = Ft4Scheduler(self)
        self._rx_capture = Ft4RxCaptureWorker(self._on_capture_period)
        self._qso: Ft4QsoManager | None = None  # created when callsign is known
        self._audio_active: bool = False  # soundcard RX subscribed via AudioDeviceManager
        self._tx_thread: threading.Thread | None = None
        self._tx_enabled: bool = False
        self._tx_in_progress: bool = False
        self._last_level_emit: float = 0.0
        self._waterfall_dialog: Ft4WaterfallDialog | None = None
        self._decode_busy: bool = False
        self._decode_thread: threading.Thread | None = None

        self._my_call: str = ""
        self._my_grid: str = ""
        self._audio_freq: float = _DEFAULT_AUDIO_FREQ
        self._out_device: int | None = None
        self._in_device: int | None = None
        self._rx_source: str = "soundcard"  # "soundcard" or "sdr"
        self._tx_slot_mode: str = "auto"  # "auto", "even", or "odd"
        self._sdr_connected: bool = False
        self._sdr_pipeline: Any | None = None

        self._load_settings()
        self._ensure_table()
        self._setup_ui()
        self.level_updated.connect(self._on_level_updated)
        self.period_skipped.connect(self._on_period_skipped)
        self._connect_rig_signals()
        self._connect_sdr_audio()
        self._refresh_codec_status()

        # Scheduler signals — Ft4Scheduler (QTimer-based) now only drives the
        # TX-turn decision and the countdown/TX-RX indicator display. RX
        # audio capture and decode triggering live entirely in
        # Ft4RxCaptureWorker (see _on_capture_period), whose own thread is
        # never blocked by whatever the Qt main thread is doing (2026-07-10).
        self._scheduler.period_tick.connect(self._on_period_tick)
        self._scheduler.period_changed.connect(self._on_period_changed)

        # Start listening immediately — decoding is a receive-only operation
        # and must not require pressing CQ / TX Enable first. TX itself
        # stays gated behind _tx_enabled, so this cannot transmit anything.
        self._start_scheduler(tx_even=True)
        if self._rx_source != "sdr":
            self._start_audio_capture()

    # ------------------------------------------------------------------ #
    # Setup                                                                #
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)

        # -- Top: input/codec banners --
        self._input_banner = QLabel()
        self._input_banner.setStyleSheet("color: #f44336;")
        _ft4_help = QLabel(" ? ")
        _ft4_help.setStyleSheet(
            "color:white;background:#2980b9;border-radius:8px;font-weight:bold;padding:2px 6px;"
        )
        _ft4_help.setToolTip(
            "FT4 is available on:\n"
            "  • RS-44   (NORAD 44909)  DL 435.612 MHz / UL 145.993 MHz\n"
            "  • JO-97   (NORAD 43803)  DL 145.857 MHz / UL 435.118 MHz\n"
            "  • MO-122  (NORAD 60209)  DL 435.812 MHz / UL 145.938 MHz\n\n"
            "Select one of these satellites in Radio Control to get started."
        )
        _banner_row = QHBoxLayout()
        _banner_row.setSpacing(6)
        _banner_row.addWidget(self._input_banner)
        _banner_row.addWidget(_ft4_help)
        _banner_row.addStretch()
        root.addLayout(_banner_row)

        self._codec_banner = QLabel()
        self._codec_banner.setWordWrap(True)
        self._codec_banner.setStyleSheet("background:#e74c3c;color:white;padding:4px;")
        self._codec_banner.setVisible(False)
        root.addWidget(self._codec_banner)

        # -- Configuration row (single GroupBox, all settings inline) --
        cfg_grp = QGroupBox(_("Configuration"))
        cfg_lay = QHBoxLayout(cfg_grp)
        cfg_lay.setSpacing(6)

        cfg_lay.addWidget(QLabel(_("My Call:")))
        self._call_edit = QLineEdit(self._my_call)
        self._call_edit.setMinimumWidth(70)
        self._call_edit.setMaximumWidth(100)
        self._call_edit.textChanged.connect(self._on_settings_changed)
        cfg_lay.addWidget(self._call_edit)

        cfg_lay.addWidget(QLabel(_("Grid:")))
        self._grid_edit = QLineEdit(self._my_grid)
        self._grid_edit.setMinimumWidth(50)
        self._grid_edit.setMaximumWidth(70)
        self._grid_edit.textChanged.connect(self._on_settings_changed)
        cfg_lay.addWidget(self._grid_edit)

        cfg_lay.addWidget(QLabel(_("Audio Hz:")))
        self._audio_freq_edit = QLineEdit(str(int(self._audio_freq)))
        self._audio_freq_edit.setMinimumWidth(50)
        self._audio_freq_edit.setMaximumWidth(60)
        self._audio_freq_edit.textChanged.connect(self._on_settings_changed)
        cfg_lay.addWidget(self._audio_freq_edit)

        cfg_lay.addWidget(QLabel(_("RX:")))
        self._rx_src_combo = QComboBox()
        self._rx_src_combo.addItem(_("Rig Soundcard"), "soundcard")
        self._rx_src_combo.addItem(_("SDR"), "sdr")
        self._rx_src_combo.setMaximumWidth(100)
        self._rx_src_combo.currentIndexChanged.connect(self._on_rx_source_changed)
        cfg_lay.addWidget(self._rx_src_combo)

        # RX level meter — confirms audio is actually reaching this tab's
        # decode pipeline, independent of whether anything decodes.
        cfg_lay.addWidget(QLabel(_("RX Level:")))
        self._level_bar = QProgressBar()
        self._level_bar.setRange(0, 100)
        self._level_bar.setValue(0)
        self._level_bar.setTextVisible(False)
        self._level_bar.setFixedHeight(14)
        self._level_bar.setFixedWidth(80)
        self._level_bar.setToolTip(_("-- dBFS"))
        cfg_lay.addWidget(self._level_bar)

        self._waterfall_btn = QPushButton(_("Waterfall"))
        self._waterfall_btn.setToolTip(
            _(
                "Open a popup showing the last RX period's audio as a "
                "spectrogram, to check whether real FT4 signals are visible "
                "in the passband even when nothing decodes."
            )
        )
        self._waterfall_btn.clicked.connect(self._on_show_waterfall)
        cfg_lay.addWidget(self._waterfall_btn)

        cfg_lay.addStretch()
        root.addWidget(cfg_grp)

        # -- Period / TX status row (no GroupBox, same as Q65) --
        status_row = QHBoxLayout()

        self._period_label = QLabel("FT4")
        self._period_label.setStyleSheet("font-weight:bold;font-size:13px;")
        status_row.addWidget(self._period_label)

        self._countdown_label = QLabel("6.0 s / 6")
        self._countdown_label.setStyleSheet("font-size:13px;")
        status_row.addWidget(self._countdown_label)

        status_row.addStretch()

        self._rx_indicator = QLabel(_("● RX"))
        self._rx_indicator.setStyleSheet("color:#00cc44;font-weight:bold;")
        status_row.addWidget(self._rx_indicator)

        self._tx_indicator = QLabel(_("● TX"))
        self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")
        status_row.addWidget(self._tx_indicator)

        root.addLayout(status_row)

        # -- Transmit GroupBox (buttons + TX line + QSO row) --
        tx_grp = QGroupBox(_("Transmit"))
        tx_lay = QVBoxLayout(tx_grp)
        tx_lay.setSpacing(4)

        # Quick buttons + TX Enable / Halt TX
        btn_row = QHBoxLayout()
        for label, slot in [
            ("CQ", self._on_btn_cq),
            ("RST", self._on_btn_rst),
            ("R+RST", self._on_btn_rrst),
            ("RR73", self._on_btn_rr73),
            ("73", self._on_btn_73),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)

        btn_row.addWidget(QLabel(_("TX Slot:")))
        self._tx_slot_combo = QComboBox()
        self._tx_slot_combo.addItem(_("Auto"), "auto")
        self._tx_slot_combo.addItem(_("Even"), "even")
        self._tx_slot_combo.addItem(_("Odd"), "odd")
        self._tx_slot_combo.setCurrentIndex(
            max(0, self._tx_slot_combo.findData(self._tx_slot_mode))
        )
        self._tx_slot_combo.currentIndexChanged.connect(self._on_tx_slot_mode_changed)
        self._tx_slot_combo.setToolTip(
            _(
                "Which 6s slot to transmit in when calling CQ / enabling TX.\n"
                "Auto: whichever slot is current when you press CQ / TX Enable.\n"
                "Responding to a decoded CQ always uses the correct opposite\n"
                "slot regardless of this setting."
            )
        )
        btn_row.addWidget(self._tx_slot_combo)

        btn_row.addStretch()

        self._tx_enable_btn = QPushButton(_("TX Enable"))
        self._tx_enable_btn.setCheckable(True)
        self._tx_enable_btn.setStyleSheet(
            "QPushButton:checked{background:#006600;color:white;font-weight:bold;}"
        )
        self._tx_enable_btn.toggled.connect(self._on_tx_enable_toggled)
        btn_row.addWidget(self._tx_enable_btn)

        self._halt_btn = QPushButton(_("Halt TX"))
        self._halt_btn.clicked.connect(self._on_halt)
        self._halt_btn.setStyleSheet("QPushButton{color:#cc3300;}")
        btn_row.addWidget(self._halt_btn)

        tx_lay.addLayout(btn_row)

        # TX message line
        tx_msg_row = QHBoxLayout()
        tx_msg_row.addWidget(QLabel(_("TX:")))
        self._tx_edit = QLineEdit()
        self._tx_edit.setPlaceholderText(_("FT4 message (auto-filled by state machine)"))
        tx_msg_row.addWidget(self._tx_edit, stretch=1)
        tx_lay.addLayout(tx_msg_row)

        # QSO state row
        qso_row = QHBoxLayout()
        self._qso_label = QLabel(_("State: IDLE"))
        self._qso_label.setStyleSheet("font-weight:bold;")
        qso_row.addWidget(self._qso_label)

        qso_row.addStretch()

        self._clear_btn = QPushButton(_("Clear"))
        self._clear_btn.clicked.connect(self._on_clear_qso)
        qso_row.addWidget(self._clear_btn)

        self._log_btn = QPushButton(_("Log QSO"))
        self._log_btn.setEnabled(False)
        self._log_btn.clicked.connect(self._on_log_qso)
        qso_row.addWidget(self._log_btn)

        self._adif_btn = QPushButton(_("Export ADIF…"))
        self._adif_btn.clicked.connect(self._on_export_adif)
        qso_row.addWidget(self._adif_btn)

        tx_lay.addLayout(qso_row)
        root.addWidget(tx_grp)

        # -- Separator --
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # -- Decoded messages table (expands to fill space) --
        root.addWidget(QLabel(_("Decoded Messages")))
        self._table = QTableWidget(0, _COL_COUNT)
        self._table.setHorizontalHeaderLabels([_("UTC"), _("dB"), _("DT"), _("Hz"), _("Message")])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(_COL_UTC, 70)
        self._table.setColumnWidth(_COL_DB, 46)
        self._table.setColumnWidth(_COL_DT, 46)
        self._table.setColumnWidth(_COL_FREQ, 56)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setFont(QFont("Courier New", 9))
        self._table.cellDoubleClicked.connect(self._on_message_double_clicked)
        root.addWidget(self._table, stretch=1)

        # -- Bottom row --
        bot_row = QHBoxLayout()
        _clr = QPushButton(_("Clear"))
        _clr.clicked.connect(self._table.clearContents)
        _clr.clicked.connect(lambda: self._table.setRowCount(0))
        bot_row.addWidget(_clr)
        self._log_count_label = QLabel("")
        bot_row.addWidget(self._log_count_label)
        bot_row.addStretch()
        self._status_label = QLabel("")
        bot_row.addWidget(self._status_label)
        root.addLayout(bot_row)

        self._refresh_log_count()

    def _connect_rig_signals(self) -> None:
        rc = self._radio_control
        for sig_name in ("rig_connected", "rig1_connected"):
            sig = getattr(rc, sig_name, None)
            if sig is not None:
                sig.connect(self._on_rig_connected)
        for sig_name in ("rig_disconnected", "rig1_disconnected"):
            sig = getattr(rc, sig_name, None)
            if sig is not None:
                sig.connect(self._on_rig_disconnected)
        # Reflect current connection state at tab-open time
        rig = getattr(rc, "_rig1", None)
        already_connected = rig is not None and getattr(rig, "is_connected", False)
        self._refresh_input_source(already_connected)

    # ------------------------------------------------------------------ #
    # Settings persistence                                                 #
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_FT4_SETTINGS_KEY,)
        ).fetchone()
        if row:
            data = json.loads(row[0])
            self._my_call = data.get("my_call", "")
            self._my_grid = data.get("my_grid", "")
            self._audio_freq = float(data.get("audio_freq_hz", _DEFAULT_AUDIO_FREQ))
            self._rx_source = data.get("rx_source", "soundcard")
            self._tx_slot_mode = data.get("tx_slot_mode", "auto")
        # Fall back to global callsign / grid from Set QTH if not yet set per-tab
        if not self._my_call:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'callsign'"
            ).fetchone()
            self._my_call = str(r[0]) if r else ""
        if not self._my_grid:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'grid_locator'"
            ).fetchone()
            self._my_grid = str(r[0]) if r else ""
        # Load soundcard device indices from shared soundcard_settings
        row2 = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if row2:
            sc = json.loads(row2[0])
            val_in = sc.get("input_device_index")
            val_out = sc.get("output_device_index")
            self._in_device = int(val_in) if val_in is not None else None
            self._out_device = int(val_out) if val_out is not None else None

    def _save_settings(self) -> None:
        data = json.dumps(
            {
                "my_call": self._my_call,
                "my_grid": self._my_grid,
                "audio_freq_hz": self._audio_freq,
                "rx_source": self._rx_source,
                "tx_slot_mode": self._tx_slot_mode,
            }
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_FT4_SETTINGS_KEY, data),
        )
        self._conn.commit()

    def _ensure_table(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ft4_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                qso_date      TEXT    NOT NULL,
                time_on       TEXT    NOT NULL,
                time_off      TEXT,
                call          TEXT    NOT NULL,
                gridsquare    TEXT,
                rst_sent      TEXT,
                rst_rcvd      TEXT,
                freq_hz       INTEGER,
                norad_cat_id  INTEGER,
                sat_name      TEXT
            )"""
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Codec status                                                         #
    # ------------------------------------------------------------------ #

    def _refresh_codec_status(self) -> None:
        if not self._codec.is_available:
            lib_dir = _(
                "Use Help > ft8lib Installation… (Recommended), or build "
                "ft8_lib (github.com/kgoba/ft8_lib) manually and place the "
                "shared library in {dir}"
            ).format(dir=get_user_ft8lib_dir())
            self._codec_banner.setText(
                _("ft8lib is not installed — FT4 TX/RX is disabled.\n") + lib_dir
            )
            self._codec_banner.setStyleSheet("background:#e74c3c;color:white;padding:4px;")
            self._codec_banner.setVisible(True)
            self._tx_enable_btn.setEnabled(False)
        elif not self._codec.decode_available:
            self._codec_banner.setText(
                _(
                    "ft8lib found but decode API is unavailable — TX only.\n"
                    "Update ft8_lib to ≥ v0.4 for RX decode support."
                )
            )
            self._codec_banner.setStyleSheet("background:#f39c12;color:white;padding:4px;")
            self._codec_banner.setVisible(True)
        elif self._codec.decode_backend != "wsjtx":
            self._codec_banner.setText(
                _(
                    "Using lightweight FT4 decoder — may miss weak or overlapping "
                    "stations in a crowded pass.\n"
                    "Help > FT4 Enhanced Decoder Installation… for WSJT-X-grade decoding."
                )
            )
            self._codec_banner.setStyleSheet("background:#3498db;color:white;padding:4px;")
            self._codec_banner.setVisible(True)
        else:
            self._codec_banner.setVisible(False)

    # ------------------------------------------------------------------ #
    # QSO helpers                                                          #
    # ------------------------------------------------------------------ #

    def _get_qso_manager(self) -> Ft4QsoManager | None:
        call = self._my_call.strip()
        grid = self._my_grid.strip()
        if not call:
            self._status_label.setText(_("Set My Call before operating"))
            return None
        if self._qso is None or self._qso._my_call != call.upper():
            self._qso = Ft4QsoManager(call, grid)
        return self._qso

    def _update_qso_display(self) -> None:
        qso = self._qso
        if qso is None or qso.state == QsoState.IDLE:
            self._qso_label.setText(_("State: IDLE"))
            self._log_btn.setEnabled(False)
            return
        sess = qso.session
        state_str = qso.state.name
        self._qso_label.setText(
            f"{sess.their_call}  [{state_str}]  "
            f"Sent: {sess.rst_sent or '—'}  Rcvd: {sess.rst_rcvd or '—'}"
        )
        self._log_btn.setEnabled(qso.state == QsoState.LOGGED)

    # ------------------------------------------------------------------ #
    # Rig / audio                                                          #
    # ------------------------------------------------------------------ #

    def _rig1(self) -> Any:
        """Return the Rig 1 controller, or None."""
        return getattr(self._radio_control, "_rig1", None)

    # ------------------------------------------------------------------ #
    # SDR audio connection                                                #
    # ------------------------------------------------------------------ #

    def _connect_sdr_audio(self) -> None:
        """Connect to SDR pipeline audio_ready signal if available."""
        if self._radio_control is None:
            return
        try:
            sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
            if sdr_ctrl is None:
                return
            pipeline = getattr(sdr_ctrl, "_pipeline", None)
            if pipeline is None:
                return
            pipeline.audio_ready.connect(self._on_sdr_audio_chunk)
            self._sdr_pipeline = pipeline
            self._sdr_connected = True
        except Exception:
            pass

    def _disconnect_sdr_audio(self) -> None:
        if self._sdr_pipeline is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.audio_ready.disconnect(self._on_sdr_audio_chunk)
            self._sdr_pipeline = None
        self._sdr_connected = False

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """Re-subscribe after MainWindow attaches a new SDR pipeline (reconnect).

        See CwTab.refresh_sdr_pipeline() for the full rationale (GitHub
        Issue #12 follow-up) — MainWindow creates a brand-new SDRPipeline
        on every Rig 1/2 (re)connect, and this tab's own reference
        (subscribed once at __init__) would otherwise silently stop
        receiving audio_ready after any later SDR reconnect. Safe to call
        unconditionally: _on_sdr_audio_chunk() already gates on
        self._rx_source == "sdr", so re-subscribing while on Soundcard
        input is harmless.
        """
        self._disconnect_sdr_audio()
        self._connect_sdr_audio()

    @Slot(object)
    def _on_sdr_audio_chunk(self, chunk: NDArray[np.float32]) -> None:
        if self._rx_source != "sdr":
            return
        chunk = chunk.astype(np.float32)
        self._rx_capture.push_audio(chunk)
        self._emit_level(chunk)

    # ------------------------------------------------------------------ #
    # Sounddevice audio capture                                            #
    # ------------------------------------------------------------------ #

    def _start_audio_capture(self) -> None:
        """Subscribe to shared soundcard RX audio for decode accumulation."""
        if self._audio_active:
            return
        try:
            import sounddevice as sd  # noqa: F401 — validate availability
        except ImportError:
            self._status_label.setText(_("sounddevice not installed — pip install sounddevice"))
            return
        if self._in_device is None:
            self._status_label.setText(
                _("Sound Card not configured — open Rig Settings > Sound Card")
            )
            return
        try:
            get_audio_device_manager().acquire_input(
                _AUDIO_OWNER, self._in_device, SAMPLE_RATE, self._audio_callback
            )
            self._audio_active = True
        except Exception as exc:
            self._status_label.setText("Audio Open Error")
            self._status_label.setToolTip(str(exc))
            self._audio_active = False

    def _stop_audio_capture(self) -> None:
        if self._audio_active:
            get_audio_device_manager().release_input(_AUDIO_OWNER, self._in_device)
            self._audio_active = False
            self._level_bar.setValue(0)
            self._level_bar.setToolTip(_("-- dBFS"))

    def _audio_callback(self, chunk: NDArray[np.float32]) -> None:
        self._rx_capture.push_audio(chunk)
        self._emit_level(chunk)

    def _emit_level(self, chunk: NDArray[np.float32]) -> None:
        """Runs on the audio callback thread (soundcard or SDR) — keep this fast."""
        if len(chunk) == 0:
            return
        now = time.monotonic()
        if now - self._last_level_emit < self._LEVEL_MIN_INTERVAL_S:
            return
        self._last_level_emit = now
        peak = float(np.max(np.abs(chunk)))
        dbfs = 20.0 * math.log10(max(peak, 1e-6))
        self.level_updated.emit(dbfs)

    @Slot(float)
    def _on_level_updated(self, dbfs: float) -> None:
        pct = max(0.0, min(100.0, (dbfs + 60.0) / 60.0 * 100.0))
        self._level_bar.setValue(int(pct))
        color = "#2ecc71" if dbfs < -12.0 else ("#f1c40f" if dbfs < -3.0 else "#e74c3c")
        self._level_bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")
        self._level_bar.setToolTip(_("{db:.0f} dBFS").format(db=dbfs))

    @Slot()
    def _on_show_waterfall(self) -> None:
        if self._waterfall_dialog is None:
            self._waterfall_dialog = Ft4WaterfallDialog(self)
        self._waterfall_dialog.show()
        self._waterfall_dialog.raise_()
        self._waterfall_dialog.activateWindow()

    # ------------------------------------------------------------------ #
    # Scheduler slots                                                      #
    # ------------------------------------------------------------------ #

    @Slot(bool, float)
    def _on_period_tick(self, is_tx: bool, seconds_remaining: float) -> None:
        self._countdown_label.setText(f"{seconds_remaining:.1f} s / 6")
        if is_tx:
            self._rx_indicator.setStyleSheet("color:gray;font-weight:bold;")
            self._tx_indicator.setStyleSheet("color:#e74c3c;font-weight:bold;")
        else:
            self._rx_indicator.setStyleSheet("color:#00cc44;font-weight:bold;")
            self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")

    @Slot(bool)
    def _on_period_changed(self, is_tx: bool) -> None:
        """Only drives the TX-turn decision now — RX audio capture and
        decode triggering live entirely in Ft4RxCaptureWorker, which is not
        tied to TX/RX slot parity at all (every slot is captured and
        decoded regardless, see Ft4Scheduler's module docstring)."""
        if is_tx and self._tx_enabled and not self._tx_in_progress:
            self._transmit_now()

    def _on_capture_period(self, audio: NDArray[np.float32]) -> None:
        """Called from Ft4RxCaptureWorker's own background thread — NOT the
        Qt main thread — once per completed 6s period. Must not touch Qt
        widgets directly; anything that does (the waterfall, in the skip
        cases below) goes through a Signal so Qt marshals it onto the main
        thread automatically. Decoding itself is further backgrounded onto
        its own thread (_RxDecodeWorker) since it can take a large fraction
        of a 6s period and this thread must stay free to keep waking up
        exactly on time for the next period (2026-07-10).
        """
        if not self._codec.decode_available:
            self.period_skipped.emit(audio)
            return
        if self._decode_busy:
            # Previous period's decode hasn't finished yet. libft4wsjt's C
            # bridge is not reentrant, so this period's decode is dropped
            # rather than risking an overlapping call — but the waterfall
            # still updates so the gap is visible instead of silently stale.
            get_ft4_decode_logger().info(
                "decode SKIPPED (previous decode still running) audio_len=%.2fs",
                len(audio) / SAMPLE_RATE,
            )
            self.period_skipped.emit(audio)
            return
        self._decode_busy = True
        worker = _RxDecodeWorker(self._codec, audio, self._my_call)
        worker.done.connect(self._on_decode_done)
        thread = threading.Thread(target=worker.run, daemon=True)
        self._decode_thread = thread
        thread.start()

    @Slot(object)
    def _on_period_skipped(self, audio: NDArray[np.float32]) -> None:
        self._update_waterfall_only(audio, [])

    def _update_waterfall_only(
        self, audio: NDArray[np.float32], messages: list[Ft4Message]
    ) -> None:
        if self._waterfall_dialog is not None and self._waterfall_dialog.isVisible():
            self._waterfall_dialog.update_waterfall(audio, messages)

    @Slot(object, object)
    def _on_decode_done(self, messages: list[Ft4Message], audio: NDArray[np.float32]) -> None:
        self._decode_busy = False
        if messages:
            self._display_decoded(messages)
        self._update_waterfall_only(audio, messages)

    # ------------------------------------------------------------------ #
    # Transmit path                                                        #
    # ------------------------------------------------------------------ #

    def _transmit_now(self) -> None:
        """Start TX in a daemon thread."""
        if not self._codec.is_available:
            return
        msg = self._tx_edit.text().strip().upper()
        if not msg:
            qso = self._qso
            if qso is not None:
                msg = qso.pending_tx
        if not msg:
            return

        try:
            audio_freq = float(self._audio_freq_edit.text())
        except ValueError:
            audio_freq = _DEFAULT_AUDIO_FREQ

        audio = self._codec.encode_audio(msg, base_freq=audio_freq)
        if audio is None:
            self._status_label.setText(_("Invalid FT4 message: ") + msg)
            return

        rig = self._rig1()
        worker = _TxWorker(audio, self._out_device, rig)
        worker.finished.connect(self._on_tx_finished)
        worker.error.connect(self._on_tx_error)

        self._tx_in_progress = True
        t = threading.Thread(target=worker.run, daemon=True)
        self._tx_thread = t
        t.start()
        self._status_label.setText(_("TX: ") + msg)

    @Slot()
    def _on_tx_finished(self) -> None:
        self._tx_in_progress = False
        self._status_label.setText(_("TX done — waiting for next period"))

    @Slot(str)
    def _on_tx_error(self, msg: str) -> None:
        self._tx_in_progress = False
        self._status_label.setText(_("TX error: ") + msg)

    # ------------------------------------------------------------------ #
    # Decoded messages display                                             #
    # ------------------------------------------------------------------ #

    def _display_decoded(self, messages: list[Ft4Message]) -> None:
        utc_str = datetime.now(UTC).strftime("%H%M")
        for msg in messages:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, _COL_UTC, QTableWidgetItem(utc_str))
            self._table.setItem(row, _COL_DB, QTableWidgetItem(f"{msg.snr_db:+.0f}"))
            self._table.setItem(row, _COL_DT, QTableWidgetItem(f"{msg.dt_sec:+.1f}"))
            self._table.setItem(row, _COL_FREQ, QTableWidgetItem(f"{msg.freq_hz:.0f}"))
            self._table.setItem(row, _COL_MSG, QTableWidgetItem(msg.text))
            # Highlight if message addressed to us
            if self._my_call and self._my_call.upper() in msg.text.upper():
                for c in range(_COL_COUNT):
                    item = self._table.item(row, c)
                    if item is not None:
                        item.setBackground(Qt.GlobalColor.yellow)
            self._table.scrollToBottom()

        # Auto-advance QSO state machine
        qso = self._qso
        if qso is not None and qso.state not in (QsoState.IDLE, QsoState.LOGGED):
            for msg in messages:
                next_tx = qso.advance(msg.text, their_snr=msg.snr_db)
                if next_tx is not None:
                    self._tx_edit.setText(next_tx)
                    self._update_qso_display()
                    break

    @Slot(int, int)
    def _on_message_double_clicked(self, row: int, _col: int) -> None:
        item = self._table.item(row, _COL_MSG)
        if item is None:
            return
        text = item.text()
        words = text.upper().split()
        qso = self._get_qso_manager()
        if qso is None:
            return

        if words[0] == "CQ" and len(words) >= 2:
            their_call, their_grid = _parse_cq_call_grid(words[1:])
            reply = qso.respond_to(their_call, their_grid)
            self._tx_edit.setText(reply)
            self._update_qso_display()
            # Start scheduler with opposite slot (responding station takes the other half)
            _is_even, _pos = Ft4Scheduler.current_slot_info()
            # CQ station is in current slot → respond in opposite
            self._start_scheduler(tx_even=not _is_even)
        else:
            # Let the state machine interpret the message
            next_tx = qso.advance(text)
            if next_tx is not None:
                self._tx_edit.setText(next_tx)
                self._update_qso_display()

    # ------------------------------------------------------------------ #
    # TX quick buttons                                                     #
    # ------------------------------------------------------------------ #

    def _on_btn_cq(self) -> None:
        qso = self._get_qso_manager()
        if qso is None:
            return
        msg = qso.start_cq()
        self._tx_edit.setText(msg)
        self._update_qso_display()
        is_even, _pos = Ft4Scheduler.current_slot_info()
        self._start_scheduler(tx_even=self._resolve_tx_even(is_even))

    def _on_btn_rst(self) -> None:
        call = self._my_call.strip().upper()
        qso = self._qso
        their = qso.session.their_call if qso else ""
        if their:
            self._tx_edit.setText(f"{their} {call} -05")

    def _on_btn_rrst(self) -> None:
        call = self._my_call.strip().upper()
        qso = self._qso
        their = qso.session.their_call if qso else ""
        if their:
            self._tx_edit.setText(f"{their} {call} R-05")

    def _on_btn_rr73(self) -> None:
        call = self._my_call.strip().upper()
        qso = self._qso
        their = qso.session.their_call if qso else ""
        if their:
            self._tx_edit.setText(f"{their} {call} RR73")

    def _on_btn_73(self) -> None:
        call = self._my_call.strip().upper()
        qso = self._qso
        their = qso.session.their_call if qso else ""
        if their:
            self._tx_edit.setText(f"{their} {call} 73")

    # ------------------------------------------------------------------ #
    # QSO log / clear                                                      #
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_log_qso(self) -> None:
        qso = self._qso
        if qso is None:
            return
        # Attach satellite info from radio control
        norad_text = getattr(self._radio_control, "_norad_label", None)
        sat_text = getattr(self._radio_control, "_sat_name_label", None)
        try:
            qso.session.norad_cat_id = int(norad_text.text()) if norad_text else None
        except (ValueError, AttributeError):
            qso.session.norad_cat_id = None
        try:
            qso.session.sat_name = sat_text.text() if sat_text else ""
        except AttributeError:
            qso.session.sat_name = ""
        qso.log_qso(self._conn)
        self._refresh_log_count()
        self._on_clear_qso()

    @Slot()
    def _on_clear_qso(self) -> None:
        if self._qso is not None:
            self._qso.clear()
        self._tx_edit.clear()
        self._update_qso_display()

    # ------------------------------------------------------------------ #
    # Settings change handlers                                             #
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_settings_changed(self) -> None:
        self._my_call = self._call_edit.text().upper().strip()
        self._my_grid = self._grid_edit.text().upper().strip()
        with contextlib.suppress(ValueError):
            self._audio_freq = float(self._audio_freq_edit.text())
        self._qso = None  # reset manager so it picks up new callsign
        self._save_settings()

    @Slot(int)
    def _on_rx_source_changed(self, _idx: int) -> None:
        self._rx_source = self._rx_src_combo.currentData()
        if self._rx_source != "sdr":
            self._start_audio_capture()
        self._save_settings()

    @Slot(int)
    def _on_tx_slot_mode_changed(self, _idx: int) -> None:
        self._tx_slot_mode = self._tx_slot_combo.currentData()
        self._save_settings()

    def _resolve_tx_even(self, auto_is_even: bool) -> bool:
        """Apply the user's manual TX Slot choice, if any, over the current slot.

        Only used when *we* initiate a CQ / TX Enable — responding to a
        decoded CQ always transmits in the opposite slot from the caller
        regardless of this setting, since that is dictated by the protocol.
        """
        if self._tx_slot_mode == "even":
            return True
        if self._tx_slot_mode == "odd":
            return False
        return auto_is_even

    # ------------------------------------------------------------------ #
    # TX Enable / Halt                                                     #
    # ------------------------------------------------------------------ #

    @Slot(bool)
    def _on_tx_enable_toggled(self, checked: bool) -> None:
        self._tx_enabled = checked
        if checked:
            if not self._codec.is_available:
                self._tx_enable_btn.setChecked(False)
                return
            if not self._my_call.strip():
                self._status_label.setText(_("Set My Call before enabling TX"))
                self._tx_enable_btn.setChecked(False)
                return
            if not self._scheduler._running:
                is_even, _pos = Ft4Scheduler.current_slot_info()
                self._start_scheduler(tx_even=self._resolve_tx_even(is_even))
            self._status_label.setText(_("TX enabled — waiting for next period"))
        else:
            self._status_label.setText(_("TX disabled"))

    @Slot()
    def _on_halt(self) -> None:
        self._tx_enabled = False
        self._tx_enable_btn.setChecked(False)
        self._status_label.setText(_("TX halted"))

    # ------------------------------------------------------------------ #
    # Rig connected/disconnected                                           #
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_rig_connected(self) -> None:
        self._refresh_input_source(connected=True)
        self._status_label.setText(_("Rig connected — ready"))
        # Re-read soundcard settings in case they were updated
        self._load_settings()

    @Slot()
    def _on_rig_disconnected(self) -> None:
        self._on_halt()
        self._stop_audio_capture()
        self._scheduler.stop()
        self._rx_capture.stop()
        self._refresh_input_source(connected=False)
        self._status_label.setText(_("Rig disconnected"))

    def _refresh_input_source(self, connected: bool) -> None:
        """Update the input-source label text and colour (matches APRS/SSTV style).

        This reflects Rig 1's CAT connection only — soundcard RX capture does
        not depend on it (see _start_audio_capture), so the label must not
        claim audio is unavailable when it may in fact be flowing fine.
        """
        if connected:
            self._input_banner.setText(_("Input: Rig connected"))
            self._input_banner.setStyleSheet("color: #4caf50;")
        else:
            self._input_banner.setText(_("Input: Rig not connected"))
            self._input_banner.setStyleSheet("color: #f44336;")

    # ------------------------------------------------------------------ #
    # Scheduler start helper                                               #
    # ------------------------------------------------------------------ #

    def _start_scheduler(self, tx_even: bool) -> None:
        if not self._scheduler._running:
            self._scheduler.start(tx_even=tx_even)
        else:
            self._scheduler.set_tx_even(tx_even)
        # RX capture's lifecycle is tied to the scheduler's — both should be
        # running or stopped together (see _on_rig_disconnected/closeEvent).
        # start() is a no-op if already running.
        self._rx_capture.start()

    # ------------------------------------------------------------------ #
    # Log count / ADIF export                                              #
    # ------------------------------------------------------------------ #

    def _refresh_log_count(self) -> None:
        row = self._conn.execute("SELECT COUNT(*) FROM ft4_log").fetchone()
        n = row[0] if row else 0
        self._log_count_label.setText(_("QSOs logged: ") + str(n))

    @Slot()
    def _on_export_adif(self) -> None:
        from ui.log_export_dialog import LogExportDialog

        dlg = LogExportDialog(self._conn, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------ #
    # Cleanup on tab close                                                 #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._on_halt()
        if self._tx_in_progress and self._tx_thread is not None:
            # _TxWorker.run() only releases PTT after sd.wait() returns,
            # which can block for the whole ~5s FT4 audio duration.
            # sd.stop() ends that wait almost immediately so the worker's
            # own PTT-off / audio-lock-release code runs promptly instead
            # of racing interpreter shutdown, which can kill a still-
            # running daemon thread before it gets there and leave the rig
            # keyed indefinitely.
            with contextlib.suppress(Exception):
                import sounddevice as sd

                sd.stop()
            self._tx_thread.join(timeout=2.0)
            if self._tx_in_progress:
                # Thread still didn't finish — force PTT off directly as a
                # last-resort safety net so the rig can't stay keyed.
                rig = self._rig1()
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.set_ptt(False)
        self._stop_audio_capture()
        self._disconnect_sdr_audio()
        self._scheduler.stop()
        self._rx_capture.stop()
        if self._waterfall_dialog is not None:
            self._waterfall_dialog.close()
        super().closeEvent(event)
