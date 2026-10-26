"""ERMINAZ-2 digipeater panel (hosted by the Message Box/Digipeater tab).

ERMINAZ-2U / 2V / 2X (AMSAT-DL pico-satellites, DP0PQA / DP0PQB / DP0PQC) carry an amateur
digipeater: a short text message (destination, source, up to 66 characters) sent to the
satellite is repeated on its downlink. The satellite also beacons ``... DIGIPEATER ON`` every
45 s while the digipeater is enabled. Downlink: 9600 baud GMSK, CCSDS TM frames; uplink: a CCSDS
TC frame (see ``comms.erminaz.frames``).

  - Receive (selectable, as in the other panels): the SDR's I/Q is FM demodulated and sliced, or
    the rig's Sound Card audio (the 9600 baud data port's discriminator output) is sliced.
  - Transmit: through the Sound Card output (the radio does the FSK modulation), manual only.

The uplink has so far only been verified against this app's own receiver, not on the air.
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
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager
from comms.audio_tx import AUDIO_RATE as _AUDIO_RATE
from comms.audio_tx import PttAudioTxWorker
from comms.ax100digi.rs_ccsds import RsUnavailableError
from comms.erminaz.frames import (
    DEFAULT_RADIO_CONF,
    MAX_MESSAGE_LENGTH,
    SPACECRAFT,
    describe_radio_conf,
    parse_digipeater_frame,
)
from comms.erminaz.rx import ErminazAudioDemod, ErminazRxFrame, ErminazSdrDemod
from comms.erminaz.tx import build_uplink
from i18n import _
from rig.controller import select_tx_rig
from ui.tx_level import (
    TX_LEVEL_MAX_DB,
    TX_LEVEL_MIN_DB,
    db_to_gain,
    format_db,
    load_level_db,
)

_SETTINGS_KEY = "erminaz_settings"
_OWNER_AUDIO = "ERMINAZ-2 receiver"
_MAX_LOG_ROWS = 500
_UI_TICK_MS = 500
# The satellite counts as "heard recently" for this long after its last digipeater frame.
_HEARD_S = 120.0
_COLOR_GREEN = "#2ecc71"


class ErminazPanel(QWidget):
    """Receive the ERMINAZ-2 digipeater downlink and send messages through it."""

    # Emitted when the satellite combo changes: the tab asks MainWindow to select it.
    satellite_changed: Signal = Signal()

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
        self._demod: ErminazSdrDemod | None = None
        self._audio_device: int | None = None
        self._tx_in_progress = False
        self._tx_thread: threading.Thread | None = None
        self._tx_worker: PttAudioTxWorker | None = None
        self._use_utc = True
        self._frames_total = 0
        self._digi_total = 0
        self._last_digi: float | None = None

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
            """CREATE TABLE IF NOT EXISTS erminaz_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at DATETIME NOT NULL,
                spacecraft  INTEGER,
                source      TEXT,
                dest        TEXT,
                content     TEXT,
                radio_conf  INTEGER,
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
        satellite = str(data.get("satellite", "ERMINAZ-2U"))
        self._satellite = satellite if satellite in SPACECRAFT else "ERMINAZ-2U"
        self._dest = str(data.get("dest", "ALL"))
        self._last_message = str(data.get("last_message", ""))
        self._tx_level_db = load_level_db(data)
        tz = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone_mode'"
        ).fetchone()
        self._use_utc = (tz["value"] if tz and tz["value"] else "utc") != "local"

    def _save_settings(self) -> None:
        data = json.dumps(
            {
                "rx_source": "soundcard" if self._rb_soundcard.isChecked() else "sdr",
                "satellite": self._sat_combo.currentData(),
                "dest": self._dest_edit.text().strip(),
                "last_message": self._message_edit.text(),
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

    def current_norad(self) -> int:
        """Catalogue number of the selected satellite."""
        return SPACECRAFT[str(self._sat_combo.currentData())][1]

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        info = QLabel(_("ERMINAZ-2 digipeater ℹ"))
        info.setToolTip(
            _(
                "ERMINAZ-2U (435.900 MHz), 2V (145.920 MHz) and 2X (145.960 MHz), 9600 baud "
                "GMSK. A short message (destination, source, up to {n} characters) sent to the "
                "satellite is repeated on its downlink; while the digipeater is enabled the "
                "satellite beacons 'DIGIPEATER ON' every 45 s. Receive with the SDR (FM "
                "demodulated) or the rig's Sound Card; the rig always transmits through the "
                "Sound Card. The uplink format is taken from the published ground station and "
                "firmware and has not been tried on the air."
            ).format(n=MAX_MESSAGE_LENGTH)
        )
        top.addWidget(info)
        top.addWidget(QLabel(_("Satellite:")))
        self._sat_combo = QComboBox()
        for name in SPACECRAFT:
            self._sat_combo.addItem(name, name)
        self._sat_combo.setCurrentIndex(max(0, self._sat_combo.findData(self._satellite)))
        self._sat_combo.currentIndexChanged.connect(self._on_satellite_changed)
        top.addWidget(self._sat_combo)
        top.addStretch(1)
        top.addWidget(QLabel(_("Receive:")))
        self._rb_sdr = QRadioButton(_("SDR (transmit via rig)"))
        self._rb_soundcard = QRadioButton(_("Rig Soundcard"))
        (self._rb_soundcard if self._rx_source == "soundcard" else self._rb_sdr).setChecked(True)
        top.addWidget(self._rb_sdr)
        top.addWidget(self._rb_soundcard)
        self._rb_soundcard.toggled.connect(self._on_source_changed)
        layout.addLayout(top)

        self._status_label = QLabel(_("Input: not connected"))
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._heard_label = QLabel(_("Digipeater: not heard yet"))
        layout.addWidget(self._heard_label)

        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            [
                self._time_column_label(),
                _("Satellite"),
                _("From"),
                _("To"),
                _("Message"),
                _("Radio config"),
            ]
        )
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        layout.addWidget(self._build_send_group())

    def _build_send_group(self) -> QGroupBox:
        group = QGroupBox(_("Digipeater message"))
        v = QVBoxLayout(group)

        row = QHBoxLayout()
        self._call_label = QLabel()
        row.addWidget(self._call_label)
        row.addWidget(QLabel(_("To:")))
        self._dest_edit = QLineEdit(self._dest)
        self._dest_edit.setMaxLength(6)
        self._dest_edit.setMaximumWidth(90)
        self._dest_edit.setToolTip(
            _("Destination (up to 6 characters, no SSID). The satellite's own beacon uses ALL.")
        )
        row.addWidget(self._dest_edit)
        row.addStretch(1)
        row.addWidget(QLabel(_("TX Level:")))
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
        row.addWidget(self._level_slider)
        row.addWidget(self._level_label)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel(_("Message:")))
        self._message_edit = QLineEdit(self._last_message)
        self._message_edit.setPlaceholderText(_("Text to be repeated by the satellite"))
        self._message_edit.textChanged.connect(self._update_length_label)
        row2.addWidget(self._message_edit, stretch=1)
        self._length_label = QLabel()
        self._length_label.setFixedWidth(60)
        row2.addWidget(self._length_label)
        self._send_btn = QPushButton(_("Send"))
        self._send_btn.clicked.connect(self._on_send)
        row2.addWidget(self._send_btn)
        v.addLayout(row2)

        self._tx_status_label = QLabel(
            _("Radio config sent: {conf}").format(conf=describe_radio_conf(DEFAULT_RADIO_CONF))
        )
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

    def _update_length_label(self) -> None:
        used = len(self._message_edit.text().encode("ascii", errors="replace"))
        self._length_label.setText(f"{used}/{MAX_MESSAGE_LENGTH}")
        self._length_label.setStyleSheet("color: #e74c3c;" if used > MAX_MESSAGE_LENGTH else "")

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
    def _on_satellite_changed(self, _index: int) -> None:
        self._save_settings()
        self.satellite_changed.emit()

    # ------------------------------------------------------------------ #
    # Input
    # ------------------------------------------------------------------ #

    def _get_sdr_pipeline(self) -> Any:
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        return getattr(sdr_ctrl, "_pipeline", None)

    @Slot(bool)
    def _on_source_changed(self, _checked: bool) -> None:
        self.start_input()
        self._save_settings()

    def _soundcard_device(self, key: str) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if not row:
            return None
        try:
            index = json.loads(row[0]).get(key)
        except (ValueError, AttributeError):
            return None
        return int(index) if index is not None else None

    def start_input(self) -> None:
        """(Re)start receiving with the selected input source."""
        self.stop_input()
        if self._rb_soundcard.isChecked():
            self._start_soundcard()
        else:
            self._start_sdr()

    def _start_sdr(self) -> None:
        pipeline = self._get_sdr_pipeline()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR not connected"))
            return
        try:
            demod = ErminazSdrDemod(int(pipeline._device.sample_rate), parent=self)
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

    def _start_soundcard(self) -> None:
        device = self._soundcard_device("input_device_index")
        if device is None:
            self._status_label.setText(
                _("Input: set the Sound Card input in Rig Settings (9600 baud data output)")
            )
            return
        demod = ErminazAudioDemod(parent=self)
        demod.frame_received.connect(self._on_frame)
        demod.start()
        try:
            get_audio_device_manager().acquire_input(
                _OWNER_AUDIO, device, _AUDIO_RATE, demod.push_samples
            )
        except Exception as exc:
            demod.stop()
            self._status_label.setText(_("Audio open error: {exc}").format(exc=exc))
            return
        self._demod = demod
        self._audio_device = device
        self._status_label.setText(_("Input: Rig Soundcard (9600 baud data output, 48 kHz)"))

    def stop_input(self) -> None:
        """Release the SDR subscription / the sound card input."""
        demod, self._demod = self._demod, None
        if self._sdr_pipeline is not None and demod is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.unsubscribe(demod.push_samples)
        self._sdr_pipeline = None
        device, self._audio_device = self._audio_device, None
        if device is not None:
            with contextlib.suppress(Exception):
                get_audio_device_manager().release_input(_OWNER_AUDIO, device)
        if demod is not None:
            demod.stop()

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """MainWindow calls this whenever the SDR (re)connects or disconnects."""
        if not self._rb_sdr.isChecked():
            return
        self.stop_input()
        if pipeline is None:
            self._status_label.setText(_("Input: SDR disconnected"))
            return
        self.start_input()

    # ------------------------------------------------------------------ #
    # RX
    # ------------------------------------------------------------------ #

    @Slot(object, float)
    def _on_frame(self, rx: ErminazRxFrame, _wall_end: float) -> None:
        self._frames_total += 1
        message = parse_digipeater_frame(rx.payload)
        if message is None:
            return  # telemetry etc.: the Telemetry tab shows those
        self._digi_total += 1
        self._last_digi = time.monotonic()
        satellite = next(
            (name for name, (scid, _n) in SPACECRAFT.items() if scid == message.spacecraft_id),
            f"SCID {message.spacecraft_id}",
        )
        self._append_row(
            satellite,
            message.source,
            message.destination,
            message.text,
            message.radio_conf,
            rx.payload,
        )

    def _append_row(
        self,
        satellite: str,
        source: str,
        dest: str,
        text: str,
        radio_conf: int | None,
        raw: bytes,
        persist: bool = True,
    ) -> None:
        now_utc = datetime.datetime.now(datetime.UTC)
        if persist:
            self._conn.execute(
                "INSERT INTO erminaz_log (received_at, spacecraft, source, dest, content, "
                "radio_conf, raw_hex) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now_utc.isoformat(),
                    SPACECRAFT.get(satellite, (0, 0))[0],
                    source,
                    dest,
                    text,
                    radio_conf,
                    raw.hex(),
                ),
            )
            self._conn.commit()
        shown = now_utc if self._use_utc else now_utc.astimezone()
        conf = describe_radio_conf(radio_conf) if radio_conf is not None else ""
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col, value in enumerate(
            (shown.strftime("%H:%M:%S"), satellite, source, dest, text, conf)
        ):
            self._table.setItem(row, col, QTableWidgetItem(value))
        self._table.scrollToBottom()
        while self._table.rowCount() > _MAX_LOG_ROWS:
            self._table.removeRow(0)

    def _tick(self) -> None:
        counts = _("frames {n} (digipeater {d})").format(n=self._frames_total, d=self._digi_total)
        if self._last_digi is not None and time.monotonic() - self._last_digi < _HEARD_S:
            self._heard_label.setText(_("Digipeater heard recently") + f"  ·  {counts}")
            self._heard_label.setStyleSheet(f"color: {_COLOR_GREEN}; font-weight: bold;")
        elif self._digi_total:
            self._heard_label.setText(_("Digipeater quiet") + f"  ·  {counts}")
            self._heard_label.setStyleSheet("")
        else:
            self._heard_label.setText(_("Digipeater: not heard yet") + f"  ·  {counts}")
            self._heard_label.setStyleSheet("")

    # ------------------------------------------------------------------ #
    # TX
    # ------------------------------------------------------------------ #

    def _tx_rig(self) -> Any:
        rc = self._radio_control
        return select_tx_rig(getattr(rc, "_rig1", None), getattr(rc, "_rig2", None))

    def _on_send(self) -> None:
        source = self._get_my_call()
        if not source:
            self._tx_status_label.setText(_("My Call not set — configure it in File > Set QTH"))
            return
        text = self._message_edit.text()
        if not text:
            self._tx_status_label.setText(_("Message is required"))
            return
        dest = self._dest_edit.text().strip().upper() or "ALL"
        name = str(self._sat_combo.currentData())
        try:
            _frame, audio = build_uplink(SPACECRAFT[name][0], dest, source, text)
        except ValueError as exc:
            self._tx_status_label.setText(str(exc))
            return
        except RsUnavailableError:
            self._tx_status_label.setText(
                _("Reed-Solomon library (reed-solomon-ccsds) not installed")
            )
            return
        if self._tx_in_progress:
            self._tx_status_label.setText(_("A transmission is already in progress"))
            return
        rig = self._tx_rig()
        if rig is None or not getattr(rig, "is_connected", False):
            self._tx_status_label.setText(_("TX rig not connected"))
            return
        out_device = self._soundcard_device("output_device_index")
        if out_device is None:
            self._tx_status_label.setText(
                _("Cannot transmit: set the Sound Card output in Rig Settings")
            )
            return
        self._save_settings()
        audio = (audio * np.float32(db_to_gain(self._level_slider.value()))).astype(np.float32)
        worker = PttAudioTxWorker("ERMINAZ-2 digipeater", audio, out_device, rig)
        worker.finished.connect(self._on_tx_finished)
        worker.error.connect(self._on_tx_error)
        self._tx_worker = worker  # keep a reference: it emits from its thread
        self._tx_in_progress = True
        self._tx_thread = threading.Thread(target=worker.run, daemon=True)
        self._tx_thread.start()
        self._tx_status_label.setText(_("TX: ") + text)
        self._append_row(_("Sent"), source, dest, text, DEFAULT_RADIO_CONF, b"", persist=False)

    @Slot()
    def _on_tx_finished(self) -> None:
        self._tx_in_progress = False
        self._tx_status_label.setText(_("TX done — watch for the repeated message"))

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
