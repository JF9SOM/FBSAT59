"""Communications > AX100 Digipeater tab (GreenCube/MARMOTSat-compatible).

GMSK "ASM+Golay" digipeater (Golay(24,12) length field, CCSDS descrambler,
Reed-Solomon(255,223), CSP transport) receiver and transmitter, following
the GreenCube (IO-117) Digipeater Manual's message format. Two input/output
paths, mirroring FT4/Q65/CW Decoder's conventions:

  - Rig + Sound Card (SSB mode, GreenCube's own approach — the GMSK
    baseband rides at a fixed offset inside the SSB passband): both RX
    and TX.
  - SDR: RX only (SDR hardware here cannot transmit).

Not yet validated against a real captured signal (see CLAUDE.md's Phase
0/1 plan). The CSP header field values used for outgoing frames
(source/destination node, ports) are unconfirmed placeholders — see
comms/ax100digi/tx.py's module docstring.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager
from comms.ax100digi.audio_bridge import RxAudioBridge
from comms.ax100digi.csp import CspHeader
from comms.ax100digi.engine import Ax100DigiReceiver, DecodedDigiFrame
from comms.ax100digi.tx import DEFAULT_CSP_HEADER, build_tx_audio
from i18n import _
from ui.adif_utils import adif_default_filename, adif_write_or_append, build_adif_record

_POLL_INTERVAL_MS = 1_000
_MAX_LOG_ROWS = 500
_SOUNDCARD_SAMPLE_RATE = 48_000
_AUDIO_OWNER = "AX100 Digipeater"
_SETTINGS_KEY = "ax100digi_settings"
_PTT_LEAD_S = 0.20  # GreenCube config.ini's KeyUpDelay default (200ms)
_PTT_TAIL_S = 0.50  # GreenCube config.ini's KeyDownDelay default (500ms)
_MAX_CONTENT_HISTORY = 20
_SQUELCH_MAX = 60  # slider range 0 (off) .. 60 -> threshold -60..0 dBFS
_SQUELCH_MIN_DBFS = -60.0


def _peak_dbfs(samples: NDArray[Any]) -> float:
    """Peak level of a real audio or complex I/Q block, in dBFS.

    Works for both input types since np.abs() gives magnitude either way.
    Returns a very low value (effectively -inf) for empty/all-zero input
    so an empty/silent chunk never accidentally passes a squelch check.
    """
    if len(samples) == 0:
        return -999.0
    peak = float(np.max(np.abs(samples)))
    if peak <= 0.0:
        return -999.0
    return 20.0 * float(np.log10(peak))


class _TxWorker(QObject):
    """Plays AX100 digipeater audio through sounddevice and controls PTT.

    Mirrors ft4_tab.py's _TxWorker exactly (plain Python thread, not
    QThread, since sounddevice.play() blocks and no Qt event loop is
    needed inside the worker)."""

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
        """Emits exactly one of `error` or `finished` — never both."""
        mgr = get_audio_device_manager()
        if not mgr.acquire_output(_AUDIO_OWNER, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            self.error.emit(_("Sound card output is in use by {other}").format(other=other))
            return
        try:
            import sounddevice as sd  # optional dep

            if self._rig is None:
                self.error.emit(_("Rig 1 not connected — cannot key PTT"))
                return
            if not self._rig.set_ptt(True):
                self.error.emit(_("PTT command failed — check Rig 1 connection"))
                return
            time.sleep(_PTT_LEAD_S)

            sd.play(
                self._audio,
                samplerate=_SOUNDCARD_SAMPLE_RATE,
                device=self._out_device,
                blocking=False,
            )
            mgr.pin_active_output(_AUDIO_OWNER)
            sd.wait()

            time.sleep(_PTT_TAIL_S)
            self._rig.set_ptt(False)
            self.finished.emit()
        except Exception as exc:
            if self._rig is not None:
                with contextlib.suppress(Exception):
                    self._rig.set_ptt(False)
            self.error.emit(str(exc))
        finally:
            mgr.release_output(_AUDIO_OWNER, self._out_device)


class _CspSettingsDialog(QDialog):
    """Priority/Source/Destination/Dest Port/Source Port for outgoing AX100
    frames' CSP header.

    Exposed in the UI because these values are unconfirmed placeholders
    (see comms/ax100digi/tx.py's module docstring) — MARMOTSat's actual
    CSP addressing isn't publicly documented, so a real transmission test
    may need these adjusted once the correct values are known some other
    way (e.g. from MARMOTSat operators or observed traffic).
    """

    def __init__(self, header: CspHeader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("CSP Address Settings"))
        form = QFormLayout(self)

        info = QLabel(
            _(
                'These identify the destination "node" and "service" on the '
                "satellite, similar to an IP address + port. Unconfirmed values "
                "may cause the satellite to silently ignore transmitted frames "
                "even if the signal itself decodes cleanly."
            )
        )
        info.setWordWrap(True)
        form.addRow(info)

        self._priority_spin = QSpinBox()
        self._priority_spin.setRange(0, 3)
        self._priority_spin.setValue(header.priority)
        form.addRow(_("Priority:"), self._priority_spin)

        self._source_spin = QSpinBox()
        self._source_spin.setRange(0, 31)
        self._source_spin.setValue(header.source)
        form.addRow(_("Source Address:"), self._source_spin)

        self._dest_spin = QSpinBox()
        self._dest_spin.setRange(0, 31)
        self._dest_spin.setValue(header.destination)
        form.addRow(_("Destination Address:"), self._dest_spin)

        self._dest_port_spin = QSpinBox()
        self._dest_port_spin.setRange(0, 63)
        self._dest_port_spin.setValue(header.dest_port)
        form.addRow(_("Destination Port:"), self._dest_port_spin)

        self._source_port_spin = QSpinBox()
        self._source_port_spin.setRange(0, 63)
        self._source_port_spin.setValue(header.source_port)
        form.addRow(_("Source Port:"), self._source_port_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_header(self) -> CspHeader:
        return CspHeader(
            priority=self._priority_spin.value(),
            source=self._source_spin.value(),
            destination=self._dest_spin.value(),
            dest_port=self._dest_port_spin.value(),
            source_port=self._source_port_spin.value(),
        )


class Ax100DigiTab(QWidget):
    """Non-resident Communications > AX100 Digipeater tab."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control
        self._sdr_pipeline: Any = None
        self._receiver: Ax100DigiReceiver | None = None
        self._rx_audio_bridge: RxAudioBridge | None = None
        self._soundcard_active = False
        self._in_device: int | None = None
        self._out_device: int | None = None
        self._tx_in_progress = False
        self._tx_thread: threading.Thread | None = None

        self._ensure_table()
        self._load_settings()
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_decode)

        self._apply_input_source()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _ensure_table(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ax100_digi_log (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at        DATETIME NOT NULL,
                source             TEXT,
                dest               TEXT,
                sat_name           TEXT,
                store_seconds      INTEGER,
                content            TEXT,
                raw_hex            TEXT NOT NULL,
                golay_bit_errors   INTEGER,
                rs_bytes_corrected INTEGER
            )"""
        )
        self._conn.commit()

    def _persist_frame(self, decoded: DecodedDigiFrame, ts: datetime.datetime) -> None:
        message = decoded.message
        self._conn.execute(
            """INSERT INTO ax100_digi_log
               (received_at, source, dest, sat_name, store_seconds, content,
                raw_hex, golay_bit_errors, rs_bytes_corrected)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts.isoformat(),
                message.source if message else None,
                message.dest if message else None,
                message.sat_name if message else None,
                message.store_seconds if message else None,
                message.content if message else (decoded.raw_text or decoded.payload.hex()),
                decoded.payload.hex(),
                decoded.golay_bit_errors,
                decoded.rs_bytes_corrected,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_SETTINGS_KEY,)
        ).fetchone()
        data: dict[str, Any] = json.loads(row[0]) if row else {}
        self._dest_call = data.get("dest_call", "")
        self._sat_name = data.get("sat_name", "MARMOTSat")
        self._rx_source = data.get("rx_source", "soundcard")
        self._content_history: list[str] = data.get("content_history", [])
        csp = data.get("csp_header")
        self._csp_header = CspHeader(**csp) if csp else DEFAULT_CSP_HEADER
        self._squelch_value = int(data.get("squelch_value", 0))

        tz_row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone_mode'"
        ).fetchone()
        self._use_utc = (tz_row["value"] if tz_row and tz_row["value"] else "utc") != "local"

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
                "dest_call": self._dest_edit.text().strip(),
                "sat_name": self._sat_edit.text().strip(),
                "rx_source": "soundcard" if self._rb_soundcard.isChecked() else "sdr",
                "content_history": self._content_history,
                "csp_header": {
                    "priority": self._csp_header.priority,
                    "source": self._csp_header.source,
                    "destination": self._csp_header.destination,
                    "dest_port": self._csp_header.dest_port,
                    "source_port": self._csp_header.source_port,
                },
                "squelch_value": self._squelch_slider.value(),
            }
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_SETTINGS_KEY, data),
        )
        self._conn.commit()

    def _remember_content(self, text: str) -> None:
        """Add `text` to the Content history combo (most-recent-first,
        deduplicated, capped), and persist it."""
        text = text.strip()
        if not text:
            return
        if text in self._content_history:
            self._content_history.remove(text)
        self._content_history.insert(0, text)
        del self._content_history[_MAX_CONTENT_HISTORY:]
        self._save_settings()
        self._refresh_content_combo(current_text=text)

    def _refresh_content_combo(self, current_text: str = "") -> None:
        self._content_combo.blockSignals(True)
        self._content_combo.clear()
        self._content_combo.addItems(self._content_history)
        self._content_combo.setCurrentText(current_text)
        self._content_combo.blockSignals(False)

    @Slot()
    def _on_clear_content_history(self) -> None:
        self._content_history = []
        self._save_settings()
        self._refresh_content_combo()

    @Slot()
    def _on_edit_csp_settings(self) -> None:
        dialog = _CspSettingsDialog(self._csp_header, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._csp_header = dialog.result_header()
            self._save_settings()

    def _get_my_call(self) -> str:
        """Read the operator's own callsign from File > Set QTH (the same
        `app_settings['callsign']` FT4/Q65/APRS fall back to). Read fresh
        on every send rather than cached, so setting the callsign for the
        first time after this tab is already open works without a restart."""
        row = self._conn.execute("SELECT value FROM app_settings WHERE key = 'callsign'").fetchone()
        return str(row[0]) if row else ""

    # ------------------------------------------------------------------ #
    # Squelch
    # ------------------------------------------------------------------ #

    def _squelch_threshold_dbfs(self) -> float | None:
        """None means disabled (slider at 0, the default — never rejects)."""
        value = self._squelch_slider.value() if hasattr(self, "_squelch_slider") else 0
        if value <= 0:
            return None
        return _SQUELCH_MIN_DBFS + value

    def _squelch_display_text(self) -> str:
        threshold = self._squelch_threshold_dbfs()
        if threshold is None:
            return _("Off")
        return _("{db:.0f} dBFS").format(db=threshold)

    def _passes_squelch(self, samples: NDArray[Any]) -> bool:
        threshold = self._squelch_threshold_dbfs()
        if threshold is None:
            return True
        return _peak_dbfs(samples) >= threshold

    @Slot(int)
    def _on_squelch_changed(self, _value: int) -> None:
        self._squelch_label.setText(self._squelch_display_text())

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        info = QLabel(_("AX100 Digi (MARMOTSat) ℹ"))
        info.setToolTip(
            _(
                'AX100 "ASM+Golay" digipeater — GMSK 1200 baud, Golay(24,12) + '
                "Reed-Solomon(255,223), CSP transport. Protocol-compatible with "
                "GreenCube (IO-117), but this tab targets MARMOTSat only — "
                "GreenCube's ground station is currently out of service. Set the "
                "rig to SSB mode on 145.875 MHz."
            )
        )
        top_row.addWidget(info)
        top_row.addStretch(1)

        top_row.addWidget(QLabel(_("Input/Output:")))
        self._rb_soundcard = QRadioButton(_("Rig Soundcard"))
        self._rb_sdr = QRadioButton(_("SDR (receive only)"))
        if self._rx_source == "sdr":
            self._rb_sdr.setChecked(True)
        else:
            self._rb_soundcard.setChecked(True)
        top_row.addWidget(self._rb_soundcard)
        top_row.addWidget(self._rb_sdr)
        layout.addLayout(top_row)
        self._rb_soundcard.toggled.connect(self._on_source_changed)

        status_row = QHBoxLayout()
        self._status_label = QLabel(_("Input: not connected"))
        # Without word wrap, QLabel's minimumSizeHint is wide enough to show
        # its entire text on one line — fine for the short default text, but
        # a long sound-card error (_validate_input_device()'s messages
        # embed the device name/index) forces the whole window to that
        # width and blocks shrinking it back down.
        self._status_label.setWordWrap(True)
        status_row.addWidget(self._status_label)
        status_row.addStretch(1)

        status_row.addWidget(QLabel(_("Squelch:")))
        self._squelch_slider = QSlider(Qt.Orientation.Horizontal)
        self._squelch_slider.setRange(0, _SQUELCH_MAX)
        self._squelch_slider.setValue(self._squelch_value)
        self._squelch_slider.setFixedWidth(90)
        self._squelch_slider.setToolTip(
            _(
                "Minimum input level required before a frame decode is attempted. "
                "Left = off (process everything, the default — a real signal is "
                "never rejected). Raise it only after watching the log fill with "
                "garbage on an idle frequency, using that as a guide for the noise "
                "floor; setting it too high can block a real, weak signal."
            )
        )
        self._squelch_label = QLabel(self._squelch_display_text())
        self._squelch_slider.valueChanged.connect(self._on_squelch_changed)
        status_row.addWidget(self._squelch_slider)
        status_row.addWidget(self._squelch_label)

        export_csv_btn = QPushButton(_("Export CSV…"))
        export_csv_btn.clicked.connect(self._on_export_csv)
        status_row.addWidget(export_csv_btn)

        export_adif_btn = QPushButton(_("Export ADIF…"))
        export_adif_btn.setToolTip(
            _("Exports only messages addressed to your own callsign (confirmed exchanges)")
        )
        export_adif_btn.clicked.connect(self._on_export_adif)
        status_row.addWidget(export_adif_btn)

        layout.addLayout(status_row)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            [self._time_column_label(), _("Src"), _("Dst"), _("Sat"), _("Message")]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Enlarge the frame-log font ~1.5x to match the APRS Received Packets
        # list and the Telemetry Received Frames table; the decoded message
        # column is dense. setFont() on the table propagates to the header
        # views, so pin them back to the base size -- only the data rows grow.
        _base_pt = self._table.font().pointSizeF()
        _base_px = self._table.font().pixelSize()
        _big_font = self._table.font()
        _header_font = self._table.font()
        if _base_pt > 0:
            _big_font.setPointSizeF(_base_pt * 1.5)
            _header_font.setPointSizeF(_base_pt)
        else:
            _big_font.setPixelSize(max(1, round(_base_px * 1.5)))
            _header_font.setPixelSize(max(1, _base_px))
        self._table.setFont(_big_font)
        self._table.horizontalHeader().setFont(_header_font)
        self._table.verticalHeader().setFont(_header_font)
        layout.addWidget(self._table)

        layout.addWidget(self._build_tx_group())

    def _build_tx_group(self) -> QGroupBox:
        group = QGroupBox(_("Send Message"))
        form = QFormLayout(group)

        addr_row = QHBoxLayout()
        addr_row.addWidget(QLabel(_("To:")))
        self._dest_edit = QLineEdit(self._dest_call)
        self._dest_edit.setMaximumWidth(90)
        addr_row.addWidget(self._dest_edit)

        addr_row.addWidget(QLabel(_("Satellite:")))
        self._sat_edit = QLineEdit(self._sat_name)
        self._sat_edit.setMaximumWidth(110)
        addr_row.addWidget(self._sat_edit)

        addr_row.addWidget(QLabel(_("STORE=:")))
        self._store_spin = QSpinBox()
        self._store_spin.setRange(0, 172_800)  # GreenCube's 2-day max relay delay
        self._store_spin.setSuffix(_(" s"))
        self._store_spin.setMaximumWidth(90)
        self._store_spin.setToolTip(
            _("0 = relay immediately; otherwise seconds to hold on board (max 172800 = 2 days)")
        )
        addr_row.addWidget(self._store_spin)
        addr_row.addStretch(1)
        form.addRow(addr_row)

        content_row = QHBoxLayout()
        self._content_combo = QComboBox()
        self._content_combo.setEditable(True)
        self._content_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        content_line_edit = self._content_combo.lineEdit()
        if content_line_edit is not None:
            content_line_edit.setPlaceholderText(_("Message content"))
        self._content_combo.addItems(self._content_history)
        self._content_combo.setCurrentText("")
        content_row.addWidget(self._content_combo, stretch=1)

        clear_history_btn = QPushButton(_("Clear History"))
        clear_history_btn.setToolTip(_("Delete all remembered message content"))
        clear_history_btn.clicked.connect(self._on_clear_content_history)
        content_row.addWidget(clear_history_btn)

        form.addRow(_("Content:"), content_row)

        send_row = QHBoxLayout()
        self._send_btn = QPushButton(_("Send"))
        self._send_btn.clicked.connect(self._on_send)
        send_row.addWidget(self._send_btn)

        csp_btn = QPushButton(_("CSP Settings..."))
        csp_btn.setToolTip(
            _(
                "CSP addressing used for outgoing frames is not yet confirmed "
                "against a real satellite — see comms/ax100digi/tx.py."
            )
        )
        csp_btn.clicked.connect(self._on_edit_csp_settings)
        send_row.addWidget(csp_btn)

        self._tx_status_label = QLabel("")
        self._tx_status_label.setWordWrap(True)
        send_row.addWidget(self._tx_status_label)
        send_row.addStretch(1)
        form.addRow(send_row)

        return group

    def _time_column_label(self) -> str:
        return _("Time (UTC)") if self._use_utc else _("Time (Local)")

    def set_use_utc(self, use_utc: bool) -> None:
        """Called by MainWindow when View > Time Zone changes while this
        tab is open (see main_window._apply_time_zone()/
        _on_time_zone_changed()'s duck-typed comms-tab broadcast loop)."""
        if use_utc == self._use_utc:
            return
        self._use_utc = use_utc
        header_item = self._table.horizontalHeaderItem(0)
        if header_item is not None:
            header_item.setText(self._time_column_label())

    def _append_row(self, decoded: DecodedDigiFrame) -> None:
        now_dt_utc = datetime.datetime.now(datetime.UTC)
        self._persist_frame(decoded, now_dt_utc)

        now_dt = now_dt_utc if self._use_utc else now_dt_utc.astimezone()
        now = now_dt.strftime("%H:%M:%S")
        if decoded.message is not None:
            src, dst, sat, text = (
                decoded.message.source,
                decoded.message.dest,
                decoded.message.sat_name,
                decoded.message.content,
            )
        else:
            src, dst, sat = "", "", ""
            text = decoded.raw_text or decoded.payload.hex()

        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, value in enumerate((now, src, dst, sat, text)):
            self._table.setItem(row, col, QTableWidgetItem(value))
        self._table.scrollToBottom()

        while self._table.rowCount() > _MAX_LOG_ROWS:
            self._table.removeRow(0)

    # ------------------------------------------------------------------ #
    # Input source
    # ------------------------------------------------------------------ #

    @Slot(bool)
    def _on_source_changed(self, _checked: bool) -> None:
        self._disconnect_sdr_audio()
        self._disconnect_soundcard_audio()
        self._apply_input_source()

    def _apply_input_source(self) -> None:
        if self._rb_soundcard.isChecked():
            self._connect_soundcard_audio()
        else:
            self._connect_sdr_audio()

    # ------------------------------------------------------------------ #
    # Rig + Sound Card
    # ------------------------------------------------------------------ #

    def _connect_soundcard_audio(self) -> None:
        if self._in_device is None:
            self._status_label.setText(
                _("Sound Card not configured — open Rig Settings > Sound Card")
            )
            return
        try:
            import sounddevice as sd  # noqa: F401 — validate availability
        except ImportError:
            self._status_label.setText(_("sounddevice not installed — pip install sounddevice"))
            return

        self._rx_audio_bridge = RxAudioBridge(sample_rate=_SOUNDCARD_SAMPLE_RATE)
        self._receiver = Ax100DigiReceiver(sample_rate=_SOUNDCARD_SAMPLE_RATE)
        try:
            get_audio_device_manager().acquire_input(
                _AUDIO_OWNER, self._in_device, _SOUNDCARD_SAMPLE_RATE, self._on_soundcard_chunk
            )
            self._soundcard_active = True
            self._status_label.setText(_("Input: Rig Soundcard connected"))
            self._timer.start()
        except Exception as exc:
            self._status_label.setText(_("Audio open error: {exc}").format(exc=exc))
            self._soundcard_active = False

    def _disconnect_soundcard_audio(self) -> None:
        self._timer.stop()
        if self._soundcard_active:
            with contextlib.suppress(Exception):
                get_audio_device_manager().release_input(_AUDIO_OWNER, self._in_device)
        self._soundcard_active = False
        self._rx_audio_bridge = None
        self._receiver = None

    def _on_soundcard_chunk(self, chunk: NDArray[np.float32]) -> None:
        if self._rx_audio_bridge is None or self._receiver is None:
            return
        if not self._passes_squelch(chunk):
            return
        iq = self._rx_audio_bridge.process(chunk)
        self._receiver.push_samples(iq)

    def _rig1(self) -> Any:
        return getattr(self._radio_control, "_rig1", None)

    # ------------------------------------------------------------------ #
    # SDR I/Q (receive only)
    # ------------------------------------------------------------------ #

    def _connect_sdr_audio(self) -> None:
        if self._radio_control is None:
            return
        try:
            sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
            if sdr_ctrl is None:
                return
            pipeline = getattr(sdr_ctrl, "_pipeline", None)
            if pipeline is None:
                self._status_label.setText(_("Input: SDR not connected"))
                return
            sample_rate = int(pipeline._device.sample_rate)
        except AttributeError:
            self._status_label.setText(_("Input: cannot determine SDR sample rate"))
            return

        self._sdr_pipeline = pipeline
        self._receiver = Ax100DigiReceiver(sample_rate=sample_rate)
        pipeline.subscribe(self._on_sdr_iq_chunk)
        self._status_label.setText(_("Input: SDR connected (receive only)"))
        self._timer.start()

    def _disconnect_sdr_audio(self) -> None:
        self._timer.stop()
        if self._sdr_pipeline is not None and self._receiver is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.unsubscribe(self._on_sdr_iq_chunk)
        self._sdr_pipeline = None
        self._receiver = None

    def _on_sdr_iq_chunk(self, iq: NDArray[np.complex64]) -> None:
        if self._receiver is None:
            return
        if not self._passes_squelch(iq):
            return
        self._receiver.push_samples(iq)

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """MainWindow calls this whenever Rig 1/2's SDR (re)connects/
        disconnects (see main_window._notify_comms_tabs_sdr_pipeline). This
        tab's own _sdr_pipeline reference, grabbed once at construction,
        would otherwise go silently stale after any later SDR reconnect
        (the same GitHub Issue #12 class of bug fixed for CW/FT4/Q65)."""
        if not self._rb_sdr.isChecked():
            return
        self._disconnect_sdr_audio()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR disconnected"))
            return
        self._connect_sdr_audio()

    def _poll_decode(self) -> None:
        if self._receiver is None:
            return
        for decoded in self._receiver.decode_pending():
            self._append_row(decoded)

    # ------------------------------------------------------------------ #
    # TX
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_send(self) -> None:
        if self._tx_in_progress:
            return
        my_call = self._get_my_call()
        dest_call = self._dest_edit.text().strip()
        sat_name = self._sat_edit.text().strip()
        content = self._content_combo.currentText().strip()
        if not my_call:
            self._tx_status_label.setText(_("My Call not set — configure it in File > Set QTH"))
            return
        if not dest_call or not content:
            self._tx_status_label.setText(_("To and Content are required"))
            return

        # Remember the content as soon as the basic fields are valid, not
        # gated on Rig 1 actually being connected — so message text typed
        # while testing without a rig isn't lost.
        self._remember_content(content)

        if not self._rb_soundcard.isChecked():
            self._tx_status_label.setText(_("Switch Input/Output to Rig Soundcard to send"))
            return

        rig = self._rig1()
        if rig is None or not getattr(rig, "is_connected", False):
            self._tx_status_label.setText(_("Rig 1 not connected"))
            return

        try:
            result = build_tx_audio(
                my_call,
                dest_call,
                sat_name,
                content,
                store_seconds=self._store_spin.value(),
                csp_header=self._csp_header,
                sample_rate=_SOUNDCARD_SAMPLE_RATE,
            )
        except ValueError as exc:
            self._tx_status_label.setText(str(exc))
            return

        worker = _TxWorker(result.audio, self._out_device, rig)
        worker.finished.connect(self._on_tx_finished)
        worker.error.connect(self._on_tx_error)

        self._tx_in_progress = True
        self._send_btn.setEnabled(False)
        t = threading.Thread(target=worker.run, daemon=True)
        self._tx_thread = t
        t.start()
        self._tx_status_label.setText(_("TX: ") + result.message_text)

    @Slot()
    def _on_tx_finished(self) -> None:
        self._tx_in_progress = False
        self._send_btn.setEnabled(True)
        self._tx_status_label.setText(_("TX done"))

    @Slot(str)
    def _on_tx_error(self, msg: str) -> None:
        self._tx_in_progress = False
        self._send_btn.setEnabled(True)
        self._tx_status_label.setText(_("TX error: ") + msg)

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_export_csv(self) -> None:
        default_name = (
            "ax100_digi_" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d") + ".csv"
        )
        path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Export AX100 Digi CSV"),
            str(Path.home() / default_name),
            "CSV (*.csv)",
        )
        if not path:
            return
        rows_count = self._table.rowCount()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([self._time_column_label(), _("Src"), _("Dst"), _("Sat"), _("Message")])
            for r in range(rows_count):
                writer.writerow(
                    [(item.text() if (item := self._table.item(r, c)) else "") for c in range(5)]
                )

    @Slot()
    def _on_export_adif(self) -> None:
        """Export confirmed message exchanges (messages addressed to this
        operator's own callsign) as ADIF, matching APRS's convention of
        only logging real bidirectional traffic rather than every relayed
        packet — see comms.aprs's _is_confirmed_reply()."""
        my_call = self._get_my_call().upper()
        if not my_call:
            QMessageBox.warning(
                self,
                _("Export ADIF"),
                _("My Call not set — configure it in File > Set QTH"),
            )
            return

        rows = self._conn.execute(
            """SELECT received_at, source, sat_name, content FROM ax100_digi_log
               WHERE dest = ? AND source IS NOT NULL
               ORDER BY received_at""",
            (my_call,),
        ).fetchall()
        if not rows:
            QMessageBox.information(
                self,
                _("Export ADIF"),
                _("No confirmed messages addressed to {call} yet.").format(call=my_call),
            )
            return

        path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Export ADIF"),
            str(Path.home() / adif_default_filename()),
            "ADIF (*.adi)",
        )
        if not path:
            return

        records = []
        for row in rows:
            ts = datetime.datetime.fromisoformat(row["received_at"])
            fields = {
                "CALL": row["source"],
                "QSO_DATE": ts.strftime("%Y%m%d"),
                "TIME_ON": ts.strftime("%H%M%S"),
                "MODE": "PKT",
                "MY_CALL": my_call,
                "COMMENT": row["content"] or "",
                "SAT_NAME": row["sat_name"] or "",
                "PROP_MODE": "SAT",
                "RST_SENT": "599",
                "RST_RCVD": "599",
            }
            records.append(build_adif_record(fields))
        adif_write_or_append(path, "".join(records))

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._save_settings()
        self._disconnect_sdr_audio()
        self._disconnect_soundcard_audio()
        if self._tx_thread is not None and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=2.0)
            if self._tx_thread.is_alive():
                rig = self._rig1()
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.set_ptt(False)
        super().closeEvent(event)
