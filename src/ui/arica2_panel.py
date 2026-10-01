"""ARICA-2 message-box panel (hosted by the Message Box/Digipeater tab).

ARICA-2 (JS1YSD) keeps 20 eight-character messages for 48 hours and accepts
uplink commands (Upload / Confirm / Parrot / Download) only for about 15 seconds
right after its CW beacon stops, on 436.830 MHz, 4800 baud G3RUH GMSK.

Receive and transmit both go through the shared Direwolf session of
``AprsEngine`` (MODEM 4800 G3RUH):

  - SDR: the SDR receives (and its I/Q also feeds the beacon-end detector that
    tells when the uplink window is open); the rig transmits through the Sound
    Card output.
  - Rig Sound Card: receive and transmit through the sound card. There is no
    beacon detection on this path, so uplinks are manual only.

Frame layouts come from a reverse-engineered third-party tool, see
comms/arica2/message_box.py.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.engine import get_aprs_engine
from comms.arica2.message_box import (
    CALLSIGN_LEN,
    MAX_SLOT,
    MESSAGE_LEN,
    Command,
    build_command,
    parse_downlink,
)
from comms.arica2.window_detector import BeaconWindowDetector
from i18n import _
from rig.controller import select_tx_rig

_OWNER = "ARICA-2 Message Box"
_SETTINGS_KEY = "arica2_settings"
_MODEM = "4800"
_MAX_LOG_ROWS = 500
_UI_TICK_MS = 200
# Direwolf TXDELAY (300 ms) + a ~25 byte frame + TXTAIL, with margin.
_TX_AUDIO_S = 0.6
# An armed command is sent this long after the window opens (rig/Doppler settle).
_AUTO_SEND_DELAY_S = 0.5
# Not worth sending when less of the window than this is left.
_MIN_REMAINING_S = 2.5

_MODE_MANUAL = "manual"
_MODE_AUTO = "auto"


class Arica2Panel(QWidget):
    """Send commands to and read responses from ARICA-2's message box."""

    # Detector events cross from the SDR pipeline thread to the Qt thread.
    _window_event: Signal = Signal(float, float)

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
        self._engine = get_aprs_engine(conn)
        self._sdr_pipeline: Any = None
        self._detector: BeaconWindowDetector | None = None
        self._engine_active = False
        self._window_deadline: float | None = None
        self._armed: tuple[Command, str, int | None] | None = None
        self._use_utc = True

        self._ensure_table()
        self._load_settings()
        self._build_ui()

        self._window_event.connect(self._on_window_event)
        self._engine.raw_frame_received.connect(self._on_raw_frame)
        self._engine.error_occurred.connect(self._on_engine_error)

        self._timer = QTimer(self)
        self._timer.setInterval(_UI_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        if autostart:
            self._apply_input_source()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _ensure_table(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS arica2_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at DATETIME NOT NULL,
                source      TEXT,
                dest        TEXT,
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
        self._rx_source = "soundcard" if data.get("rx_source") == "soundcard" else "sdr"
        self._send_mode = _MODE_AUTO if data.get("send_mode") == _MODE_AUTO else _MODE_MANUAL
        self._last_message = str(data.get("last_message", ""))

        tz = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone_mode'"
        ).fetchone()
        self._use_utc = (tz["value"] if tz and tz["value"] else "utc") != "local"

    def _save_settings(self) -> None:
        data = json.dumps(
            {
                "rx_source": "soundcard" if self._rb_soundcard.isChecked() else "sdr",
                "send_mode": self._mode_combo.currentData(),
                "last_message": self._message_edit.text(),
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
        info = QLabel(_("ARICA-2 (JS1YSD) message box ℹ"))
        info.setToolTip(
            _(
                "436.830 MHz, 4800 baud G3RUH GMSK (uplink and downlink). 20 slots, "
                "8 characters each, kept for 48 hours. Uplink works only for about "
                "15 seconds right after the CW beacon stops. Frame format is "
                "reverse engineered from the official Windows tool and is not "
                "officially documented."
            )
        )
        top.addWidget(info)
        top.addStretch(1)
        top.addWidget(QLabel(_("Receive:")))
        self._rb_sdr = QRadioButton(_("SDR (transmit via rig)"))
        self._rb_soundcard = QRadioButton(_("Rig Soundcard"))
        (self._rb_soundcard if self._rx_source == "soundcard" else self._rb_sdr).setChecked(True)
        top.addWidget(self._rb_sdr)
        top.addWidget(self._rb_soundcard)
        layout.addLayout(top)
        self._rb_soundcard.toggled.connect(self._on_source_changed)

        self._status_label = QLabel(_("Input: not connected"))
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(
            [self._time_column_label(), _("From"), _("To"), _("Message")]
        )
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        layout.addWidget(self._build_window_group())
        layout.addWidget(self._build_send_group())

    def _build_window_group(self) -> QGroupBox:
        group = QGroupBox(_("Uplink window"))
        row = QHBoxLayout(group)
        self._window_label = QLabel(_("Window: no beacon seen yet"))
        row.addWidget(self._window_label, stretch=1)
        row.addWidget(QLabel(_("Send mode:")))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem(_("Manual (send now)"), _MODE_MANUAL)
        self._mode_combo.addItem(_("Auto (send at next window)"), _MODE_AUTO)
        self._mode_combo.setCurrentIndex(1 if self._send_mode == _MODE_AUTO else 0)
        self._mode_combo.setToolTip(
            _(
                "Manual sends the moment you press a button. Auto holds the command "
                "and sends it as soon as the beacon ends (needs the SDR input, which "
                "watches the beacon)."
            )
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(self._mode_combo)
        return group

    def _build_send_group(self) -> QGroupBox:
        group = QGroupBox(_("Message box"))
        v = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel(_("Message:")))
        self._message_edit = QLineEdit(self._last_message)
        self._message_edit.setMaxLength(MESSAGE_LEN)
        self._message_edit.setMaximumWidth(120)
        self._message_edit.setPlaceholderText(_("max 8 chars"))
        row.addWidget(self._message_edit)
        self._call_label = QLabel()
        row.addWidget(self._call_label)
        row.addStretch(1)
        v.addLayout(row)

        buttons = QHBoxLayout()
        self._upload_btn = QPushButton(_("Upload"))
        self._upload_btn.setToolTip(_("Store the message in the next free slot"))
        self._confirm_btn = QPushButton(_("Confirm"))
        self._confirm_btn.setToolTip(_("List the occupied slots"))
        self._parrot_btn = QPushButton(_("Parrot"))
        self._parrot_btn.setToolTip(_("The satellite echoes the message straight back"))
        self._download_btn = QPushButton(_("Download"))
        self._download_btn.setToolTip(
            _("Read back the message with this ID (1-20). Only Download uses it")
        )
        self._slot_spin = QSpinBox()
        self._slot_spin.setRange(1, MAX_SLOT)
        self._slot_spin.setPrefix(_("Message ID "))
        self._cancel_btn = QPushButton(_("Cancel armed"))
        self._cancel_btn.setEnabled(False)

        self._upload_btn.clicked.connect(lambda: self._on_command(Command.UPLOAD))
        self._confirm_btn.clicked.connect(lambda: self._on_command(Command.CONFIRM))
        self._parrot_btn.clicked.connect(lambda: self._on_command(Command.PARROT))
        self._download_btn.clicked.connect(lambda: self._on_command(Command.DOWNLOAD))
        self._cancel_btn.clicked.connect(self._on_cancel_armed)
        for w in (
            self._upload_btn,
            self._confirm_btn,
            self._parrot_btn,
            self._download_btn,
            self._slot_spin,
            self._cancel_btn,
        ):
            buttons.addWidget(w)
        buttons.addStretch(1)
        v.addLayout(buttons)

        self._tx_status_label = QLabel("")
        self._tx_status_label.setWordWrap(True)
        v.addWidget(self._tx_status_label)
        self._refresh_call_label()
        return group

    def _refresh_call_label(self) -> None:
        call = self._get_my_call()
        self._call_label.setText(
            _("From: {call}").format(call=call) if call else _("From: (set in File > Set QTH)")
        )

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

    # ------------------------------------------------------------------ #
    # Engine / input source
    # ------------------------------------------------------------------ #

    def _tx_rig(self) -> Any:
        rc = self._radio_control
        return select_tx_rig(getattr(rc, "_rig1", None), getattr(rc, "_rig2", None))

    def _get_sdr_pipeline(self) -> Any:
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        return getattr(sdr_ctrl, "_pipeline", None)

    @Slot(bool)
    def _on_source_changed(self, _checked: bool) -> None:
        self._stop_input()
        self._apply_input_source()
        self._save_settings()

    def start_input(self) -> None:
        """(Re)start receiving with the selected input source."""
        self._stop_input()
        self._apply_input_source()

    def stop_input(self) -> None:
        """Release the SDR subscription and the shared Direwolf session."""
        self._stop_input()

    def _apply_input_source(self) -> None:
        if self._rb_soundcard.isChecked():
            self._start_soundcard()
        else:
            self._start_sdr()
        self._update_mode_enabled()

    def _start_sdr(self) -> None:
        pipeline = self._get_sdr_pipeline()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR not connected"))
            return
        try:
            sample_rate = float(pipeline._device.sample_rate)
            detector = BeaconWindowDetector(sample_rate)
        except (AttributeError, TypeError, ValueError):
            self._status_label.setText(_("Input: cannot determine SDR sample rate"))
            return
        ok, err = self._engine.start_sdr_direwolf(_OWNER, pipeline, modem=_MODEM, tx=True)
        if not ok:
            self._status_label.setText(_("Direwolf error: {err}").format(err=err))
            return
        self._engine.sync_sdr_baud(pipeline, _MODEM)
        self._engine.set_rig(self._tx_rig())
        self._sdr_pipeline = pipeline
        self._detector = detector
        pipeline.subscribe(self._on_iq_chunk)
        self._engine_active = True
        self._status_label.setText(_("Input: SDR connected (4800 baud), transmit via rig"))

    def _start_soundcard(self) -> None:
        call = self._get_my_call() or "N0CALL"
        ok, err = self._engine.start_rig(_OWNER, call, 0, "", modem=_MODEM)
        if not ok:
            self._status_label.setText(_("Direwolf error: {err}").format(err=err))
            return
        self._engine.restart_if_modem_changed(_MODEM)
        self._engine.set_rig(self._tx_rig())
        self._engine_active = True
        self._status_label.setText(
            _("Input: Rig Soundcard + Direwolf (4800 baud). No beacon detection: manual only")
        )

    def _stop_input(self) -> None:
        if self._sdr_pipeline is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.unsubscribe(self._on_iq_chunk)
        self._sdr_pipeline = None
        self._detector = None
        if self._engine_active:
            self._engine.stop(_OWNER)
        self._engine_active = False
        self._window_deadline = None
        self._armed = None

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """MainWindow calls this whenever the SDR (re)connects or disconnects."""
        if not self._rb_sdr.isChecked():
            return
        self._stop_input()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR disconnected"))
            return
        self._apply_input_source()

    def _on_iq_chunk(self, iq: NDArray[np.complex64]) -> None:
        """SDR pipeline thread: run the detector, hand events to the Qt thread."""
        detector = self._detector
        if detector is None:
            return
        for event in detector.push_samples(iq):
            self._window_event.emit(event.burst_s, event.remaining_s)

    @Slot(str)
    def _on_engine_error(self, msg: str) -> None:
        self._status_label.setText(msg)

    # ------------------------------------------------------------------ #
    # Uplink window
    # ------------------------------------------------------------------ #

    def _window_remaining(self) -> float:
        if self._window_deadline is None:
            return 0.0
        return max(0.0, self._window_deadline - time.monotonic())

    @Slot(float, float)
    def _on_window_event(self, _burst_s: float, remaining_s: float) -> None:
        self._window_deadline = time.monotonic() + remaining_s
        armed = self._armed
        if armed is not None and remaining_s >= _MIN_REMAINING_S:
            self._armed = None
            QTimer.singleShot(
                int(_AUTO_SEND_DELAY_S * 1000), lambda: self._transmit(armed[0], armed[1], armed[2])
            )

    def _tick(self) -> None:
        remaining = self._window_remaining()
        detector = self._detector
        if remaining > 0.0:
            self._window_label.setText(
                _("Window OPEN: {s:.0f} s left").format(s=remaining)
                + (_(" (armed command waiting)") if self._armed else "")
            )
            self._window_label.setStyleSheet("color: #2ecc71; font-weight: bold;")
        elif detector is not None and detector.carrier_present:
            self._window_label.setText(_("Beacon on air: wait for it to stop"))
            self._window_label.setStyleSheet("color: #f1c40f; font-weight: bold;")
        elif detector is not None:
            text = _("Window closed: waiting for the next beacon")
            if self._armed:
                text += _(" (armed command waiting)")
            self._window_label.setText(text)
            self._window_label.setStyleSheet("")
        else:
            self._window_label.setText(_("Window: no beacon detection (manual only)"))
            self._window_label.setStyleSheet("")
        self._cancel_btn.setEnabled(self._armed is not None)

    def _update_mode_enabled(self) -> None:
        """Auto needs the beacon detector, i.e. the SDR input."""
        auto_ok = self._rb_sdr.isChecked()
        model = self._mode_combo.model()
        item = getattr(model, "item", None)
        if item is not None:
            auto_item = item(1)
            if auto_item is not None:
                auto_item.setEnabled(auto_ok)
        if not auto_ok and self._mode_combo.currentData() == _MODE_AUTO:
            self._mode_combo.setCurrentIndex(0)

    @Slot(int)
    def _on_mode_changed(self, _index: int) -> None:
        if self._mode_combo.currentData() == _MODE_MANUAL:
            self._armed = None
        self._save_settings()

    # ------------------------------------------------------------------ #
    # TX
    # ------------------------------------------------------------------ #

    def _on_command(self, command: Command) -> None:
        message = self._message_edit.text().strip()
        slot = self._slot_spin.value() if command is Command.DOWNLOAD else None
        if command in (Command.UPLOAD, Command.PARROT) and not message:
            self._tx_status_label.setText(_("Message is required"))
            return
        try:
            build_command(command, self._get_my_call() or " ", message, slot)
        except ValueError as exc:
            self._tx_status_label.setText(self._describe_error(str(exc)))
            return
        self._save_settings()

        if self._mode_combo.currentData() == _MODE_AUTO:
            if self._window_remaining() >= _MIN_REMAINING_S:
                self._transmit(command, message, slot)
                return
            self._armed = (command, message, slot)
            self._cancel_btn.setEnabled(True)
            self._tx_status_label.setText(
                _("Armed: {cmd} will be sent when the beacon ends").format(cmd=command.value)
            )
            return
        self._transmit(command, message, slot)

    @staticmethod
    def _describe_error(text: str) -> str:
        if "callsign is required" in text:
            return _("My Call not set — configure it in File > Set QTH")
        if "callsign" in text:
            return _("Callsign must be at most {n} characters").format(n=CALLSIGN_LEN)
        if "message" in text:
            return _("Message must be ASCII, at most {n} characters").format(n=MESSAGE_LEN)
        return text

    def _on_cancel_armed(self) -> None:
        self._armed = None
        self._cancel_btn.setEnabled(False)
        self._tx_status_label.setText(_("Armed command cancelled"))

    def _transmit(self, command: Command, message: str, slot: int | None) -> None:
        my_call = self._get_my_call()
        try:
            payload = build_command(command, my_call, message, slot)
        except ValueError as exc:
            self._tx_status_label.setText(self._describe_error(str(exc)))
            return
        rig = self._tx_rig()
        if rig is None or not getattr(rig, "is_connected", False):
            self._tx_status_label.setText(_("TX rig not connected"))
            return
        self._engine.set_rig(rig)
        if not self._engine.can_transmit:
            self._tx_status_label.setText(
                _("Cannot transmit: check the Sound Card output in Rig Settings")
            )
            return
        if not self._engine.send_raw(payload, audio_s=_TX_AUDIO_S):
            self._tx_status_label.setText(_("Direwolf is not running"))
            return
        text = command.value + (f" {message}" if message else "")
        if slot is not None:
            text += f" message ID {slot}"
        self._tx_status_label.setText(_("TX: ") + text)
        self._append_row(my_call, "JS1YSD", "→ " + text, payload, persist=False)

    # ------------------------------------------------------------------ #
    # RX
    # ------------------------------------------------------------------ #

    @Slot(bytes)
    def _on_raw_frame(self, raw: bytes) -> None:
        if not self._engine_active:
            return
        parsed = parse_downlink(raw)
        if parsed is None:
            self._append_row("?", "", raw.hex(), raw)
        else:
            self._append_row(parsed.source, parsed.dest, parsed.text, raw)

    def _append_row(self, src: str, dest: str, text: str, raw: bytes, persist: bool = True) -> None:
        now_utc = datetime.datetime.now(datetime.UTC)
        if persist:
            self._conn.execute(
                "INSERT INTO arica2_log (received_at, source, dest, content, raw_hex) "
                "VALUES (?, ?, ?, ?, ?)",
                (now_utc.isoformat(), src, dest, text, raw.hex()),
            )
            self._conn.commit()
        shown = now_utc if self._use_utc else now_utc.astimezone()
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, value in enumerate((shown.strftime("%H:%M:%S"), src, dest, text)):
            self._table.setItem(row, col, QTableWidgetItem(value))
        self._table.scrollToBottom()
        while self._table.rowCount() > _MAX_LOG_ROWS:
            self._table.removeRow(0)

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """Release the engine and the SDR subscription (tab closed)."""
        self._timer.stop()
        self._save_settings()
        with contextlib.suppress(Exception):
            self._engine.raw_frame_received.disconnect(self._on_raw_frame)
        with contextlib.suppress(Exception):
            self._engine.error_occurred.disconnect(self._on_engine_error)
        self._stop_input()
