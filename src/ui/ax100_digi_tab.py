"""Communications > AX100 Digipeater tab (GreenCube/MARMOTSat-compatible).

Receive-only (Phase 1): GMSK demodulation of SDR-fed I/Q, AX100 "ASM+Golay"
frame decode (Golay(24,12) length field, CCSDS descrambler, Reed-
Solomon(255,223), CSP transport), and best-effort interpretation of the
GreenCube/MARMOTSat store-and-forward message format.

Not yet validated against a real captured signal (see CLAUDE.md's Phase
0/1 plan). TX (sending relay requests, matching GreenCube Terminal's
Call To/Content/Send) is not yet implemented — a future addition needs
Rig 1 + PTT, following the same pattern as FT4/Q65.
"""

from __future__ import annotations

import contextlib
import datetime
import sqlite3
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.ax100digi.engine import Ax100DigiReceiver, DecodedDigiFrame
from i18n import _

_POLL_INTERVAL_MS = 1_000
_MAX_LOG_ROWS = 500


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

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_decode)

        self._connect_sdr_audio()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            _(
                'AX100 "ASM+Golay" digipeater/telemetry receiver — GMSK 1200 baud, '
                "Golay(24,12) + Reed-Solomon(255,223), CSP transport. Compatible with "
                "GreenCube (IO-117) and MARMOTSat. Receive only — sending relay "
                "requests is not yet implemented."
            )
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self._status_label = QLabel(_("Input: SDR not connected"))
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            [_("Time (UTC)"), _("Src"), _("Dst"), _("Sat"), _("Message")]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

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
    # SDR I/Q
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
                return
            sample_rate = int(pipeline._device.sample_rate)
        except AttributeError:
            self._status_label.setText(_("Input: cannot determine SDR sample rate"))
            return

        self._sdr_pipeline = pipeline
        self._receiver = Ax100DigiReceiver(sample_rate=sample_rate)
        pipeline.subscribe(self._receiver.push_samples)
        self._status_label.setText(_("Input: SDR connected"))
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
    # Cleanup
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._disconnect_sdr_audio()
        super().closeEvent(event)
