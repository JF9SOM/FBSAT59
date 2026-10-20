"""Foresail-1p digipeater panel (hosted by the Message Box/Digipeater tab).

Foresail-1p (OH2F1S, NORAD 66778) carries an amateur packet repeater on Skylink virtual
channel 3: AX.25 UI frames (APRS or anything else) sent to it are repeated back with the
satellite as a digipeater. Both directions use 437.125 MHz, 9600 baud GMSK.

  - Receive: the SDR's raw I/Q goes to ``comms.foresail.rx`` (a coherent GMSK detector plus the
    Skylink ASM / Golay / RS stack). Every satellite frame also feeds a ``TddTracker``, which
    follows the satellite's transmit windows.
  - Transmit: the rig transmits through the Sound Card output (the radio does the FSK
    modulation). *Manual* sends at once; *Auto* waits until the satellite's windows leave room.

Frame formats and the satellite's quirks: comms/foresail/skylink.py. The uplink has so far only
been verified against decoders (this app's receiver and gr-satellites), not on the air.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
import threading
import time
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.audio_tx import AUDIO_RATE as _AUDIO_RATE
from comms.audio_tx import PTT_LEAD_S, PttAudioTxWorker
from comms.ax100digi.rs_ccsds import RsUnavailableError
from comms.foresail.rx import ForesailRxFrame, ForesailSdrDemod
from comms.foresail.skylink import (
    ACCEPTED_DESTINATIONS,
    VC_REPEATER,
    SkylinkFrame,
    max_info_length,
    parse_repeater_frame,
    parse_skylink_frame,
)
from comms.foresail.tdd import DEFAULT_GUARD_S, TddTracker
from comms.foresail.tx import build_uplink
from i18n import _
from rig.controller import select_tx_rig
from ui.tx_level import (
    TX_LEVEL_MAX_DB,
    TX_LEVEL_MIN_DB,
    db_to_gain,
    format_db,
    load_level_db,
)

_SETTINGS_KEY = "foresail1p_settings"
_MAX_LOG_ROWS = 500
_UI_TICK_MS = 100
_MODE_MANUAL = "manual"
_MODE_AUTO = "auto"
_DEFAULT_IDENTITY = "PyGS"
# The satellite counts as "heard recently" for this long after its last frame.
_HEARD_S = 90.0
_COLOR_GREEN = "#2ecc71"
_COLOR_YELLOW = "#f1c40f"

_SERVICE_NAMES = {
    (3, 2): "OBC housekeeping",
    (3, 3): "EPS housekeeping",
    (3, 4): "UHF housekeeping",
    (3, 5): "ADCS housekeeping",
}


def describe_frame(frame: SkylinkFrame) -> tuple[str, str, str, str, str]:
    """``(kind, from, to, via, content)`` of a Skylink frame for the log table."""
    if frame.vc == VC_REPEATER:
        repeater = parse_repeater_frame(frame.payload)
        if repeater is not None:
            fcs = "" if repeater.fcs_status == "ok" else f" [FCS {repeater.fcs_status}]"
            return (
                _("Repeater (VC 3)"),
                str(repeater.source),
                str(repeater.destination),
                repeater.via,
                repeater.text + fcs,
            )
        return (_("Repeater (VC 3)"), "", "", "", frame.payload.hex(" "))
    payload = frame.payload
    if len(payload) >= 9:
        key = (payload[7], payload[8])
        name = _SERVICE_NAMES.get(key, _("service {a},{b}").format(a=key[0], b=key[1]))
        return (
            _("Telemetry (VC {vc})").format(vc=frame.vc),
            frame.identity,
            "",
            "",
            f"{name} ({len(payload)} B)",
        )
    return (_("VC {vc}").format(vc=frame.vc), frame.identity, "", "", payload.hex(" "))


class Foresail1pPanel(QWidget):
    """Receive the Foresail-1p repeater and send frames through it."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: QWidget | None = None,
        parent: QWidget | None = None,
        autostart: bool = True,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control
        self._sdr_pipeline: Any = None
        self._demod: ForesailSdrDemod | None = None
        self._tdd = TddTracker()
        self._tx_in_progress = False
        self._tx_thread: threading.Thread | None = None
        self._tx_worker: PttAudioTxWorker | None = None
        self._armed: tuple[Any, ...] | None = None  # (dest, src, info, digis) waiting for a slot
        self._use_utc = True
        self._frames_total = 0
        self._frames_repeater = 0
        self._last_heard: float | None = None

        self._ensure_table()
        self._load_settings()
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(_UI_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        if autostart:
            self.start_input()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _ensure_table(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS foresail1p_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at DATETIME NOT NULL,
                kind        TEXT,
                source      TEXT,
                dest        TEXT,
                via         TEXT,
                content     TEXT,
                raw_hex     TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _load_settings(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_SETTINGS_KEY,)
        ).fetchone()
        data: dict[str, Any] = json.loads(row[0]) if row else {}
        self._dest = str(data.get("dest", "APRS"))
        self._via = str(data.get("via", ""))
        self._last_message = str(data.get("last_message", ""))
        self._identity = str(data.get("identity", _DEFAULT_IDENTITY))
        self._send_mode = _MODE_AUTO if data.get("send_mode") == _MODE_AUTO else _MODE_MANUAL
        self._guard_ms = int(data.get("guard_ms", int(DEFAULT_GUARD_S * 1000)))
        self._show_telemetry = bool(data.get("show_telemetry", False))
        self._tx_level_db = load_level_db(data)
        tz = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone_mode'"
        ).fetchone()
        self._use_utc = (tz["value"] if tz and tz["value"] else "utc") != "local"

    def _save_settings(self) -> None:
        data = json.dumps(
            {
                "dest": self._dest_combo.currentText().strip(),
                "via": self._via_edit.text().strip(),
                "last_message": self._message_edit.text(),
                "identity": self._identity_edit.text().strip(),
                "send_mode": self._mode_combo.currentData(),
                "guard_ms": self._guard_spin.value(),
                "show_telemetry": self._telemetry_check.isChecked(),
                "tx_level_db": self._level_slider.value(),
            }
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_SETTINGS_KEY, data),
        )
        self._conn.commit()

    def _get_my_call(self) -> str:
        row = self._conn.execute("SELECT value FROM app_settings WHERE key = 'callsign'").fetchone()
        return str(row[0]).strip().upper() if row else ""

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        info = QLabel(_("Foresail-1p (OH2F1S) digipeater ℹ"))
        info.setToolTip(
            _(
                "437.125 MHz, 9600 baud GMSK, Skylink protocol. Virtual channel 3 is the "
                "amateur packet repeater: AX.25 UI frames (APRS or any text) sent to the "
                "satellite are repeated back with the satellite added as a digipeater (kept "
                "in memory and repeated 4 times every 4 minutes). Receive with the SDR; the "
                "rig transmits through the Sound Card. The satellite is only heard during "
                "its beacon every minute or so, and its transmit windows must be avoided."
            )
        )
        top.addWidget(info)
        top.addStretch(1)
        self._telemetry_check = QCheckBox(_("Show telemetry frames (VC 0/1)"))
        self._telemetry_check.setChecked(self._show_telemetry)
        self._telemetry_check.toggled.connect(lambda _c: self._save_settings())
        top.addWidget(self._telemetry_check)
        layout.addLayout(top)

        self._status_label = QLabel(_("Input: not connected"))
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            [self._time_column_label(), _("Type"), _("From"), _("To"), _("Via"), _("Message")]
        )
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        layout.addWidget(self._build_window_group())
        layout.addWidget(self._build_send_group())

    def _build_window_group(self) -> QGroupBox:
        group = QGroupBox(_("Satellite windows"))
        row = QHBoxLayout(group)
        self._window_label = QLabel(_("Satellite: not heard yet"))
        row.addWidget(self._window_label, stretch=1)
        row.addWidget(QLabel(_("Send mode:")))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem(_("Manual (send now)"), _MODE_MANUAL)
        self._mode_combo.addItem(_("Auto (between satellite windows)"), _MODE_AUTO)
        self._mode_combo.setCurrentIndex(1 if self._send_mode == _MODE_AUTO else 0)
        self._mode_combo.setToolTip(
            _(
                "Manual sends the moment you press Send. Auto holds the message until the "
                "satellite's current transmit window (read from its frames) has ended and the "
                "next one is not about to start."
            )
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(self._mode_combo)
        row.addWidget(QLabel(_("Guard:")))
        self._guard_spin = QSpinBox()
        self._guard_spin.setRange(0, 2000)
        self._guard_spin.setSingleStep(50)
        self._guard_spin.setSuffix(" ms")
        self._guard_spin.setValue(self._guard_ms)
        self._guard_spin.setToolTip(
            _(
                "Wait this long after a satellite window ends before transmitting. How soon "
                "after its window the satellite listens is not published: tune it on real passes."
            )
        )
        self._guard_spin.valueChanged.connect(lambda _v: self._save_settings())
        row.addWidget(self._guard_spin)
        return group

    def _build_send_group(self) -> QGroupBox:
        group = QGroupBox(_("Repeater message"))
        v = QVBoxLayout(group)

        row = QHBoxLayout()
        self._call_label = QLabel()
        row.addWidget(self._call_label)
        row.addWidget(QLabel(_("To:")))
        self._dest_combo = QComboBox()
        self._dest_combo.setEditable(True)
        for dest in ACCEPTED_DESTINATIONS:
            self._dest_combo.addItem(dest)
        self._dest_combo.setCurrentText(self._dest)
        self._dest_combo.setToolTip(
            _(
                "The satellite repeats frames addressed to OH2F1S, APRS, ALL*, BEACON or CQ* "
                "(* = any characters)."
            )
        )
        self._dest_combo.setMaximumWidth(110)
        row.addWidget(self._dest_combo)
        row.addWidget(QLabel(_("Via:")))
        self._via_edit = QLineEdit(self._via)
        self._via_edit.setPlaceholderText(_("optional, comma separated"))
        self._via_edit.setMaximumWidth(180)
        self._via_edit.setToolTip(_("Up to 8 digipeater callsigns. The satellite adds itself."))
        row.addWidget(self._via_edit)
        row.addStretch(1)
        row.addWidget(QLabel(_("Skylink ID:")))
        self._identity_edit = QLineEdit(self._identity)
        self._identity_edit.setMaxLength(7)
        self._identity_edit.setMaximumWidth(80)
        self._identity_edit.setToolTip(
            _(
                "Identity of this ground station in the Skylink header (1-7 characters, not "
                "OH2F1S). The Aalto test tools use PyGS."
            )
        )
        row.addWidget(self._identity_edit)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel(_("Message:")))
        self._message_edit = QLineEdit(self._last_message)
        self._message_edit.setPlaceholderText(
            _("APRS data or any text (first character = APRS data type)")
        )
        self._message_edit.textChanged.connect(self._update_length_label)
        row2.addWidget(self._message_edit, stretch=1)
        self._length_label = QLabel()
        self._length_label.setFixedWidth(60)
        row2.addWidget(self._length_label)
        row2.addWidget(QLabel(_("TX Level:")))
        self._level_slider = QSlider(Qt.Orientation.Horizontal)
        self._level_slider.setRange(TX_LEVEL_MIN_DB, TX_LEVEL_MAX_DB)
        self._level_slider.setValue(self._tx_level_db)
        self._level_slider.setFixedWidth(110)
        self._level_slider.setToolTip(
            _(
                "TX audio level in dB below full scale. Set it so the FM data deviation is "
                "right for 9600 baud; combine with the radio's data input gain."
            )
        )
        self._level_label = QLabel(format_db(self._tx_level_db))
        self._level_label.setFixedWidth(46)
        self._level_slider.valueChanged.connect(self._on_level_changed)
        row2.addWidget(self._level_slider)
        row2.addWidget(self._level_label)
        v.addLayout(row2)

        buttons = QHBoxLayout()
        self._send_btn = QPushButton(_("Send"))
        self._send_btn.clicked.connect(self._on_send)
        self._cancel_btn = QPushButton(_("Cancel armed"))
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel_armed)
        buttons.addWidget(self._send_btn)
        buttons.addWidget(self._cancel_btn)
        buttons.addStretch(1)
        v.addLayout(buttons)

        self._tx_status_label = QLabel("")
        self._tx_status_label.setWordWrap(True)
        v.addWidget(self._tx_status_label)
        self._refresh_call_label()
        self._update_length_label()
        return group

    def _refresh_call_label(self) -> None:
        call = self._get_my_call()
        self._call_label.setText(
            _("From: {call}").format(call=call) if call else _("From: (set in File > Set QTH)")
        )

    def _digipeaters(self) -> tuple[str, ...]:
        return tuple(p.strip().upper() for p in self._via_edit.text().split(",") if p.strip())

    def _update_length_label(self) -> None:
        limit = max_info_length(len(self._digipeaters()))
        used = len(self._message_edit.text().encode("ascii", errors="replace"))
        over = used > limit
        self._length_label.setText(f"{used}/{limit}")
        self._length_label.setStyleSheet("color: #e74c3c;" if over else "")

    def _time_column_label(self) -> str:
        return _("Time (UTC)") if self._use_utc else _("Time (Local)")

    def set_use_utc(self, use_utc: bool) -> None:
        """Called by MainWindow when View > Time Zone changes."""
        if use_utc == self._use_utc:
            return
        self._use_utc = use_utc
        item = self._table.horizontalHeaderItem(0)
        if item is not None:
            item.setText(self._time_column_label())

    @Slot(int)
    def _on_level_changed(self, value: int) -> None:
        self._level_label.setText(format_db(value))

    @Slot(int)
    def _on_mode_changed(self, _index: int) -> None:
        if self._mode_combo.currentData() == _MODE_MANUAL:
            self._on_cancel_armed()
        self._save_settings()

    # ------------------------------------------------------------------ #
    # Input (SDR)
    # ------------------------------------------------------------------ #

    def _get_sdr_pipeline(self) -> Any:
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        return getattr(sdr_ctrl, "_pipeline", None)

    def start_input(self) -> None:
        """(Re)start receiving from the SDR."""
        self.stop_input()
        pipeline = self._get_sdr_pipeline()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR not connected"))
            return
        try:
            sample_rate = int(pipeline._device.sample_rate)
            demod = ForesailSdrDemod(sample_rate, parent=self)
        except (AttributeError, TypeError, ValueError):
            self._status_label.setText(_("Input: cannot determine SDR sample rate"))
            return
        demod.frame_received.connect(self._on_frame)
        demod.start()
        pipeline.subscribe(demod.push_samples)
        self._sdr_pipeline = pipeline
        self._demod = demod
        self._status_label.setText(
            _("Input: SDR connected (9600 baud GMSK); the rig transmits through the Sound Card")
        )

    def stop_input(self) -> None:
        """Release the SDR subscription."""
        demod, self._demod = self._demod, None
        if self._sdr_pipeline is not None and demod is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.unsubscribe(demod.push_samples)
        self._sdr_pipeline = None
        if demod is not None:
            demod.stop()
        self._tdd.reset()
        self._armed = None

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """MainWindow calls this whenever the SDR (re)connects or disconnects."""
        self.stop_input()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR disconnected"))
            return
        self.start_input()

    # ------------------------------------------------------------------ #
    # RX
    # ------------------------------------------------------------------ #

    @Slot(object, float)
    def _on_frame(self, rx: ForesailRxFrame, wall_end: float) -> None:
        frame = parse_skylink_frame(rx.payload)
        if frame is None:
            return
        if frame.identity.upper() == self._identity_edit.text().strip().upper():
            return  # our own transmission picked up by the receiver
        self._frames_total += 1
        self._last_heard = time.monotonic()
        tdd = frame.tdd
        if tdd is not None:
            wall_start = wall_end - (rx.end_s - rx.start_s)
            self._tdd.observe(wall_start, tdd[0], tdd[1])
        kind, source, dest, via, content = describe_frame(frame)
        if frame.vc == VC_REPEATER:
            self._frames_repeater += 1
        elif not self._telemetry_check.isChecked():
            return
        self._append_row(kind, source, dest, via, content, rx.payload)

    def _append_row(
        self,
        kind: str,
        source: str,
        dest: str,
        via: str,
        content: str,
        raw: bytes,
        persist: bool = True,
    ) -> None:
        now_utc = datetime.datetime.now(datetime.UTC)
        if persist:
            self._conn.execute(
                "INSERT INTO foresail1p_log (received_at, kind, source, dest, via, content, "
                "raw_hex) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_utc.isoformat(), kind, source, dest, via, content, raw.hex()),
            )
            self._conn.commit()
        shown = now_utc if self._use_utc else now_utc.astimezone()
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, value in enumerate((shown.strftime("%H:%M:%S"), kind, source, dest, via, content)):
            self._table.setItem(row, col, QTableWidgetItem(value))
        self._table.scrollToBottom()
        while self._table.rowCount() > _MAX_LOG_ROWS:
            self._table.removeRow(0)

    # ------------------------------------------------------------------ #
    # Windows / status
    # ------------------------------------------------------------------ #

    def _tick(self) -> None:
        now = time.monotonic()
        heard = self._last_heard is not None and now - self._last_heard < _HEARD_S
        counts = _("frames {n} (repeater {r})").format(
            n=self._frames_total, r=self._frames_repeater
        )
        if self._tdd.in_satellite_window(now):
            self._window_label.setText(_("Satellite TRANSMITTING — wait") + f"  ·  {counts}")
            self._window_label.setStyleSheet(f"color: {_COLOR_YELLOW}; font-weight: bold;")
        elif heard:
            end = self._tdd.window_end
            ago = _("last window {s:.1f} s ago").format(s=now - end) if end is not None else ""
            self._window_label.setText(
                _("Satellite heard — {ago}").format(ago=ago) + f"  ·  {counts}"
            )
            self._window_label.setStyleSheet(f"color: {_COLOR_GREEN}; font-weight: bold;")
        elif self._frames_total:
            self._window_label.setText(_("Satellite quiet") + f"  ·  {counts}")
            self._window_label.setStyleSheet("")
        else:
            self._window_label.setText(_("Satellite: not heard yet") + f"  ·  {counts}")
            self._window_label.setStyleSheet("")
        self._cancel_btn.setEnabled(self._armed is not None)
        if self._armed is not None and not self._tx_in_progress:
            self._try_armed(now)

    # ------------------------------------------------------------------ #
    # TX
    # ------------------------------------------------------------------ #

    def _tx_rig(self) -> Any:
        rc = self._radio_control
        return select_tx_rig(getattr(rc, "_rig1", None), getattr(rc, "_rig2", None))

    def _output_device(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if not row:
            return None
        try:
            index = json.loads(row[0]).get("output_device_index")
        except (ValueError, AttributeError):
            return None
        return int(index) if index is not None else None

    def _build_message(self) -> tuple[str, str, bytes, tuple[str, ...]] | None:
        """(dest, source, info, digipeaters) from the form, or None (reason on screen)."""
        source = self._get_my_call()
        if not source:
            self._tx_status_label.setText(_("My Call not set — configure it in File > Set QTH"))
            return None
        text = self._message_edit.text()
        if not text:
            self._tx_status_label.setText(_("Message is required"))
            return None
        try:
            info = text.encode("ascii")
        except UnicodeEncodeError:
            self._tx_status_label.setText(_("Message must be ASCII"))
            return None
        dest = self._dest_combo.currentText().strip().upper()
        return dest, source, info, self._digipeaters()

    def _on_send(self) -> None:
        message = self._build_message()
        if message is None:
            return
        dest, source, info, digis = message
        identity = self._identity_edit.text().strip()
        try:
            _skylink, audio = build_uplink(dest, source, info, digis, identity=identity)
        except ValueError as exc:
            self._tx_status_label.setText(str(exc))
            return
        except RsUnavailableError:
            self._tx_status_label.setText(
                _("Reed-Solomon library (reed-solomon-ccsds) not installed")
            )
            return
        self._save_settings()
        if self._mode_combo.currentData() == _MODE_AUTO:
            self._armed = (dest, source, info, digis, identity)
            self._tx_status_label.setText(
                _("Armed: will be sent between satellite windows ({s:.2f} s of audio)").format(
                    s=len(audio) / _AUDIO_RATE
                )
            )
            return
        self._transmit(dest, source, info, digis, identity)

    def _on_cancel_armed(self) -> None:
        if self._armed is not None:
            self._armed = None
            self._tx_status_label.setText(_("Armed message cancelled"))
        self._cancel_btn.setEnabled(False)

    def _try_armed(self, now: float) -> None:
        """Auto mode: send the armed message once the satellite's windows leave room for it."""
        armed = self._armed
        if armed is None:
            return
        dest, source, info, digis, identity = armed
        try:
            _skylink, audio = build_uplink(dest, source, info, digis, identity=identity)
        except (ValueError, RsUnavailableError):
            self._armed = None
            return
        tx_len = len(audio) / _AUDIO_RATE
        # The audio starts PTT_LEAD_S after the PTT goes up, so judge the audio's start time.
        advice = self._tdd.advice(
            now + PTT_LEAD_S, tx_len, guard_s=self._guard_spin.value() / 1000.0
        )
        if not advice.go:
            return
        self._armed = None
        self._transmit(dest, source, info, digis, identity)

    def _transmit(
        self, dest: str, source: str, info: bytes, digis: tuple[str, ...], identity: str
    ) -> None:
        if self._tx_in_progress:
            self._tx_status_label.setText(_("A transmission is already in progress"))
            return
        try:
            _skylink, audio = build_uplink(dest, source, info, digis, identity=identity)
        except ValueError as exc:
            self._tx_status_label.setText(str(exc))
            return
        except RsUnavailableError:
            self._tx_status_label.setText(
                _("Reed-Solomon library (reed-solomon-ccsds) not installed")
            )
            return
        rig = self._tx_rig()
        if rig is None or not getattr(rig, "is_connected", False):
            self._tx_status_label.setText(_("TX rig not connected"))
            return
        out_device = self._output_device()
        if out_device is None:
            self._tx_status_label.setText(
                _("Cannot transmit: set the Sound Card output in Rig Settings")
            )
            return
        # The sound card runs at the shared audio rate; the uplink audio is built at it.
        audio = (audio * np.float32(db_to_gain(self._level_slider.value()))).astype(np.float32)
        worker = PttAudioTxWorker("Foresail-1p digipeater", audio, out_device, rig)
        worker.finished.connect(self._on_tx_finished)
        worker.error.connect(self._on_tx_error)
        self._tx_worker = worker  # keep a reference: it emits from its thread
        self._tx_in_progress = True
        self._tx_thread = threading.Thread(target=worker.run, daemon=True)
        self._tx_thread.start()
        text = info.decode("ascii", errors="replace")
        self._tx_status_label.setText(_("TX: ") + text)
        via = ",".join(digis)
        self._append_row(_("Sent"), source, dest, via, text, b"", persist=False)

    @Slot()
    def _on_tx_finished(self) -> None:
        self._tx_in_progress = False
        self._tx_status_label.setText(_("TX done — watch for the repeated frame"))

    @Slot(str)
    def _on_tx_error(self, msg: str) -> None:
        self._tx_in_progress = False
        self._tx_status_label.setText(_("TX error: ") + msg)

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """Release the SDR subscription (tab closed)."""
        self._timer.stop()
        self._save_settings()
        thread = self._tx_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                rig = self._tx_rig()
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.set_ptt(False)
        self.stop_input()
