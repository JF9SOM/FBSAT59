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
import datetime
import json
import sqlite3
import threading
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QFormLayout,
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

from comms.audio_device_manager import get_audio_device_manager
from comms.ax100digi.audio_bridge import RxAudioBridge
from comms.ax100digi.engine import Ax100DigiReceiver, DecodedDigiFrame
from comms.ax100digi.tx import DEFAULT_CSP_HEADER, build_tx_audio
from i18n import _

_POLL_INTERVAL_MS = 1_000
_MAX_LOG_ROWS = 500
_SOUNDCARD_SAMPLE_RATE = 48_000
_AUDIO_OWNER = "AX100 Digipeater"
_SETTINGS_KEY = "ax100digi_settings"
_PTT_LEAD_S = 0.20  # GreenCube config.ini's KeyUpDelay default (200ms)
_PTT_TAIL_S = 0.50  # GreenCube config.ini's KeyDownDelay default (500ms)


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

        self._load_settings()
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_decode)

        self._apply_input_source()

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_SETTINGS_KEY,)
        ).fetchone()
        data: dict[str, Any] = json.loads(row[0]) if row else {}
        self._my_call = data.get("my_call", "")
        if not self._my_call:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'callsign'"
            ).fetchone()
            self._my_call = str(r[0]) if r else ""
        self._dest_call = data.get("dest_call", "")
        self._sat_name = data.get("sat_name", "MARMOTSat")
        self._rx_source = data.get("rx_source", "soundcard")

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
                "my_call": self._call_edit.text().strip(),
                "dest_call": self._dest_edit.text().strip(),
                "sat_name": self._sat_edit.text().strip(),
                "rx_source": "soundcard" if self._rb_soundcard.isChecked() else "sdr",
            }
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_SETTINGS_KEY, data),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            _(
                'AX100 "ASM+Golay" digipeater — GMSK 1200 baud, Golay(24,12) + '
                "Reed-Solomon(255,223), CSP transport. Compatible with GreenCube "
                "(IO-117) and MARMOTSat. Set the rig to SSB mode on the digipeater "
                "frequency (145.875 MHz for MARMOTSat)."
            )
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel(_("Input/Output:")))
        self._rb_soundcard = QRadioButton(_("Rig Soundcard"))
        self._rb_sdr = QRadioButton(_("SDR (receive only)"))
        if self._rx_source == "sdr":
            self._rb_sdr.setChecked(True)
        else:
            self._rb_soundcard.setChecked(True)
        source_row.addWidget(self._rb_soundcard)
        source_row.addWidget(self._rb_sdr)
        source_row.addStretch(1)
        layout.addLayout(source_row)
        self._rb_soundcard.toggled.connect(self._on_source_changed)

        self._status_label = QLabel(_("Input: not connected"))
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            [_("Time (UTC)"), _("Src"), _("Dst"), _("Sat"), _("Message")]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        layout.addWidget(self._build_tx_group())

    def _build_tx_group(self) -> QGroupBox:
        group = QGroupBox(_("Send Message"))
        form = QFormLayout(group)

        self._call_edit = QLineEdit(self._my_call)
        form.addRow(_("My Call:"), self._call_edit)

        self._dest_edit = QLineEdit(self._dest_call)
        form.addRow(_("To:"), self._dest_edit)

        self._sat_edit = QLineEdit(self._sat_name)
        form.addRow(_("Satellite:"), self._sat_edit)

        self._store_spin = QSpinBox()
        self._store_spin.setRange(0, 172_800)  # GreenCube's 2-day max relay delay
        self._store_spin.setSuffix(_(" s (0 = immediate)"))
        form.addRow(_("STORE=:"), self._store_spin)

        self._content_edit = QLineEdit()
        self._content_edit.setPlaceholderText(_("Message content"))
        form.addRow(_("Content:"), self._content_edit)

        send_row = QHBoxLayout()
        self._send_btn = QPushButton(_("Send"))
        self._send_btn.clicked.connect(self._on_send)
        send_row.addWidget(self._send_btn)
        self._tx_status_label = QLabel("")
        send_row.addWidget(self._tx_status_label)
        send_row.addStretch(1)
        form.addRow(send_row)

        warning = QLabel(
            _(
                "CSP addressing used for outgoing frames is not yet confirmed "
                "against a real satellite — see comms/ax100digi/tx.py."
            )
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b8860b;")
        form.addRow(warning)

        return group

    def _append_row(self, decoded: DecodedDigiFrame) -> None:
        now = datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")
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
        pipeline.subscribe(self._receiver.push_samples)
        self._status_label.setText(_("Input: SDR connected (receive only)"))
        self._timer.start()

    def _disconnect_sdr_audio(self) -> None:
        self._timer.stop()
        if self._sdr_pipeline is not None and self._receiver is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.unsubscribe(self._receiver.push_samples)
        self._sdr_pipeline = None
        self._receiver = None

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
        my_call = self._call_edit.text().strip()
        dest_call = self._dest_edit.text().strip()
        sat_name = self._sat_edit.text().strip()
        content = self._content_edit.text().strip()
        if not my_call or not dest_call or not content:
            self._tx_status_label.setText(_("My Call, To, and Content are required"))
            return
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
                csp_header=DEFAULT_CSP_HEADER,
                sample_rate=_SOUNDCARD_SAMPLE_RATE,
            )
        except ValueError as exc:
            self._tx_status_label.setText(str(exc))
            return

        self._save_settings()

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
