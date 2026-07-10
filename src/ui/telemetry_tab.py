"""Telemetry tab widget — Communications > Telemetry.

Receives AX.25 frames from:
  - Bell 202 AFSK Python demodulator (SDR receive path)
  - Direwolf / KISS (via Rig + Sound Card)
  - gr-satellites subprocess (SDR path, 300+ satellites including 9k6 FSK)

Decodes frames using JSON format definitions in
src/data/telemetry_formats/{norad}.json.
Satellites without a definition show raw hex.

All received frames are persisted to the ``telemetry_log`` SQLite table
and can be exported as CSV.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.engine import AX25_BAUD_SETTING_KEY, get_aprs_engine, resolve_ax25_modem
from comms.aprs.parser import decode_ax25
from comms.telemetry.decoder import TelemetryFrame, decode_telemetry, list_formats
from comms.telemetry.gr_satellites_backend import (
    GrSatellitesBackend,
    detect_gr_satellites,
    get_satellite_info,
    list_gr_satellites_with_names,
)
from i18n import _

_MODE_AFSK = "Bell 202 AFSK"
_MODE_GR = "gr-satellites"

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine).
# The APRS tab shares the same engine under its own "aprs" tag so closing
# one tab doesn't stop the other's reception.
_ENGINE_OWNER = "telemetry"


class TelemetryTab(QWidget):
    """Non-resident tab opened from Communications > Telemetry."""

    # emitted when user picks a satellite in either combo: (norad, mode_str)
    satellite_selected = Signal(int, str)

    def __init__(
        self,
        conn: Any,
        radio_control: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control

        # AFSK backend state — shared with the APRS tab via the AprsEngine
        # singleton (see comms.aprs.engine) so the two tabs never spawn
        # duplicate Direwolf processes / AfskDemodulator instances.
        self._engine = get_aprs_engine(conn)
        self._afsk_source: str | None = None  # "direwolf" | "sdr" | None
        self._sdr_pipeline: object | None = None
        self._rig_connected = False
        self._sdr_connected = False

        # gr-satellites backend
        self._gr_backend = GrSatellitesBackend(self)
        self._gr_backend.telemetry_received.connect(self._on_gr_telemetry)
        self._gr_backend.status_changed.connect(self._on_gr_status)
        self._gr_sat_list: list[tuple[int, str]] = []  # (norad, name) sorted by name

        # Selected satellite from main satellite list (set_satellite from main_window)
        self._selected_norad: int | None = None
        self._selected_name: str = ""

        self._frame_count = 0

        self._ensure_db_table()
        self._setup_ui()
        self._load_baud_mode()
        self._connect_signals()
        self._populate_afsk_combo()
        if detect_gr_satellites():
            self._gr_sat_list = list_gr_satellites_with_names()
            self._populate_gr_combo()
        self._detect_already_connected()
        self._refresh_input_combo()
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # DB
    # ------------------------------------------------------------------ #

    def _ensure_db_table(self) -> None:
        if not hasattr(self._conn, "execute"):
            return
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at   DATETIME NOT NULL,
                norad_cat_id  INTEGER,
                callsign      TEXT NOT NULL,
                raw_hex       TEXT NOT NULL,
                parsed_json   TEXT,
                signal_db     REAL
            )
        """)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- Input source group ---
        input_box = QGroupBox(_("Input Source"))
        input_layout = QVBoxLayout(input_box)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel(_("Mode:")))
        self._combo_mode = QComboBox()
        self._combo_mode.addItem(_MODE_AFSK)
        self._combo_mode.addItem(_MODE_GR)
        self._combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        row1.addWidget(self._combo_mode)
        self._combo_afsk_sat = QComboBox()
        self._combo_afsk_sat.setMinimumWidth(280)
        self._combo_afsk_sat.currentIndexChanged.connect(self._on_afsk_sat_changed)
        row1.addWidget(self._combo_afsk_sat)
        self._combo_gr_sat = QComboBox()
        self._combo_gr_sat.setMinimumWidth(280)
        self._combo_gr_sat.setVisible(False)
        self._combo_gr_sat.currentIndexChanged.connect(self._on_gr_sat_changed)
        row1.addWidget(self._combo_gr_sat)

        row1.addSpacing(12)
        self._baud_combo = QComboBox()
        self._baud_combo.addItem(_("Auto"), "auto")
        self._baud_combo.addItem("1200", "1200")
        self._baud_combo.addItem("9600", "9600")
        self._baud_combo.setToolTip(
            _(
                "AX.25 baud rate for Bell 202 AFSK mode's Rig + Sound Card\n"
                "(Direwolf) reception. Auto reads the selected transponder's\n"
                "baud rate from SATNOGS (defaults to 1200 if unknown). Shared\n"
                "with the APRS tab — has no effect on gr-satellites mode or\n"
                "SDR-only reception, which is always 1200 baud AFSK."
            )
        )
        self._baud_combo.currentIndexChanged.connect(self._on_baud_mode_changed)
        row1.addWidget(QLabel(_("Baud:")))
        row1.addWidget(self._baud_combo)

        row1.addStretch()
        self._lbl_sat = QLabel(_("Satellite: —"))
        self._lbl_sat.setStyleSheet("color: #aaa;")
        row1.addWidget(self._lbl_sat)
        input_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self._btn_start = QPushButton(_("▶ Start"))
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop = QPushButton(_("■ Stop"))
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_stop.setEnabled(False)
        self._lbl_status = QLabel(_("—"))
        self._lbl_status.setStyleSheet("color: #aaa;")
        row2.addWidget(self._btn_start)
        row2.addWidget(self._btn_stop)
        row2.addWidget(self._lbl_status)
        row2.addStretch()
        self._lbl_count = QLabel(_("Frames: 0 received"))
        row2.addWidget(self._lbl_count)
        input_layout.addLayout(row2)

        root.addWidget(input_box)

        # --- Receive log ---
        log_box = QGroupBox(_("Received Frames"))
        log_layout = QVBoxLayout(log_box)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            [_("Time (UTC)"), _("Callsign"), _("Satellite"), _("Data")]
        )
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        log_layout.addWidget(self._table)
        root.addWidget(log_box, 1)

        # --- Footer ---
        footer = QHBoxLayout()
        self._btn_clear = QPushButton(_("Clear Log"))
        self._btn_clear.clicked.connect(self._on_clear)
        self._btn_export = QPushButton(_("Export CSV…"))
        self._btn_export.clicked.connect(self._on_export_csv)
        footer.addWidget(self._btn_clear)
        footer.addStretch()
        footer.addWidget(self._btn_export)
        root.addLayout(footer)

    # ------------------------------------------------------------------ #
    # Public API — called by main_window when satellite selection changes
    # ------------------------------------------------------------------ #

    def set_satellite(self, norad: int | None, name: str) -> None:
        """Update the currently tracked satellite."""
        self._selected_norad = norad
        self._selected_name = name
        if norad:
            self._lbl_sat.setText(f"{name} ({norad})")
            self._lbl_sat.setStyleSheet("color: #ddd;")
            # Auto-select in the active mode's satellite combo if supported
            for combo in (self._combo_afsk_sat, self._combo_gr_sat):
                for i in range(combo.count()):
                    if combo.itemData(i) == norad:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(i)
                        combo.blockSignals(False)
                        break
        else:
            self._lbl_sat.setText(_("Satellite: —"))
            self._lbl_sat.setStyleSheet("color: #aaa;")
        self._refresh_input_combo()

    # ------------------------------------------------------------------ #
    # Signals from RadioControlWidget
    # ------------------------------------------------------------------ #

    def _detect_already_connected(self) -> None:
        """Sync connection state for rigs/SDRs that were connected before this tab opened."""
        rc = self._radio_control
        for attr in ("_rig1", "_rig2"):
            rig = getattr(rc, attr, None)
            if rig is None or not getattr(rig, "is_connected", False):
                continue
            if getattr(rig, "is_sdr", False):
                self._sdr_connected = True
                self._sdr_pipeline = getattr(rig, "_pipeline", None)
            else:
                self._rig_connected = True

    def _connect_signals(self) -> None:
        try:
            self._radio_control.rig_connected.connect(self._on_rig_connected)  # type: ignore[attr-defined]
            self._radio_control.rig_disconnected.connect(self._on_rig_disconnected)  # type: ignore[attr-defined]
            self._radio_control.rig2_connected.connect(self._on_rig2_connected)  # type: ignore[attr-defined]
            self._radio_control.rig2_disconnected.connect(self._on_rig2_disconnected)  # type: ignore[attr-defined]
            self._radio_control.transmitter_changed.connect(self._on_transmitter_changed)  # type: ignore[attr-defined]
        except AttributeError:
            pass

    def _on_rig_connected(self) -> None:
        rc = self._radio_control
        rig1 = getattr(rc, "_rig1", None)
        if rig1 is not None and getattr(rig1, "is_sdr", False):
            self._sdr_connected = True
            self._sdr_pipeline = getattr(rig1, "_pipeline", None)
        else:
            self._rig_connected = True
        self._refresh_input_combo()
        self._refresh_status()

    def _on_rig_disconnected(self) -> None:
        rc = self._radio_control
        rig1 = getattr(rc, "_rig1", None)
        if rig1 is not None and getattr(rig1, "is_sdr", False):
            self._sdr_connected = False
            self._on_stop()
        else:
            self._rig_connected = False
            self._stop_engine()
        self._refresh_input_combo()
        self._refresh_status()

    def _on_rig2_connected(self) -> None:
        rc = self._radio_control
        rig2 = getattr(rc, "_rig2", None)
        if rig2 is not None and getattr(rig2, "is_sdr", False):
            self._sdr_connected = True
            self._sdr_pipeline = getattr(rig2, "_pipeline", None)
        else:
            self._rig_connected = True
        self._refresh_input_combo()
        self._refresh_status()

    def _on_rig2_disconnected(self) -> None:
        rc = self._radio_control
        rig2 = getattr(rc, "_rig2", None)
        if rig2 is not None and getattr(rig2, "is_sdr", False):
            self._sdr_connected = False
            self._on_stop()
        else:
            self._rig_connected = False
            self._stop_engine()
        self._refresh_input_combo()
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Input combo helpers
    # ------------------------------------------------------------------ #

    def _populate_afsk_combo(self) -> None:
        """Fill the AFSK satellite combo with satellites that have format definitions."""
        self._combo_afsk_sat.blockSignals(True)
        self._combo_afsk_sat.clear()
        for fmt in sorted(list_formats(), key=lambda f: str(f.get("name", "")).upper()):
            norad = fmt.get("norad")
            name = fmt.get("name") or str(norad)
            if norad:
                self._combo_afsk_sat.addItem(f"{name}  ({norad})", userData=int(norad))
        self._combo_afsk_sat.blockSignals(False)

    def _populate_gr_combo(self) -> None:
        """Fill the gr-satellites satellite combo from the loaded list."""
        self._combo_gr_sat.clear()
        for norad, name in self._gr_sat_list:
            self._combo_gr_sat.addItem(f"{name}  ({norad})", userData=norad)

    def _refresh_input_combo(self) -> None:
        """Enable/disable gr-satellites option based on availability."""
        gr_available = detect_gr_satellites() and bool(self._gr_sat_list)
        model = self._combo_mode.model()
        if isinstance(model, QStandardItemModel):
            item = model.item(1)
            if item is not None:
                item.setEnabled(gr_available)
        if not gr_available and self._combo_mode.currentIndex() == 1:
            self._combo_mode.setCurrentIndex(0)

    def _on_mode_changed(self, _index: int) -> None:
        is_gr = self._current_mode() == _MODE_GR
        self._combo_afsk_sat.setVisible(not is_gr)
        self._combo_gr_sat.setVisible(is_gr)
        self._refresh_status()

    def _on_afsk_sat_changed(self, _index: int) -> None:
        norad = self._combo_afsk_sat.currentData()
        if norad is not None:
            self.satellite_selected.emit(int(norad), "afsk")

    def _on_gr_sat_changed(self, _index: int) -> None:
        norad = self._combo_gr_sat.currentData()
        if norad is not None:
            self.satellite_selected.emit(int(norad), "gr")

    def _current_mode(self) -> str:
        return self._combo_mode.currentText()

    # ------------------------------------------------------------------ #
    # AX.25 baud mode (shared with the APRS tab)
    # ------------------------------------------------------------------ #

    def _load_baud_mode(self) -> None:
        """Restore the Auto/1200/9600 selection from app_settings."""
        mode = "auto"
        if hasattr(self._conn, "execute"):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (AX25_BAUD_SETTING_KEY,),
            ).fetchone()
            if row and row["value"] in ("auto", "1200", "9600"):
                mode = row["value"]
        idx = self._baud_combo.findData(mode)
        self._baud_combo.blockSignals(True)
        self._baud_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._baud_combo.blockSignals(False)

    def _on_baud_mode_changed(self, _index: int) -> None:
        """Persist the Auto/1200/9600 selection and apply it immediately."""
        mode = self._baud_combo.currentData()
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (AX25_BAUD_SETTING_KEY, mode),
            )
            self._conn.commit()
        self._engine.restart_if_modem_changed(resolve_ax25_modem(self._conn, self._radio_control))
        self._refresh_status()

    def _on_transmitter_changed(self, _xpdr: object) -> None:
        """Restart Direwolf if the newly selected transponder's baud differs."""
        self._engine.restart_if_modem_changed(resolve_ax25_modem(self._conn, self._radio_control))
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    def _on_start(self) -> None:
        if self._current_mode() == _MODE_GR:
            self._start_gr_satellites()
        else:
            self._try_start_afsk()
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

    def _on_stop(self) -> None:
        self._stop_gr_satellites()
        self._stop_engine()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # gr-satellites lifecycle
    # ------------------------------------------------------------------ #

    def _start_gr_satellites(self) -> None:
        norad = self._combo_gr_sat.currentData()
        if norad is None:
            self._set_error(_("⚠ No satellite selected"))
            return

        pipeline = self._sdr_pipeline
        if pipeline is None:
            pipeline = self._auto_connect_sdr()
            if pipeline is None:
                return

        try:
            samp_rate = int(pipeline._device.sample_rate)  # type: ignore[attr-defined]
        except AttributeError:
            samp_rate = 2_400_000

        ok, err = self._gr_backend.start(norad, samp_rate, pipeline)
        if not ok:
            self._set_error(f"⚠ {err}")
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)

    def _auto_connect_sdr(self) -> object | None:
        """Connect the first available SDR rig via Radio Control and return its pipeline."""
        rc = self._radio_control
        for attr in ("_rig1", "_rig2"):
            rig = getattr(rc, attr, None)
            if rig is None or not getattr(rig, "is_sdr", False):
                continue
            # Already connected — just grab the pipeline
            if getattr(rig, "is_connected", False):
                pipeline = getattr(rig, "_pipeline", None)
                if pipeline is not None:
                    self._sdr_connected = True
                    self._sdr_pipeline = pipeline
                    result: object = pipeline
                    return result
            # Delegate to Radio Control's connect button handler so the UI
            # stays consistent (button state, status label, signals, etc.)
            self._lbl_status.setText(_("Connecting SDR…"))
            connect_fn = getattr(
                rc, "_on_connect_rig1" if attr == "_rig1" else "_on_connect_rig2", None
            )
            if connect_fn is not None:
                connect_fn()
            self._set_error(
                _("SDR connecting via Radio Control — press Start again once connected")
            )
            return None
        self._set_error(_("⚠ No SDR configured in Rig Settings"))
        return None

    def _stop_gr_satellites(self) -> None:
        if self._gr_backend.is_running:
            self._gr_backend.stop()

    def _on_gr_status(self, msg: str) -> None:
        self._lbl_status.setText(msg)
        color = "#27ae60" if self._gr_backend.is_running else "#aaa"
        self._lbl_status.setStyleSheet(f"color: {color};")

    def _on_gr_telemetry(self, text: str) -> None:
        """Parse a gr-satellites stdout block and add it to the table."""
        callsign = ""
        data_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if line.startswith("-> Packet from"):
                callsign = line.replace("-> Packet from", "").strip()
            elif stripped and stripped != "Container:":
                data_lines.append(stripped)

        sat_name = self._selected_name
        if not sat_name and self._selected_norad:
            info = get_satellite_info(self._selected_norad)
            sat_name = str(info.get("name", "")) if info else ""
        data_text = "  |  ".join(data_lines) if data_lines else text[:120]

        self._append_row(
            callsign=callsign or sat_name or "—",
            sat_name=sat_name or "—",
            data=data_text,
            norad=self._selected_norad,
        )

    # ------------------------------------------------------------------ #
    # AFSK lifecycle (Bell 202)
    # ------------------------------------------------------------------ #

    def _try_start_afsk(self) -> None:
        if self._sdr_connected and self._sdr_pipeline is not None:
            self._try_start_sdr(self._sdr_pipeline)
        elif self._rig_connected:
            self._try_start_direwolf()
        else:
            # Try auto-connecting an SDR before giving up
            pipeline = self._auto_connect_sdr()
            if pipeline is not None:
                self._try_start_sdr(pipeline)
            else:
                self._btn_start.setEnabled(True)
                self._btn_stop.setEnabled(False)

    def _try_start_direwolf(self) -> None:
        modem = resolve_ax25_modem(self._conn, self._radio_control)
        ok, err = self._engine.start_rig(_ENGINE_OWNER, "N0CALL", 0, "", modem=modem)
        if not ok:
            self._set_error(f"⚠ {err}")
            return
        self._engine.raw_frame_received.connect(self._on_ax25_frame)
        self._engine.error_occurred.connect(self._set_error)
        self._afsk_source = "direwolf"
        self._refresh_status()

    def _try_start_sdr(self, pipeline: object) -> None:
        ok, err = self._engine.start_sdr(_ENGINE_OWNER, pipeline)
        if not ok:
            self._set_error(f"⚠ {err}")
            return
        self._sdr_pipeline = pipeline
        self._engine.raw_frame_received.connect(self._on_ax25_frame)
        self._afsk_source = "sdr"
        self._refresh_status()

    def _stop_engine(self) -> None:
        """Release this tab's interest in the shared AprsEngine.

        Only actually stops Direwolf / the AfskDemodulator once no other
        tab (e.g. APRS) still needs it — see AprsEngine.stop().
        """
        if self._afsk_source is None:
            return
        with contextlib.suppress(RuntimeError, TypeError):
            self._engine.raw_frame_received.disconnect(self._on_ax25_frame)
        with contextlib.suppress(RuntimeError, TypeError):
            self._engine.error_occurred.disconnect(self._set_error)
        self._engine.stop(_ENGINE_OWNER)
        self._afsk_source = None

    # ------------------------------------------------------------------ #
    # AX.25 frame handler (Bell 202 path)
    # ------------------------------------------------------------------ #

    def _on_ax25_frame(self, raw: bytes) -> None:
        frame = decode_ax25(raw)
        if frame is None:
            return
        norad = self._callsign_to_norad(frame.src)
        tf = decode_telemetry(frame.src, frame.payload, norad)
        self._append_row(
            callsign=tf.callsign,
            sat_name=tf.satellite_name,
            data=tf.summary(),
            norad=tf.norad,
            gray=not tf.has_fields,
        )
        self._persist_frame(tf, datetime.datetime.now(datetime.UTC))

    def _callsign_to_norad(self, callsign: str) -> int | None:
        call_upper = callsign.upper().split("-")[0]
        for fmt in list_formats():
            if fmt.get("callsign", "").upper() == call_upper:
                return int(fmt["norad"])
        if not hasattr(self._conn, "execute"):
            return None
        row = self._conn.execute(
            "SELECT norad_cat_id FROM satellites WHERE name LIKE ?",
            (f"%{call_upper}%",),
        ).fetchone()
        return int(row["norad_cat_id"]) if row else None

    # ------------------------------------------------------------------ #
    # Table helpers
    # ------------------------------------------------------------------ #

    def _append_row(
        self,
        *,
        callsign: str,
        sat_name: str,
        data: str,
        norad: int | None,
        gray: bool = False,
    ) -> None:
        now = datetime.datetime.now(datetime.UTC)
        ts = now.strftime("%H:%M:%S")
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(ts))
        self._table.setItem(row, 1, QTableWidgetItem(callsign))
        self._table.setItem(row, 2, QTableWidgetItem(sat_name))
        data_item = QTableWidgetItem(data)
        if gray:
            data_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row, 3, data_item)
        self._table.scrollToBottom()
        self._frame_count += 1
        self._lbl_count.setText(_("Frames: ") + str(self._frame_count) + _(" received"))

    def _persist_frame(self, tf: TelemetryFrame, ts: datetime.datetime) -> None:
        if not hasattr(self._conn, "execute"):
            return
        parsed = (
            json.dumps({f.name: {"value": f.scaled_value, "unit": f.unit} for f in tf.fields})
            if tf.fields
            else None
        )
        self._conn.execute(
            """INSERT INTO telemetry_log
               (received_at, norad_cat_id, callsign, raw_hex, parsed_json)
               VALUES (?, ?, ?, ?, ?)""",
            (ts.isoformat(), tf.norad, tf.callsign, tf.raw_hex, parsed),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #

    def _set_error(self, msg: str) -> None:
        self._lbl_status.setText(msg)
        self._lbl_status.setStyleSheet("color: #e74c3c;")

    def _refresh_status(self) -> None:
        if self._gr_backend.is_running:
            return  # managed by _on_gr_status
        if self._afsk_source == "direwolf" and self._engine.is_running:
            modem = self._engine.current_modem
            suffix = f"  [{modem} baud]" if modem else ""
            self._lbl_status.setText(_("Rig + Direwolf (receiving)") + suffix)
            self._lbl_status.setStyleSheet("color: #27ae60;")
        elif self._afsk_source == "sdr" and self._engine.is_running:
            self._lbl_status.setText(_("SDR — Bell 202 AFSK (receive only)"))
            self._lbl_status.setStyleSheet("color: #4a9eff;")
        else:
            self._lbl_status.setText(_("—  (connect Rig or SDR, then click ▶ Start)"))
            self._lbl_status.setStyleSheet("color: #aaa;")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _load_soundcard_devices(self) -> tuple[int | None, int | None]:
        if not hasattr(self._conn, "execute"):
            return None, None
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if not row or not row["value"]:
            return None, None
        try:
            data = json.loads(row["value"])
            in_idx = data.get("input_device_index")
            out_idx = data.get("output_device_index")
            return (
                int(in_idx) if in_idx is not None else None,
                int(out_idx) if out_idx is not None else None,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None, None

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _on_clear(self) -> None:
        self._table.setRowCount(0)
        self._frame_count = 0
        self._lbl_count.setText(_("Frames: 0 received"))

    def _on_export_csv(self) -> None:
        default_name = (
            "telemetry_" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d") + ".csv"
        )
        path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Export Telemetry CSV"),
            str(Path.home() / default_name),
            "CSV (*.csv)",
        )
        if not path:
            return
        rows_count = self._table.rowCount()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Time (UTC)", "Callsign", "Satellite", "Data"])
            for r in range(rows_count):
                writer.writerow(
                    [(item.text() if (item := self._table.item(r, c)) else "") for c in range(4)]
                )

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._on_stop()
        super().closeEvent(event)
