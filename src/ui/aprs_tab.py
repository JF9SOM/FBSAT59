"""APRS tab widget — Communications > APRS.

Displays a settings bar (callsign, SSID, via path), a received-packet log,
a message-send form, and an ADIF export button.

Input source is determined automatically by the Rig Control state:
  - SDR connected  → SDR (receive-only, Python Bell 202 demodulation)
  - Rig connected  → Sound Card + Direwolf (send + receive)
  - Neither        → tab shows a "no audio source" notice

The actual demodulation / Direwolf backend is wired in subsequent commits.
This commit provides the complete UI and settings persistence.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from i18n import _

# SSID range 0-15 per AX.25 spec
_SSID_MIN = 0
_SSID_MAX = 15

# Default via path for ISS digipeater
_DEFAULT_VIA = "ARISS"

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine).
# The Telemetry tab's Bell 202 AFSK mode shares the same engine under its
# own "telemetry" tag so closing one tab doesn't stop the other's reception.
_ENGINE_OWNER = "aprs"


class AprsTab(QWidget):
    """Non-resident tab opened from Communications > APRS.

    Persists callsign / SSID / via settings in ``app_settings`` under the
    key ``aprs_settings``.  Received packets are stored in ``aprs_log`` (DB
    table created on first open if absent).

    Signals
    -------
    aprs_stations_updated(dict)
        Emitted whenever the set of positioned APRS stations changes.
        Payload: {callsign: (lat_deg, lon_deg)}.
    aprs_stations_cleared()
        Emitted when the tab closes so callers can clear map pins.
    """

    aprs_stations_updated: Signal = Signal(dict)
    aprs_stations_cleared: Signal = Signal()

    def __init__(
        self,
        conn: Any,
        radio_control: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control

        # Connection state tracked via RadioControlWidget signals
        self._rig_connected = False
        self._sdr_connected = False
        self._rig_label = ""
        self._sdr_label = ""

        # Positioned APRS stations received this session: {callsign: (lat, lon)}
        self._aprs_stations: dict[str, tuple[float, float]] = {}

        # Callsigns we have sent a message to this session (base call, no SSID).
        # A QSO is logged only when the remote station replies with a real message.
        self._pending_qso: set[str] = set()

        # Auto-beacon timer for position transmission
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._on_send_position)

        # APRS engine (Direwolf backend) — process-wide singleton, shared
        # with the Telemetry tab's Bell 202 AFSK mode
        from comms.aprs.engine import get_aprs_engine

        self._engine = get_aprs_engine(conn)
        self._engine.packet_received.connect(self._on_packet_received)
        self._engine.status_changed.connect(self._on_engine_status)
        self._engine.error_occurred.connect(self._on_engine_error)

        self._ensure_db_table()
        self._setup_ui()
        self._load_settings()
        self._connect_signals()
        self._refresh_input_source()

        # Start receiving immediately when Sound Card is configured — this must
        # not require pressing Connect on Rig 1 first, so ground-based APRS
        # reception can be tested with no rig at all. TX stays gated behind
        # can_tx in _refresh_input_source() (requires an actual rig for PTT).
        self._try_start_engine()

    # ------------------------------------------------------------------ #
    # DB helpers
    # ------------------------------------------------------------------ #

    def _ensure_db_table(self) -> None:
        """Create aprs_log table if it does not yet exist."""
        if not hasattr(self._conn, "execute"):
            return
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aprs_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at   DATETIME NOT NULL,
                callsign      TEXT NOT NULL,
                via           TEXT,
                latitude_deg  REAL,
                longitude_deg REAL,
                comment       TEXT,
                raw_frame     TEXT,
                norad_sat     INTEGER
            )
            """
        )
        self._conn.commit()

    def _load_log_from_db(self) -> None:
        """Populate the receive log with the most recent 200 entries."""
        if not hasattr(self._conn, "execute"):
            return
        self._log_list.clear()
        rows = self._conn.execute(
            "SELECT received_at, callsign, via, comment, raw_frame "
            "FROM aprs_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        for row in reversed(rows):
            self._append_log_item(
                ts=row["received_at"],
                callsign=row["callsign"],
                via=row["via"] or "",
                comment=row["comment"] or row["raw_frame"] or "",
            )

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # -- Settings bar --
        settings_group = QGroupBox(_("Station Settings"))
        settings_form = QFormLayout(settings_group)
        settings_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # Row 1: Callsign + SSID + Via
        row1 = QHBoxLayout()
        self._callsign_edit = QLineEdit()
        self._callsign_edit.setPlaceholderText("")
        self._callsign_edit.setMaxLength(6)
        self._callsign_edit.setFixedWidth(90)
        row1.addWidget(QLabel(_("My Call:")))
        row1.addWidget(self._callsign_edit)

        row1.addSpacing(12)
        self._ssid_spin = QSpinBox()
        self._ssid_spin.setRange(_SSID_MIN, _SSID_MAX)
        self._ssid_spin.setValue(0)
        self._ssid_spin.setFixedWidth(55)
        row1.addWidget(QLabel("SSID:"))
        row1.addWidget(self._ssid_spin)

        row1.addSpacing(12)
        self._via_edit = QLineEdit()
        self._via_edit.setPlaceholderText(_DEFAULT_VIA)
        self._via_edit.setText(_DEFAULT_VIA)
        self._via_edit.setFixedWidth(120)
        row1.addWidget(QLabel(_("Via:")))
        row1.addWidget(self._via_edit)
        row1.addStretch()
        settings_form.addRow(row1)

        # Row 2: input source (read-only display) + satellite guide
        self._input_label = QLabel(_("Input: —"))
        self._input_label.setStyleSheet("color: #aaa;")
        _aprs_help = QLabel(" ? ")
        _aprs_help.setStyleSheet(
            "color:white;background:#2980b9;border-radius:8px;font-weight:bold;padding:2px 6px;"
        )
        _aprs_help.setToolTip(
            "APRS is available via these satellites:\n"
            "  • ISS (NORAD 25544)  145.825 MHz FM  via ARISS\n"
            "  • (Other amateur satellites with APRS digipeaters may also be active)\n\n"
            "Use callsign path ARISS or RS0ISS.\n"
            "Select ISS in Radio Control to get started."
        )
        _input_row_w = QWidget()
        _input_row_l = QHBoxLayout(_input_row_w)
        _input_row_l.setContentsMargins(0, 0, 0, 0)
        _input_row_l.setSpacing(6)
        _input_row_l.addWidget(self._input_label)
        _input_row_l.addWidget(_aprs_help)
        _input_row_l.addStretch()
        settings_form.addRow(_input_row_w)

        root.addWidget(settings_group)

        # -- Receive log --
        log_group = QGroupBox(_("Received Packets"))
        log_layout = QVBoxLayout(log_group)
        self._log_list = QListWidget()
        self._log_list.setAlternatingRowColors(True)
        self._log_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_layout.addWidget(self._log_list)
        root.addWidget(log_group, stretch=1)

        # -- Send form --
        send_group = QGroupBox(_("Send Message"))
        send_layout = QHBoxLayout(send_group)
        send_layout.addWidget(QLabel(_("To:")))
        self._to_edit = QLineEdit()
        self._to_edit.setPlaceholderText("JA1XYZ")
        self._to_edit.setFixedWidth(100)
        send_layout.addWidget(self._to_edit)
        send_layout.addSpacing(8)
        send_layout.addWidget(QLabel(_("Message:")))
        self._msg_edit = QLineEdit()
        self._msg_edit.setPlaceholderText(_("Text message (max 67 chars)"))
        self._msg_edit.setMaxLength(67)
        send_layout.addWidget(self._msg_edit, stretch=1)
        self._send_btn = QPushButton(_("Send"))
        self._send_btn.setEnabled(False)
        self._send_btn.clicked.connect(self._on_send)
        send_layout.addWidget(self._send_btn)
        root.addWidget(send_group)

        # -- Send Position --
        pos_group = QGroupBox(_("Send My Position"))
        pos_layout = QHBoxLayout(pos_group)
        self._pos_enable_chk = QCheckBox(_("Auto-beacon every"))
        self._pos_enable_chk.setChecked(False)
        self._pos_enable_chk.toggled.connect(self._on_pos_beacon_toggled)
        pos_layout.addWidget(self._pos_enable_chk)
        self._pos_interval_spin = QSpinBox()
        self._pos_interval_spin.setRange(1, 60)
        self._pos_interval_spin.setValue(5)
        self._pos_interval_spin.setSuffix(_(" min"))
        self._pos_interval_spin.setFixedWidth(80)
        pos_layout.addWidget(self._pos_interval_spin)
        pos_layout.addSpacing(12)
        pos_layout.addWidget(QLabel(_("Symbol:")))
        self._pos_symbol_combo = QComboBox()
        # (display label, APRS symbol code)
        self._pos_symbols = [
            (_("Fixed Station  /-"), "/-"),
            (_("Mobile  />"), "/>"),
            (_("Balloon  /O"), "/O"),
            (_("Antenna  /Y"), "/Y"),
            (_("Satellite  /S"), "/S"),
        ]
        for label, _code in self._pos_symbols:
            self._pos_symbol_combo.addItem(label)
        self._pos_symbol_combo.setFixedWidth(80)
        pos_layout.addWidget(self._pos_symbol_combo)
        pos_layout.addSpacing(12)
        pos_layout.addWidget(QLabel(_("Comment:")))
        self._pos_comment_edit = QLineEdit()
        self._pos_comment_edit.setPlaceholderText(_("Optional free text"))
        self._pos_comment_edit.setMaxLength(43)
        self._pos_comment_edit.setMinimumWidth(200)
        pos_layout.addWidget(self._pos_comment_edit, stretch=1)
        self._pos_send_btn = QPushButton(_("Send Now"))
        self._pos_send_btn.setEnabled(False)
        self._pos_send_btn.clicked.connect(self._on_send_position)
        pos_layout.addWidget(self._pos_send_btn)
        self._pos_loc_label = QLabel(_("QTH: —"))
        self._pos_loc_label.setStyleSheet("color: #aaa; font-size: 10px;")
        pos_layout.addWidget(self._pos_loc_label)
        root.addWidget(pos_group)

        # -- Footer: export + QSO count --
        footer = QHBoxLayout()
        self._export_btn = QPushButton(_("Export ADIF…"))
        self._export_btn.clicked.connect(self._on_export_adif)
        footer.addWidget(self._export_btn)
        self._qso_count_label = QLabel("")
        self._qso_count_label.setStyleSheet("color: #aaa;")
        footer.addWidget(self._qso_count_label)
        footer.addStretch()
        root.addLayout(footer)

        self._refresh_qso_count()

    # ------------------------------------------------------------------ #
    # Signal wiring
    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        """Connect to RadioControlWidget connection state signals."""
        rc = self._radio_control
        if hasattr(rc, "rig_connected"):
            rc.rig_connected.connect(self._on_rig_connected)
        if hasattr(rc, "rig_disconnected"):
            rc.rig_disconnected.connect(self._on_rig_disconnected)
        # SDR connect/disconnect — emitted by RadioControlWidget when SDR rig connects
        if hasattr(rc, "rig2_connected"):
            rc.rig2_connected.connect(self._on_rig2_connected)
        if hasattr(rc, "rig2_disconnected"):
            rc.rig2_disconnected.connect(self._on_rig2_disconnected)

    # ------------------------------------------------------------------ #
    # Connection state slots
    # ------------------------------------------------------------------ #

    def _on_rig_connected(self) -> None:
        """Rig 1 connected — may be a Hamlib rig or an SDR adapter."""
        rc = self._radio_control
        rig1 = getattr(rc, "_rig1", None)
        if rig1 is not None and getattr(rig1, "is_sdr", False):
            self._sdr_connected = True
            dev = getattr(rig1, "device_label", "SDR")
            self._sdr_label = str(dev)
            self._try_start_sdr(rig1)
        else:
            self._rig_connected = True
            self._engine.set_rig(rig1)
            self._try_start_engine()
        self._refresh_input_source()

    def _on_rig_disconnected(self) -> None:
        rc = self._radio_control
        rig1 = getattr(rc, "_rig1", None)
        if rig1 is not None and getattr(rig1, "is_sdr", False):
            self._sdr_connected = False
            self._sdr_label = ""
            self._engine.stop(_ENGINE_OWNER)
        else:
            self._rig_connected = False
            self._engine.set_rig(None)
            self._engine.stop(_ENGINE_OWNER)
        self._refresh_input_source()

    def _on_rig2_connected(self) -> None:
        """Rig 2 connected — may be a Hamlib rig or an SDR adapter."""
        rc = self._radio_control
        rig2 = getattr(rc, "_rig2", None)
        if rig2 is not None and getattr(rig2, "is_sdr", False):
            self._sdr_connected = True
            dev = getattr(rig2, "device_label", "SDR")
            self._sdr_label = str(dev)
            self._try_start_sdr(rig2)
        else:
            self._rig_connected = True
            self._engine.set_rig(rig2)
            self._try_start_engine()
        self._refresh_input_source()

    def _on_rig2_disconnected(self) -> None:
        rc = self._radio_control
        rig2 = getattr(rc, "_rig2", None)
        if rig2 is not None and getattr(rig2, "is_sdr", False):
            self._sdr_connected = False
            self._sdr_label = ""
            self._engine.stop(_ENGINE_OWNER)
        else:
            self._rig_connected = False
            self._engine.set_rig(None)
            self._engine.stop(_ENGINE_OWNER)
        self._refresh_input_source()

    # ------------------------------------------------------------------ #
    # Input source display
    # ------------------------------------------------------------------ #

    def _refresh_input_source(self) -> None:
        """Update the input-source label and send-button state."""
        sc_ok = self._is_soundcard_configured()

        can_tx = self._rig_connected and sc_ok
        if can_tx:
            self._input_label.setText(_("Input: Sound Card + Direwolf  (send + receive)"))
            self._input_label.setStyleSheet("color: #7bed9f;")
            self._send_btn.setEnabled(True)
        elif self._sdr_connected:
            label = self._sdr_label or "SDR"
            self._input_label.setText(_("Input: {dev}  (receive only — SDR)").format(dev=label))
            self._input_label.setStyleSheet("color: #4a9eff;")
            self._send_btn.setEnabled(False)
        elif sc_ok and self._engine.is_running:
            self._input_label.setText(
                _("Input: Sound Card + Direwolf  (receive only — Rig not connected)")
            )
            self._input_label.setStyleSheet("color: #4a9eff;")
            self._send_btn.setEnabled(False)
        elif self._rig_connected and not sc_ok:
            self._input_label.setText(
                _("Input: Sound Card not configured — open Rig Settings > Sound Card")
            )
            self._input_label.setStyleSheet("color: orange;")
            self._send_btn.setEnabled(False)
        else:
            self._input_label.setText(
                _("Input: No audio source — connect Rig or SDR in Radio Control")
            )
            self._input_label.setStyleSheet("color: #f44336;")
            self._send_btn.setEnabled(False)

        # Position send requires TX capability; update button and QTH label
        self._pos_send_btn.setEnabled(can_tx)
        self._refresh_pos_label()

    @property
    def engine(self) -> Any:
        """Return the APRSEngine instance (for SSDV cross-tab wiring)."""
        return self._engine

    def _try_start_engine(self) -> None:
        """Start Direwolf engine whenever Sound Card is configured.

        Direwolf runs in ADEVICE stdin stdout / PTT NONE mode, so it needs no
        rig at all to receive. Rig 1 is only required later for PTT (send);
        see can_tx in _refresh_input_source().
        """
        if not self._is_soundcard_configured():
            return
        if self._engine.is_running:
            return
        cs = self._callsign_edit.text().strip().upper()
        ssid = self._ssid_spin.value()
        via = self._via_edit.text().strip()
        if cs:
            self._engine.start_rig(_ENGINE_OWNER, cs, ssid, via)

    def _try_start_sdr(self, rig2: object) -> None:
        """Start Bell 202 AFSK demodulator on the SDR pipeline (receive only)."""
        if self._engine.is_running:
            return
        pipeline = getattr(rig2, "_pipeline", None)
        if pipeline is None:
            return
        self._engine.start_sdr(_ENGINE_OWNER, pipeline)

    # ------------------------------------------------------------------ #
    # Engine signal slots
    # ------------------------------------------------------------------ #

    def _on_packet_received(self, packet: object) -> None:
        """Handle a decoded APRS packet from the engine."""
        from comms.aprs.parser import AprsPacket

        if not isinstance(packet, AprsPacket):
            return

        # Determine whether to persist this packet to the DB.
        # Only log when the remote station sends us a real message text reply
        # after we have messaged them first (bidirectional QSO confirmation).
        log_to_db = self._is_confirmed_reply(packet)

        self.append_packet(
            callsign=packet.callsign,
            via=packet.via,
            comment=packet.comment,
            raw_frame=packet.raw_info,
            lat=packet.latitude,
            lon=packet.longitude,
            log=log_to_db,
        )
        # Update map pin when the packet carries a position
        if packet.latitude is not None and packet.longitude is not None:
            self._aprs_stations[packet.callsign] = (packet.latitude, packet.longitude)
            self.aprs_stations_updated.emit(dict(self._aprs_stations))

    def _is_confirmed_reply(self, packet: object) -> bool:
        """Return True when *packet* completes a bidirectional QSO.

        Conditions (all must hold):
          1. The packet is an APRS message (data_type == ':').
          2. The message text is non-empty and is not a bare ack.
          3. The addressee matches our own callsign (ignoring SSID).
          4. We previously sent a message to the sender's base callsign.
        """
        from comms.aprs.parser import AprsPacket

        if not isinstance(packet, AprsPacket):
            return False
        if packet.data_type != ":":
            return False
        msg_text = (packet.message_text or "").strip()
        if not msg_text or msg_text.lower().startswith("ack"):
            return False

        my_call = self._callsign_edit.text().strip().upper().split("-")[0]
        addressee = (packet.message_addressee or "").strip().upper().split("-")[0]
        if addressee != my_call:
            return False

        sender_base = packet.callsign.split("-")[0].upper()
        if sender_base not in self._pending_qso:
            return False

        # Confirmed — remove from pending so duplicate replies don't re-log
        self._pending_qso.discard(sender_base)
        return True

    def _on_engine_status(self, status: str) -> None:
        self._input_label.setText(status)

    def _on_engine_error(self, msg: str) -> None:
        self._input_label.setText(f"⚠ {msg}")
        self._input_label.setStyleSheet("color: orange;")

    def _is_soundcard_configured(self) -> bool:
        """Return True when Sound Card settings have been saved."""
        if not hasattr(self._conn, "execute"):
            return False
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if not row or not row["value"]:
            return False
        try:
            data = json.loads(row["value"])
            return bool(data.get("configured", False))
        except (json.JSONDecodeError, TypeError):
            return False

    # ------------------------------------------------------------------ #
    # Settings persistence
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> None:
        """Restore callsign / SSID / via from app_settings."""
        if not hasattr(self._conn, "execute"):
            return
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'aprs_settings'"
        ).fetchone()
        if not row or not row["value"]:
            # No APRS settings yet — pre-fill callsign from Set QTH
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'callsign'"
            ).fetchone()
            cs = str(r["value"]) if r else ""
            if cs:
                self._callsign_edit.setText(cs.upper())
            self._load_log_from_db()
            return
        try:
            data = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return
        cs = data.get("callsign", "")
        if not cs:
            # Fall back to global callsign from Set QTH
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'callsign'"
            ).fetchone()
            cs = str(r["value"]) if r else ""
        if cs:
            self._callsign_edit.setText(str(cs).upper())
        self._ssid_spin.setValue(int(data.get("ssid", 0)))
        if via := data.get("via"):
            self._via_edit.setText(str(via))
        self._load_log_from_db()

    def _save_settings(self) -> None:
        """Persist callsign / SSID / via to app_settings."""
        if not hasattr(self._conn, "execute"):
            return
        data = {
            "callsign": self._callsign_edit.text().strip().upper(),
            "ssid": self._ssid_spin.value(),
            "via": self._via_edit.text().strip(),
        }
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('aprs_settings', ?, CURRENT_TIMESTAMP)",
            (json.dumps(data),),
        )
        self._conn.commit()

    def closeEvent(self, event: Any) -> None:
        """Stop engine, clear map pins, stop beacon timer, and save settings."""
        self._pos_timer.stop()
        # AprsEngine is a process-wide singleton (outlives this tab), so
        # explicitly disconnect rather than relying on Qt's auto-disconnect
        # on receiver destruction — otherwise a still-running engine could
        # deliver a stray signal to this tab in the gap between close() and
        # actual deletion.
        with contextlib.suppress(RuntimeError, TypeError):
            self._engine.packet_received.disconnect(self._on_packet_received)
        with contextlib.suppress(RuntimeError, TypeError):
            self._engine.status_changed.disconnect(self._on_engine_status)
        with contextlib.suppress(RuntimeError, TypeError):
            self._engine.error_occurred.disconnect(self._on_engine_error)
        self._engine.stop(_ENGINE_OWNER)
        self._aprs_stations.clear()
        self.aprs_stations_cleared.emit()
        self._save_settings()
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # Receive log helpers (called by APRS engine in future commits)
    # ------------------------------------------------------------------ #

    def append_packet(
        self,
        callsign: str,
        via: str,
        comment: str,
        raw_frame: str,
        lat: float | None = None,
        lon: float | None = None,
        norad: int | None = None,
        log: bool = False,
    ) -> None:
        """Add a decoded APRS packet to the receive log widget.

        Persists to the DB only when *log* is True (bidirectional QSO confirmed).
        """
        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._append_log_item(ts, callsign, via, comment)

        if log and hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT INTO aprs_log "
                "(received_at, callsign, via, latitude_deg, longitude_deg, "
                " comment, raw_frame, norad_sat) "
                "VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?)",
                (callsign, via, lat, lon, comment, raw_frame, norad),
            )
            self._conn.commit()
            self._broadcast_adif(callsign, via, comment, lat, lon, norad)
        self._refresh_qso_count()

    def _broadcast_adif(
        self,
        callsign: str,
        via: str,
        comment: str,
        lat: float | None,
        lon: float | None,
        norad: int | None,
    ) -> None:
        """Send this confirmed APRS QSO to the UDP log broadcaster.

        APRS has no signal-report exchange, so a confirmed message exchange
        is logged as a nominal 599/599 report (see CLAUDE.md "ログソフト連携"
        design note).
        """
        from comms.log_broadcast import get_log_broadcaster
        from ui.adif_utils import build_adif_record

        now = datetime.now(tz=UTC)
        cs = str(callsign or "").split(">")[0].split("-")[0]
        my_call = self._callsign_edit.text().strip().upper()
        my_ssid = self._ssid_spin.value()
        my_station = f"{my_call}-{my_ssid}" if my_ssid else my_call

        sat_name = ""
        if norad and hasattr(self._conn, "execute"):
            with contextlib.suppress(Exception):
                row = self._conn.execute(
                    "SELECT name FROM satellites WHERE norad_cat_id = ?", (norad,)
                ).fetchone()
                if row:
                    sat_name = str(row["name"])

        grid = _latlon_to_grid(float(lat), float(lon or 0)) if lat is not None else ""

        fields: dict[str, str] = {
            "CALL": cs,
            "QSO_DATE": now.strftime("%Y%m%d"),
            "TIME_ON": now.strftime("%H%M%S"),
            "MODE": "PKT",
            "MY_CALL": my_station,
            "COMMENT": str(comment or ""),
            "VIA": str(via or ""),
            "SAT_NAME": sat_name,
        }
        if sat_name:
            fields["PROP_MODE"] = "SAT"
        fields["RST_SENT"] = "599"
        fields["RST_RCVD"] = "599"
        if grid:
            fields["GRIDSQUARE"] = grid

        broadcaster = get_log_broadcaster()
        broadcaster.reload_settings(self._conn)
        broadcaster.send_adif_record(build_adif_record(fields))

    def _append_log_item(self, ts: str, callsign: str, via: str, comment: str) -> None:
        via_str = f",{via}" if via else ""
        text = f"{ts}  {callsign}{via_str}: {comment}"
        item = QListWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._log_list.addItem(item)
        self._log_list.scrollToBottom()

    def _refresh_qso_count(self) -> None:
        if not hasattr(self._conn, "execute"):
            return
        row = self._conn.execute("SELECT COUNT(*) AS n FROM aprs_log").fetchone()
        n = row["n"] if row else 0
        self._qso_count_label.setText(_("QSOs logged: {n}").format(n=n))

    # ------------------------------------------------------------------ #
    # Send slots
    # ------------------------------------------------------------------ #

    def _on_send(self) -> None:
        """Transmit an APRS message via Direwolf KISS TX."""
        to_call = self._to_edit.text().strip().upper()
        msg = self._msg_edit.text().strip()
        if not to_call or not msg:
            return
        self._save_settings()
        my_call = self._callsign_edit.text().strip().upper()
        ssid = self._ssid_spin.value()
        via = self._via_edit.text().strip()

        if self._engine.is_running:
            self._engine.send_message(my_call, ssid, via, to_call, msg)

        # Track outgoing message so a reply triggers QSO logging
        self._pending_qso.add(to_call.split("-")[0].upper())

        # Echo to receive log as sent marker
        src = f"{my_call}-{ssid}" if ssid else my_call
        self.append_packet(
            callsign=f"{src}>APRS",
            via=via,
            comment=f"[TX→{to_call}] {msg}",
            raw_frame="",
        )
        self._msg_edit.clear()

    def _get_my_location(self) -> tuple[float, float] | None:
        """Return (lat_deg, lon_deg) from saved QTH, or None if not set."""
        try:
            from core.location import LocationManager

            mgr = LocationManager(self._conn)
            loc = mgr.load_saved()
            if loc is not None:
                return (loc.latitude_deg, loc.longitude_deg)
        except Exception:
            pass
        return None

    def _refresh_pos_label(self) -> None:
        """Update the QTH coordinates label in the Send Position group."""
        pos = self._get_my_location()
        if pos is not None:
            lat, lon = pos
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            self._pos_loc_label.setText(f"QTH: {abs(lat):.4f}°{ns} {abs(lon):.4f}°{ew}")
            self._pos_loc_label.setStyleSheet("color: #aaa; font-size: 10px;")
        else:
            self._pos_loc_label.setText(_("QTH: not set — configure in Settings"))
            self._pos_loc_label.setStyleSheet("color: orange; font-size: 10px;")

    def _on_pos_beacon_toggled(self, checked: bool) -> None:
        """Start or stop the auto-beacon timer."""
        if checked:
            interval_ms = self._pos_interval_spin.value() * 60 * 1000
            self._pos_timer.start(interval_ms)
            # Send immediately on enable
            self._on_send_position()
        else:
            self._pos_timer.stop()

    def _on_send_position(self) -> None:
        """Transmit one APRS position packet with the saved QTH coordinates."""
        pos = self._get_my_location()
        if pos is None:
            self._pos_loc_label.setText(_("QTH: not set — configure in Settings"))
            self._pos_loc_label.setStyleSheet("color: orange; font-size: 10px;")
            return

        if not self._engine.is_running:
            return

        my_call = self._callsign_edit.text().strip().upper()
        ssid = self._ssid_spin.value()
        via = self._via_edit.text().strip()
        symbol = self._pos_symbols[self._pos_symbol_combo.currentIndex()][1]
        comment = self._pos_comment_edit.text().strip()

        lat, lon = pos
        self._engine.send_position(my_call, ssid, via, lat, lon, symbol, comment)

        # Echo to receive log as sent marker
        src = f"{my_call}-{ssid}" if ssid else my_call
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        self.append_packet(
            callsign=f"{src}>APRS",
            via=via,
            comment=f"[TX POS] {abs(lat):.4f}°{ns} {abs(lon):.4f}°{ew} {comment}".strip(),
            raw_frame="",
        )

    # ------------------------------------------------------------------ #
    # ADIF export
    # ------------------------------------------------------------------ #

    def _on_export_adif(self) -> None:
        """Open the unified date-range ADIF export dialog."""
        from ui.log_export_dialog import LogExportDialog

        my_call = self._callsign_edit.text().strip().upper()
        ssid = self._ssid_spin.value()
        dlg = LogExportDialog(self._conn, my_call=my_call, my_ssid=ssid, parent=self)
        dlg.exec()


# ---------------------------------------------------------------------------
# Maidenhead grid helper (lat/lon → 4-char grid square)
# ---------------------------------------------------------------------------


def _latlon_to_grid(lat: float, lon: float) -> str:
    """Convert latitude / longitude to a 4-character Maidenhead locator."""
    lon_adj = lon + 180.0
    lat_adj = lat + 90.0
    field_lon = int(lon_adj / 20)
    field_lat = int(lat_adj / 10)
    sq_lon = int((lon_adj % 20) / 2)
    sq_lat = int(lat_adj % 10)
    return chr(ord("A") + field_lon) + chr(ord("A") + field_lat) + str(sq_lon) + str(sq_lat)
