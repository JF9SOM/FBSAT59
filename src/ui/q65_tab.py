"""Communications > Q65 tab — EME weak-signal digital mode (RX + TX).

Phase 1: Q65 decoding via libq65 shared library.
Phase 2: Q65 encoding (pure Python) + QSO state machine + PTT control.

TX requires ft8lib (same as FT4) and a connected rig.
libq65 is only needed for RX decoding; TX works without it.

Tab is non-resident: opened via Communications > Q65, closed with x.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager
from comms.q65.codec import (
    Q65_PERIODS,
    Q65_SUBMODE,
    SAMPLE_RATE,
    Q65Codec,
    Q65Message,
    is_available,
)
from comms.q65.qso import Q65QsoManager, Q65QsoState, format_report
from comms.q65.scheduler import Q65Scheduler
from core.clock_offset import corrected_time
from i18n import _

# ---------------------------------------------------------------------------
# Table column indices
# ---------------------------------------------------------------------------
_COL_UTC = 0
_COL_DB = 1
_COL_DT = 2
_COL_FREQ = 3
_COL_MSG = 4
_COL_COUNT = 5
_AUDIO_OWNER = "Q65"
# ~20ms @ 12000 Hz — bounds the worst-case delay before a TX Level slider
# change takes effect during an active transmission (GitHub Issue #16).
_TX_BLOCK_SIZE = 240

_Q65_SETTINGS_KEY = "q65_settings"

_MODE_PRESETS: list[str] = [
    "Q65-60A",  # 144 MHz EME standard
    "Q65-60B",
    "Q65-30B",  # 50/70 MHz EME
    "Q65-30C",
    "Q65-15C",
    "Q65-15D",
    "Q65-15E",
]

_TX_SLOT_EVEN = "EVEN"
_TX_SLOT_ODD = "ODD"


class Q65Tab(QWidget):
    """Non-resident Q65 RX+TX tab.

    Args:
        conn:          SQLite DB connection (shared).
        radio_control: RadioControlWidget for SDR audio and PTT access.
        parent:        Parent widget.
    """

    _decoded_signal: Signal = Signal(str, list)

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: object = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control

        self._scheduler = Q65Scheduler(period_seconds=60)
        self._audio_buffer: list[NDArray[np.float32]] = []
        self._buffer_lock = threading.Lock()
        self._sdr_connected = False
        self._sdr_pipeline: Any | None = None
        self._last_period_start: float = 0.0

        # TX state
        self._tx_active = False
        self._tx_slot: str = _TX_SLOT_EVEN
        self._tx_thread: threading.Thread | None = None
        self._out_device: int | None = None
        self._tx_level_pct: float = 100.0  # % of full-scale TX audio amplitude

        # QSO manager (created after UI so callbacks can update labels)
        self._qso: Q65QsoManager | None = None

        self._build_ui()

        self._qso = Q65QsoManager(
            conn=conn,
            my_call=self._call_edit.text().strip(),
            my_grid=self._grid_edit.text().strip(),
            on_tx_msg=self._on_qso_tx_msg,
            on_state=self._on_qso_state,
        )

        self._decoded_signal.connect(self._on_decoded)
        self._load_settings()
        self._connect_sdr_audio()
        self._connect_rig_signals()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(500)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        self._decode_timer = QTimer(self)
        self._decode_timer.setInterval(200)
        self._decode_timer.timeout.connect(self._check_period_boundary)
        self._decode_timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)

        # Input / rig connection banner (same style as FT4 tab)
        self._input_banner = QLabel(_("Input: No audio source — connect Rig in Radio Control"))
        self._input_banner.setStyleSheet("color: #f44336;")
        root.addWidget(self._input_banner)

        # Lib status banner (shown only when libq65 is not installed)
        if not is_available():
            banner = QLabel(
                _(
                    "libq65 not installed — RX decoding disabled.\n"
                    "TX encoding uses pure Python and works without libq65."
                )
            )
            banner.setStyleSheet("background:#e74c3c;color:white;padding:4px;")
            banner.setWordWrap(True)
            root.addWidget(banner)

        # ---- Config row ----
        cfg = QGroupBox(_("Configuration"))
        cfg_lay = QHBoxLayout(cfg)
        cfg_lay.setSpacing(6)

        cfg_lay.addWidget(QLabel(_("My Call:")))
        self._call_edit = QLineEdit()
        self._call_edit.setMaximumWidth(100)
        self._call_edit.textChanged.connect(self._on_call_changed)
        cfg_lay.addWidget(self._call_edit)

        cfg_lay.addWidget(QLabel(_("Grid:")))
        self._grid_edit = QLineEdit()
        self._grid_edit.setMaximumWidth(70)
        self._grid_edit.textChanged.connect(self._on_call_changed)
        cfg_lay.addWidget(self._grid_edit)

        cfg_lay.addWidget(QLabel(_("Mode:")))
        self._mode_combo = QComboBox()
        for m in _MODE_PRESETS:
            self._mode_combo.addItem(m)
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        cfg_lay.addWidget(self._mode_combo)

        cfg_lay.addWidget(QLabel(_("TX Slot:")))
        self._slot_combo = QComboBox()
        self._slot_combo.addItem(_("Even"))
        self._slot_combo.addItem(_("Odd"))
        cfg_lay.addWidget(self._slot_combo)

        cfg_lay.addWidget(QLabel(_("RX:")))
        self._input_combo = QComboBox()
        self._input_combo.addItem(_("Rig Soundcard"))
        self._input_combo.addItem(_("SDR"))
        cfg_lay.addWidget(self._input_combo)

        cfg_lay.addStretch()
        root.addWidget(cfg)

        # ---- Period / TX status row ----
        status_row = QHBoxLayout()

        self._period_label = QLabel("Q65-60A")
        self._period_label.setStyleSheet("font-weight:bold;font-size:13px;")
        status_row.addWidget(self._period_label)

        self._countdown_label = QLabel("-- s / 60")
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

        # ---- TX control group ----
        tx_grp = QGroupBox(_("Transmit"))
        tx_lay = QVBoxLayout(tx_grp)
        tx_lay.setSpacing(4)

        # Quick buttons row 1
        btn_row1 = QHBoxLayout()
        self._cq_btn = QPushButton(_("CQ"))
        self._cq_btn.setToolTip(_("Start calling CQ"))
        self._cq_btn.clicked.connect(self._on_cq)
        btn_row1.addWidget(self._cq_btn)

        self._rst_btn = QPushButton(_("RST"))
        self._rst_btn.setToolTip(_("Send signal report"))
        self._rst_btn.clicked.connect(lambda: self._on_quick_report(r_prefix=False))
        btn_row1.addWidget(self._rst_btn)

        self._r_rst_btn = QPushButton(_("R+RST"))
        self._r_rst_btn.setToolTip(_("Send R + signal report (confirm received)"))
        self._r_rst_btn.clicked.connect(lambda: self._on_quick_report(r_prefix=True))
        btn_row1.addWidget(self._r_rst_btn)

        self._rr73_btn = QPushButton(_("RR73"))
        self._rr73_btn.clicked.connect(self._on_rr73)
        btn_row1.addWidget(self._rr73_btn)

        self._73_btn = QPushButton(_("73"))
        self._73_btn.clicked.connect(self._on_73)
        btn_row1.addWidget(self._73_btn)

        btn_row1.addStretch()

        self._tx_enable_btn = QPushButton(_("TX Enable"))
        self._tx_enable_btn.setCheckable(True)
        self._tx_enable_btn.toggled.connect(self._on_tx_enable_toggled)
        self._tx_enable_btn.setStyleSheet(
            "QPushButton:checked{background:#006600;color:white;font-weight:bold;}"
        )
        btn_row1.addWidget(self._tx_enable_btn)

        self._halt_btn = QPushButton(_("Halt TX"))
        self._halt_btn.clicked.connect(self._on_halt)
        self._halt_btn.setStyleSheet("QPushButton{color:#cc3300;}")
        btn_row1.addWidget(self._halt_btn)

        tx_lay.addLayout(btn_row1)

        # TX message + free text row
        msg_row = QHBoxLayout()
        msg_row.addWidget(QLabel(_("TX:")))
        self._tx_msg_edit = QLineEdit()
        self._tx_msg_edit.setPlaceholderText(
            _("Message to transmit (auto-filled by state machine)")
        )
        self._tx_msg_edit.setReadOnly(False)
        msg_row.addWidget(self._tx_msg_edit, stretch=1)

        self._send_free_btn = QPushButton(_("Send Once"))
        self._send_free_btn.setToolTip(_("Send the text above as a one-shot free message"))
        self._send_free_btn.clicked.connect(self._on_send_free)
        msg_row.addWidget(self._send_free_btn)

        tx_lay.addLayout(msg_row)

        # QSO status row
        qso_row = QHBoxLayout()
        self._qso_state_label = QLabel(_("State: IDLE"))
        self._qso_state_label.setStyleSheet("font-weight:bold;")
        qso_row.addWidget(self._qso_state_label)

        qso_row.addStretch()

        qso_row.addWidget(QLabel(_("TX Level:")))
        self._tx_level_slider = QSlider(Qt.Orientation.Horizontal)
        self._tx_level_slider.setRange(1, 100)
        self._tx_level_slider.setValue(int(self._tx_level_pct))
        self._tx_level_slider.setFixedWidth(80)
        self._tx_level_slider.setToolTip(
            _(
                # See the matching tooltip in ft4_tab.py for why this avoids
                # "(% of" and "100% here" — xgettext would otherwise treat the
                # string as python-format and msgfmt --check would reject the
                # Japanese translation's literal "%".
                "TX audio output level (percentage of full scale).\n"
                "Lower this if the rig's ALC is triggered or the transmit\n"
                "audio sounds distorted — Q65 audio is generated at full\n"
                "scale and some rigs/sound cards need well under 100%."
            )
        )
        self._tx_level_label = QLabel(f"{int(self._tx_level_pct)}%")
        self._tx_level_label.setFixedWidth(34)
        self._tx_level_slider.valueChanged.connect(self._on_tx_level_changed)
        qso_row.addWidget(self._tx_level_slider)
        qso_row.addWidget(self._tx_level_label)

        self._dx_call_label = QLabel(_("DX: —"))
        qso_row.addWidget(self._dx_call_label)

        self._log_btn = QPushButton(_("Log QSO"))
        self._log_btn.clicked.connect(self._on_log_qso)
        self._log_btn.setEnabled(False)
        qso_row.addWidget(self._log_btn)

        self._adif_btn = QPushButton(_("Export ADIF…"))
        self._adif_btn.clicked.connect(self._on_export_adif)
        qso_row.addWidget(self._adif_btn)

        tx_lay.addLayout(qso_row)
        root.addWidget(tx_grp)

        # ---- Separator ----
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # ---- Decoded messages table ----
        root.addWidget(QLabel(_("Decoded Messages")))
        self._table = QTableWidget(0, _COL_COUNT)
        self._table.setHorizontalHeaderLabels([_("UTC"), _("dB"), _("DT"), _("Hz"), _("Message")])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(_COL_UTC, 80)
        self._table.setColumnWidth(_COL_DB, 50)
        self._table.setColumnWidth(_COL_DT, 50)
        self._table.setColumnWidth(_COL_FREQ, 60)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setFont(QFont("Courier New", 9))
        self._table.cellDoubleClicked.connect(self._on_table_double_click)
        root.addWidget(self._table, stretch=1)

        # ---- Bottom row ----
        bot = QHBoxLayout()
        clr = QPushButton(_("Clear"))
        clr.clicked.connect(self._table.clearContents)
        clr.clicked.connect(lambda: self._table.setRowCount(0))
        bot.addWidget(clr)
        bot.addStretch()
        self._status_label = QLabel(_("Waiting for period boundary…"))
        # Without word wrap, a long error string (e.g. a sound-card
        # exception message) forces QLabel's minimumSizeHint to fit the
        # whole line, widening the window and blocking shrinking it back.
        self._status_label.setWordWrap(True)
        bot.addWidget(self._status_label)
        root.addLayout(bot)

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        import json

        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_Q65_SETTINGS_KEY,)
        ).fetchone()
        d: dict[str, str] = json.loads(row[0]) if row else {}

        callsign = d.get("callsign", "")
        grid = d.get("grid", "")
        # Fall back to global callsign / grid from Set QTH if not yet set per-tab
        if not callsign:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'callsign'"
            ).fetchone()
            callsign = str(r[0]) if r else ""
        if not grid:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'grid_locator'"
            ).fetchone()
            grid = str(r[0]) if r else ""
        if callsign:
            self._call_edit.setText(callsign)
        if grid:
            self._grid_edit.setText(grid)
        if "mode" in d and d["mode"] in _MODE_PRESETS:
            self._mode_combo.setCurrentText(d["mode"])
        if "rx_input" in d:
            idx = self._input_combo.findText(d["rx_input"])
            if idx >= 0:
                self._input_combo.setCurrentIndex(idx)
        if "tx_slot" in d:
            idx = self._slot_combo.findText(d["tx_slot"])
            if idx >= 0:
                self._slot_combo.setCurrentIndex(idx)
        self._tx_level_pct = float(d.get("tx_level_pct", 100.0))
        self._tx_level_slider.setValue(int(self._tx_level_pct))
        self._tx_level_label.setText(f"{int(self._tx_level_pct)}%")

        # Load output device index from shared soundcard_settings
        row2 = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if row2:
            sc = json.loads(row2[0])
            val_out = sc.get("output_device_index")
            self._out_device = int(val_out) if val_out is not None else None

    def _save_settings(self) -> None:
        import json

        data = json.dumps(
            {
                "callsign": self._call_edit.text().strip(),
                "grid": self._grid_edit.text().strip(),
                "mode": self._mode_combo.currentText(),
                "rx_input": self._input_combo.currentText(),
                "tx_slot": self._slot_combo.currentText(),
                "tx_level_pct": self._tx_level_pct,
            }
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_Q65_SETTINGS_KEY, data),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Slot: configuration changes
    # ------------------------------------------------------------------

    def _on_call_changed(self) -> None:
        if self._qso:
            self._qso.set_location(
                self._call_edit.text().strip(),
                self._grid_edit.text().strip(),
            )

    def _on_mode_changed(self, mode: str) -> None:
        period = Q65_PERIODS.get(mode, 60)
        self._scheduler = Q65Scheduler(period_seconds=period)
        self._period_label.setText(mode)
        self._last_period_start = 0.0
        with self._buffer_lock:
            self._audio_buffer.clear()

    def _on_tx_level_changed(self, value: int) -> None:
        self._tx_level_pct = float(value)
        self._tx_level_label.setText(f"{value}%")
        self._save_settings()

    # ------------------------------------------------------------------
    # Rig connection signals
    # ------------------------------------------------------------------

    def _connect_rig_signals(self) -> None:
        rc = self._radio_control
        if rc is None:
            return
        for sig_name in ("rig_connected", "rig1_connected"):
            sig = getattr(rc, sig_name, None)
            if sig is not None:
                sig.connect(self._on_rig_connected)
        for sig_name in ("rig_disconnected", "rig1_disconnected"):
            sig = getattr(rc, sig_name, None)
            if sig is not None:
                sig.connect(self._on_rig_disconnected)
        rig = getattr(rc, "_rig1", None)
        already_connected = rig is not None and getattr(rig, "_connected", False)
        self._refresh_input_source(already_connected)

    @Slot()
    def _on_rig_connected(self) -> None:
        self._refresh_input_source(connected=True)

    @Slot()
    def _on_rig_disconnected(self) -> None:
        self._refresh_input_source(connected=False)

    def _refresh_input_source(self, connected: bool) -> None:
        if connected:
            self._input_banner.setText(_("Input: Rig connected"))
            self._input_banner.setStyleSheet("color: #4caf50;")
        else:
            self._input_banner.setText(_("Input: No audio source — connect Rig in Radio Control"))
            self._input_banner.setStyleSheet("color: #f44336;")

    # SDR audio connection
    # ------------------------------------------------------------------

    def _connect_sdr_audio(self) -> None:
        if self._radio_control is None:
            return
        try:
            sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
            if sdr_ctrl is None:
                return
            pipeline = getattr(sdr_ctrl, "_pipeline", None)
            if pipeline is None:
                return
            pipeline.audio_ready.connect(self._on_audio_chunk)
            # Without this, the pipeline never actually demodulates/emits
            # audio_ready unless the user separately presses "Start Audio"
            # in SDR Control — an easy-to-miss, unrelated-looking button in
            # a different tab (GitHub Issue #12 follow-up).
            pipeline.request_audio(_AUDIO_OWNER)
            self._sdr_pipeline = pipeline
            self._sdr_connected = True
        except Exception:
            pass

    def _disconnect_sdr_audio(self) -> None:
        if self._sdr_pipeline is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.release_audio(_AUDIO_OWNER)
            with contextlib.suppress(Exception):
                self._sdr_pipeline.audio_ready.disconnect(self._on_audio_chunk)
            self._sdr_pipeline = None
        self._sdr_connected = False

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """Re-subscribe after MainWindow attaches a new SDR pipeline (reconnect).

        See CwTab.refresh_sdr_pipeline() for the full rationale (GitHub
        Issue #12 follow-up) — MainWindow creates a brand-new SDRPipeline
        on every Rig 1/2 (re)connect, and this tab's own reference
        (subscribed once at __init__) would otherwise silently stop
        receiving audio_ready after any later SDR reconnect. Safe to call
        unconditionally: _on_audio_chunk() already gates on the Input
        combo being set to "SDR", so re-subscribing while on another
        input is harmless.
        """
        self._disconnect_sdr_audio()
        self._connect_sdr_audio()

    @Slot(object)
    def _on_audio_chunk(self, chunk: NDArray[np.float32]) -> None:
        if self._input_combo.currentText() != _("SDR"):
            return
        with self._buffer_lock:
            self._audio_buffer.append(chunk.astype(np.float32))

    # ------------------------------------------------------------------
    # Period boundary — decode and TX scheduling
    # ------------------------------------------------------------------

    @Slot()
    def _on_tick(self) -> None:
        self._countdown_label.setText(self._scheduler.countdown_str())

    @Slot()
    def _check_period_boundary(self) -> None:
        now = corrected_time()
        phase = self._scheduler.period_phase()
        period = self._scheduler.period_seconds

        if phase > 0.4:
            return
        boundary = now - phase
        if boundary <= self._last_period_start:
            return
        self._last_period_start = boundary

        # Determine if this boundary falls on an even or odd period
        period_index = int(boundary / period)
        is_even_period = (period_index % 2) == 0
        my_slot = self._slot_combo.currentText().lower()
        is_my_tx_slot = (my_slot == "even" and is_even_period) or (
            my_slot == "odd" and not is_even_period
        )

        # Start TX if enabled and it's our slot
        if self._tx_enable_btn.isChecked() and is_my_tx_slot and self._qso:
            msg = self._qso.consume_tx_message()
            if msg:
                self._do_tx(msg, period)
                return  # skip decode during TX slot

        # Collect audio for decode
        with self._buffer_lock:
            chunks = list(self._audio_buffer)
            self._audio_buffer.clear()

        if not is_available() or not chunks:
            if not chunks:
                self._status_label.setText(_("No audio in last period"))
            return

        samples = np.concatenate(chunks)
        expected = period * SAMPLE_RATE
        if len(samples) < expected:
            samples = np.concatenate([samples, np.zeros(expected - len(samples), dtype=np.float32)])
        else:
            samples = samples[:expected]

        threading.Thread(
            target=self._decode_thread,
            args=(samples, period, boundary),
            daemon=True,
        ).start()

    def _decode_thread(self, samples: NDArray[np.float32], period: int, boundary: float) -> None:
        mode_str = self._mode_combo.currentText()
        submode = mode_str[-1] if mode_str and mode_str[-1] in Q65_SUBMODE else "A"
        messages = Q65Codec(submode=submode).decode(samples, period_seconds=period)
        utc_str = datetime.fromtimestamp(boundary, UTC).strftime("%H:%M")
        self._decoded_signal.emit(utc_str, messages)

    @Slot(str, list)
    def _on_decoded(self, utc_str: str, messages: list[Q65Message]) -> None:
        if not messages:
            self._status_label.setText(_("Period {utc}: no decodes").format(utc=utc_str))
            return
        self._status_label.setText(
            _("Period {utc}: {n} decode(s)").format(utc=utc_str, n=len(messages))
        )
        my_call = self._call_edit.text().strip().upper()
        for msg in messages:
            self._append_row(utc_str, msg)
            if my_call and my_call in msg.text.upper() and self._qso:
                self._qso.on_decoded(msg.text, msg.snr_db)

    def _append_row(self, utc_str: str, msg: Q65Message) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        def _item(text: str, align: int = 0) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            if align:
                it.setTextAlignment(align)
            return it

        align_r = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        self._table.setItem(row, _COL_UTC, _item(utc_str))
        self._table.setItem(row, _COL_DB, _item(f"{msg.snr_db:+.0f}", align_r))
        self._table.setItem(row, _COL_DT, _item(f"{msg.dt_sec:+.1f}", align_r))
        self._table.setItem(row, _COL_FREQ, _item(str(int(msg.freq_hz)), align_r))
        self._table.setItem(row, _COL_MSG, _item(msg.text))

        if "CQ" in msg.text.upper():
            for col in range(_COL_COUNT):
                item = self._table.item(row, col)
                if item:
                    item.setBackground(QColor("#1a3a1a"))

        self._table.scrollToBottom()

    # ------------------------------------------------------------------
    # Table double-click — respond to decoded message
    # ------------------------------------------------------------------

    def _row_snr_db(self, row: int) -> float | None:
        """Measured SNR of a decoded row, or None if unavailable."""
        item = self._table.item(row, _COL_DB)
        if item is None:
            return None
        try:
            return float(item.text())
        except ValueError:
            return None

    @Slot(int, int)
    def _on_table_double_click(self, row: int, col: int) -> None:
        msg_item = self._table.item(row, _COL_MSG)
        if msg_item is None or self._qso is None:
            return
        text = msg_item.text().strip().upper()
        parts = text.split()
        # Expect "CALL1 CALL2 ..." or "CQ CALL GRID"
        if len(parts) >= 2:
            if parts[0] == "CQ" and len(parts) >= 3:
                dx_call = parts[1]
                dx_grid = parts[2] if len(parts) > 2 else ""
            else:
                dx_call = parts[0]
                dx_grid = ""
            self._qso.call_station(dx_call, dx_grid, self._row_snr_db(row))
            self._tx_enable_btn.setChecked(True)

    # ------------------------------------------------------------------
    # TX buttons
    # ------------------------------------------------------------------

    def _on_cq(self) -> None:
        if self._qso:
            self._qso.start_cq()
        self._tx_enable_btn.setChecked(True)

    def _require_dx_call(self) -> bool:
        """True if there is a station to address; otherwise says why not.

        RST/R+RST/73 used to silently do nothing when dx_call was empty --
        indistinguishable from a dead button (GitHub Issue #16, same class
        of fix as the FT4 tab's _active_qso_for_button()).
        """
        if self._qso is None or not self._qso.dx_call:
            self._status_label.setText(_("Double-click a decoded station first"))
            return False
        return True

    def _current_report(self) -> str:
        """Our measured SNR of dx_call as a report string.

        Falls back to a placeholder only if nothing has been measured yet
        -- GitHub Issue #16: this used to be a fixed "-05" always, even
        when a real reading was available in the decoded table.
        """
        if self._qso is not None and self._qso.their_snr_db is not None:
            return format_report(self._qso.their_snr_db)
        return "-05"

    def _on_quick_report(self, r_prefix: bool) -> None:
        if not self._require_dx_call():
            return
        assert self._qso is not None
        report = self._current_report()
        if r_prefix:
            report = f"R{report}"
        msg = f"{self._qso.dx_call} {self._qso.my_call} {report}"
        self._qso.send_free(msg)
        self._tx_msg_edit.setText(msg)

    def _on_rr73(self) -> None:
        if not self._require_dx_call():
            return
        assert self._qso is not None
        msg = f"{self._qso.dx_call} {self._qso.my_call} RR73"
        self._qso.send_free(msg)
        self._tx_msg_edit.setText(msg)

    def _on_73(self) -> None:
        if not self._require_dx_call():
            return
        assert self._qso is not None
        msg = f"{self._qso.dx_call} {self._qso.my_call} 73"
        self._qso.send_free(msg)
        self._tx_msg_edit.setText(msg)

    def _on_send_free(self) -> None:
        text = self._tx_msg_edit.text().strip()
        if text and self._qso:
            self._qso.send_free(text)
            self._tx_enable_btn.setChecked(True)

    def _on_tx_enable_toggled(self, checked: bool) -> None:
        if self._qso:
            self._qso.tx_enable = checked
        if not checked:
            self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")

    def _on_halt(self) -> None:
        self._tx_enable_btn.setChecked(False)
        if self._qso:
            self._qso.halt()
        self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")

    def _on_log_qso(self) -> None:
        if self._qso:
            self._qso.log_qso_manually()

    def _on_export_adif(self) -> None:
        from ui.log_export_dialog import LogExportDialog

        dlg = LogExportDialog(self._conn, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------
    # QSO state machine callbacks (called from QSO manager)
    # ------------------------------------------------------------------

    def _on_qso_tx_msg(self, msg: str) -> None:
        """Update TX message edit when state machine generates a new message."""
        self._tx_msg_edit.setText(msg)

    def _on_qso_state(self, state: Q65QsoState, tx_msg: str) -> None:
        """Update UI labels when QSO state changes."""
        self._qso_state_label.setText(f"State: {state.name}")
        if tx_msg:
            self._tx_msg_edit.setText(tx_msg)
        if self._qso:
            dx = self._qso.dx_call
            self._dx_call_label.setText(f"DX: {dx}" if dx else "DX: —")
        can_log = state in (Q65QsoState.EXCHANGE, Q65QsoState.CONFIRM, Q65QsoState.LOGGED)
        self._log_btn.setEnabled(can_log)
        if state == Q65QsoState.LOGGED:
            self._tx_enable_btn.setChecked(False)
            self._tx_msg_edit.clear()
            self._status_label.setText(_("QSO logged"))

    # ------------------------------------------------------------------
    # TX execution (runs in background thread)
    # ------------------------------------------------------------------

    def _do_tx(self, msg: str, period: int) -> None:
        """Spawn background thread to encode and transmit one Q65 message."""
        mode_str = self._mode_combo.currentText()
        submode = mode_str[-1] if mode_str and mode_str[-1] in Q65_SUBMODE else "A"

        self._tx_indicator.setStyleSheet("color:red;font-weight:bold;")
        self._status_label.setText(f"TX: {msg}")

        def _run() -> None:
            try:
                from comms.q65.encoder import get_q65_tones, synthesize_audio

                tones = get_q65_tones(msg)
                audio = synthesize_audio(tones, period, submode)
                self._transmit_audio(audio, msg)
            except Exception as exc:
                self._status_label.setText(f"TX error: {exc}")
            finally:
                self._tx_active = False
                self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")

        if self._tx_active:
            return
        self._tx_active = True
        self._tx_thread = threading.Thread(target=_run, daemon=True)
        self._tx_thread.start()

    def _transmit_audio(self, audio: NDArray[np.float32], msg: str) -> None:
        """Play audio via sounddevice with PTT control.

        Streams via a sounddevice.OutputStream callback (rather than a
        single sd.play() call) so the TX Level gain can be re-read every
        block and take effect live during an active transmission, instead
        of only on the next one (GitHub Issue #16). The gain is linearly
        ramped across each block from the previous block's gain to the
        freshly-read value, so a mid-transmission slider move never
        produces an abrupt amplitude step (click) at a block boundary.
        """
        mgr = get_audio_device_manager()
        if not mgr.acquire_output(_AUDIO_OWNER, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            self._status_label.setText(
                _("Sound card output is in use by {other}").format(other=other)
            )
            return

        rig = self._get_rig()

        # PTT ON. freeze_doppler=False: a Q65 transmission runs 15-60 s, so
        # holding the VFOs still for its duration would drag our signal right
        # out of the passband. Keep correcting through TX (GitHub Issue #16).
        if rig is not None:
            with contextlib.suppress(Exception):
                rig.set_ptt(True, freeze_doppler=False)

        try:
            import sounddevice as sd

            n = len(audio)
            idx = 0
            last_gain = self._tx_level_pct / 100.0
            done = threading.Event()

            def _callback(
                outdata: NDArray[np.float32], frames: int, _time: Any, _status: Any
            ) -> None:
                nonlocal idx, last_gain
                remaining = n - idx
                take = min(frames, remaining)
                if take > 0:
                    gain_now = self._tx_level_pct / 100.0
                    ramp = np.linspace(last_gain, gain_now, take, dtype=np.float32)
                    outdata[:take, 0] = audio[idx : idx + take] * ramp
                    last_gain = gain_now
                    idx += take
                if take < frames:
                    outdata[take:, 0] = 0.0
                if remaining <= frames:
                    raise sd.CallbackStop()

            stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                device=self._out_device,
                channels=1,
                dtype="float32",
                blocksize=_TX_BLOCK_SIZE,
                callback=_callback,
                finished_callback=done.set,
            )
            with stream:
                mgr.pin_active_output(_AUDIO_OWNER)
                done.wait()
        except Exception as exc:
            self._status_label.setText(f"Audio error: {exc}")
        finally:
            if rig is not None:
                with contextlib.suppress(Exception):
                    rig.set_ptt(False)
            mgr.release_output(_AUDIO_OWNER, self._out_device)

    def _get_rig(self) -> Any | None:
        """Return RigController for Rig 1 if connected, else None."""
        if self._radio_control is None:
            return None
        try:
            rig = getattr(self._radio_control, "_rig", None)
            if rig is not None and getattr(rig, "is_connected", False):
                return rig
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        self._tick_timer.stop()
        self._decode_timer.stop()
        if self._tx_active and self._tx_thread:
            # A Q65 TX period can run up to 60s (sd.wait() blocks the TX
            # thread for the whole transmission) — waiting that out here
            # would freeze tab/app close. sd.stop() ends the blocking
            # sd.wait() almost immediately, so the TX thread's own finally
            # block (PTT off + audio lock release) runs promptly instead of
            # racing interpreter shutdown, which can kill a still-running
            # daemon thread before its finally block executes and leave the
            # rig keyed indefinitely.
            with contextlib.suppress(Exception):
                import sounddevice as sd

                sd.stop()
            self._tx_thread.join(timeout=2.0)
            if self._tx_active:
                # Thread still didn't finish — force PTT off directly as a
                # last-resort safety net so the rig can't stay keyed.
                rig = self._get_rig()
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.set_ptt(False)
        self._disconnect_sdr_audio()
        self._save_settings()
        super().closeEvent(event)
