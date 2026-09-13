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

# Receive-log item roles: (lat, lon) tuple, plus the Plain / Raw text variants
# so the display toggle can re-render existing rows without re-parsing.
_ROLE_COORDS = int(Qt.ItemDataRole.UserRole)
_ROLE_PLAIN = int(Qt.ItemDataRole.UserRole) + 1
_ROLE_RAW = int(Qt.ItemDataRole.UserRole) + 2

# app_settings key for the receive-log display toggle ("plain" | "raw").
_DISPLAY_MODE_KEY = "aprs_display_mode"

# Default via path for ISS digipeater
_DEFAULT_VIA = "ARISS"

# Generic digipeater path aliases accepted by APRS-capable satellite
# digipeaters (ISS/ARISS, and historically PCSAT/PCSAT2/PSAT) — all three
# are configured on the same digipeaters, so any one works without
# reconfiguring per satellite. Source: aprs.org ISS/APRS FAQ
# (https://www.aprs.org/ariss.html) — "All APRS satellites and the ISS use
# any of the three generic paths of ARISS or APRSAT or WIDE." The combo box
# is editable so a satellite-specific path can still be typed in.
_VIA_PATH_CHOICES = ("ARISS", "APRSAT", "WIDE")

# app_settings key for user-typed Via values, persisted so they remain
# selectable in the dropdown across sessions (see _load_via_choices()).
_VIA_CUSTOM_SETTING_KEY = "aprs_custom_via_paths"

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine).
# The Telemetry tab's Direwolf (AX.25) mode shares the same engine under its
# own "telemetry" tag so closing one tab doesn't stop the other's reception.
_ENGINE_OWNER = "aprs"


class AprsTab(QWidget):
    """Non-resident tab opened from Communications > APRS.

    Persists callsign / SSID / via settings in ``app_settings`` under the
    key ``aprs_settings``.  Received packets are stored in ``aprs_log`` (DB
    table created on first open if absent).

    Signals
    -------
    open_map_url(str)
        Emitted when the user picks "Open in Google Maps" from a received
        packet's context menu. Payload: the Google Maps URL to open in an
        app-mode browser window (MainWindow wires this to _open_url_app_mode).
    """

    open_map_url: Signal = Signal(str)

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

        # Callsigns we have sent a message to this session (base call, no SSID).
        # A QSO is logged only when the remote station replies with a real message.
        self._pending_qso: set[str] = set()

        # Receive-log display mode ("plain" | "raw"); restored in _load_display_mode().
        self._display_mode = "plain"

        # Auto-beacon timer for position transmission
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._on_send_position)

        # APRS engine (Direwolf backend) — process-wide singleton, shared
        # with the Telemetry tab's Direwolf (AX.25) mode
        from comms.aprs.engine import get_aprs_engine

        self._engine = get_aprs_engine(conn)
        self._engine.packet_received.connect(self._on_packet_received)
        self._engine.status_changed.connect(self._on_engine_status)
        self._engine.error_occurred.connect(self._on_engine_error)

        self._ensure_db_table()
        self._setup_ui()
        self._load_via_choices()
        self._load_display_mode()
        self._load_settings()
        self._load_baud_mode()
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
            "SELECT received_at, callsign, via, comment, raw_frame, "
            "latitude_deg, longitude_deg "
            "FROM aprs_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        for row in reversed(rows):
            body = row["comment"] or row["raw_frame"] or ""
            self._append_log_item(
                ts=row["received_at"],
                callsign=row["callsign"],
                via=row["via"] or "",
                plain=body,
                raw=row["raw_frame"] or body,
                lat=row["latitude_deg"],
                lon=row["longitude_deg"],
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
        self._via_edit = QComboBox()
        self._via_edit.setEditable(True)
        self._via_edit.addItems(_VIA_PATH_CHOICES)
        self._via_edit.setCurrentText(_DEFAULT_VIA)
        self._via_edit.setFixedWidth(120)
        self._via_edit.setToolTip(
            _(
                "Digipeater path. ARISS / APRSAT / WIDE are the generic\n"
                "aliases most APRS-capable satellite digipeaters (ISS and\n"
                "others) accept — any one of them works without\n"
                "reconfiguring for a specific satellite. Type a different\n"
                "value if a particular satellite needs its own path.\n"
                "Typed values are remembered in the dropdown — right-click\n"
                "one to remove it."
            )
        )
        via_line_edit = self._via_edit.lineEdit()
        assert via_line_edit is not None  # always present: setEditable(True) above
        via_line_edit.editingFinished.connect(self._on_via_edited)
        via_line_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        via_line_edit.customContextMenuRequested.connect(self._on_via_context_menu)
        row1.addWidget(QLabel(_("Via:")))
        row1.addWidget(self._via_edit)

        row1.addSpacing(12)
        self._baud_combo = QComboBox()
        self._baud_combo.addItem(_("Auto"), "auto")
        self._baud_combo.addItem("1200", "1200")
        self._baud_combo.addItem("4800", "4800")
        self._baud_combo.addItem("9600", "9600")
        self._baud_combo.setToolTip(
            _(
                "AX.25 baud rate for Direwolf (Rig + Sound Card reception).\n"
                "Auto reads the selected transponder's baud rate from SATNOGS\n"
                "(defaults to 1200 if unknown). Only applies to Rig + Sound\n"
                "Card reception — SDR-only reception is always 1200 baud AFSK."
            )
        )
        self._baud_combo.currentIndexChanged.connect(self._on_baud_mode_changed)
        row1.addWidget(QLabel(_("Baud:")))
        row1.addWidget(self._baud_combo)

        # Receive-log display toggle: Plain (humanised sentence) vs Raw (the
        # on-air APRS information field verbatim).
        row1.addSpacing(18)
        self._display_combo = QComboBox()
        self._display_combo.addItem(_("Plain"), "plain")
        self._display_combo.addItem(_("Raw packet"), "raw")
        self._display_combo.setToolTip(
            _(
                "Plain: a human-readable summary of each packet (position,\n"
                "message, status…). Raw packet: the APRS information field\n"
                "exactly as received on the air."
            )
        )
        self._display_combo.currentIndexChanged.connect(self._on_display_mode_changed)
        row1.addWidget(QLabel(_("Show:")))
        row1.addWidget(self._display_combo)

        row1.addStretch()
        settings_form.addRow(row1)

        # Row 2: input source (read-only display)
        self._input_label = QLabel(_("Input: —"))
        self._input_label.setStyleSheet("color: #aaa;")
        # Without word wrap, a long DirewolfManager error string forces
        # QLabel's minimumSizeHint to fit the whole line, widening the
        # window and blocking shrinking it back.
        self._input_label.setWordWrap(True)
        _input_row_w = QWidget()
        _input_row_l = QHBoxLayout(_input_row_w)
        _input_row_l.setContentsMargins(0, 0, 0, 0)
        _input_row_l.setSpacing(6)
        _input_row_l.addWidget(self._input_label)
        _input_row_l.addStretch()
        settings_form.addRow(_input_row_w)

        root.addWidget(settings_group)

        # -- Receive log --
        log_group = QGroupBox(_("Received Packets"))
        log_layout = QVBoxLayout(log_group)
        self._log_list = QListWidget()
        self._log_list.setAlternatingRowColors(True)
        self._log_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Enlarge the packet-log font ~1.5x; the raw AX.25 info field is dense
        # and the default size is hard to read.
        _log_font = self._log_list.font()
        _log_pt = _log_font.pointSizeF()
        if _log_pt > 0:
            _log_font.setPointSizeF(_log_pt * 1.5)
        else:
            _log_font.setPixelSize(max(1, round(_log_font.pixelSize() * 1.5)))
        self._log_list.setFont(_log_font)
        # Right-click a packet that carries a position → "Open in Google Maps"
        self._log_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._log_list.customContextMenuRequested.connect(self._on_log_context_menu)
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
        if hasattr(rc, "transmitter_changed"):
            rc.transmitter_changed.connect(self._on_transmitter_changed)

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

        modem = self._engine.current_modem
        baud_suffix = f"  [{modem} baud]" if modem else ""

        can_tx = self._rig_connected and sc_ok
        if can_tx:
            self._input_label.setText(
                _("Input: Sound Card + Direwolf  (send + receive)") + baud_suffix
            )
            self._input_label.setStyleSheet("color: #7bed9f;")
            self._send_btn.setEnabled(True)
        elif self._sdr_connected:
            label = self._sdr_label or "SDR"
            self._input_label.setText(_("Input: {dev}  (receive only — SDR)").format(dev=label))
            self._input_label.setStyleSheet("color: #4a9eff;")
            self._send_btn.setEnabled(False)
        elif sc_ok and self._engine.is_running:
            self._input_label.setText(
                _("Input: Sound Card + Direwolf  (receive only — Rig not connected)") + baud_suffix
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
        via = self._via_edit.currentText().strip()
        if cs:
            from comms.aprs.engine import resolve_ax25_modem

            modem = resolve_ax25_modem(self._conn, self._radio_control)
            self._engine.start_rig(_ENGINE_OWNER, cs, ssid, via, modem=modem)

    def _get_sdr_pipeline(self) -> Any | None:
        """Return the connected SDR rig's pipeline (Rig 1 or Rig 2), if any."""
        rc = self._radio_control
        for attr in ("_rig1", "_rig2"):
            rig = getattr(rc, attr, None)
            if rig is not None and getattr(rig, "is_sdr", False):
                return getattr(rig, "_pipeline", None)
        return None

    def _try_start_sdr(self, rig2: object) -> None:
        """Start AX.25 reception on the SDR pipeline (receive only).

        Uses the lightweight AfskDemodulator (1200 baud Bell 202, no
        Direwolf dependency) unless the resolved baud is 4800/9600, in
        which case Direwolf's built-in G3RUH decoder is used instead (fed
        by a raw-discriminator demod of the SDR's I/Q — see
        AprsEngine.start_sdr_direwolf()).
        """
        if self._engine.is_running:
            if not self._rig_connected:
                # The engine is only running because Sound Card happened to
                # be configured (see _try_start_engine()'s "no rig needed"
                # design in __init__) -- no actual Hamlib rig is connected,
                # so nothing genuinely needs that session. An SDR connecting
                # now means the user wants SDR reception; replace the
                # passive/no-rig session with it rather than silently
                # leaving reception stuck on whatever audio device Sound
                # Card was last configured with (e.g. a real rig's
                # soundcard from a previous session, reading silence).
                # Reported live: SDR showed as the connected input, but
                # nothing ever decoded, and changing Baud visibly affected
                # a Rig + Sound Card session instead of SDR.
                self._engine.stop(_ENGINE_OWNER)
            else:
                # A real (non-SDR) rig is genuinely connected and already
                # driving the engine -- don't steal it out from under TX.
                return
        pipeline = getattr(rig2, "_pipeline", None)
        if pipeline is None:
            return
        from comms.aprs.engine import resolve_ax25_modem

        modem = resolve_ax25_modem(self._conn, self._radio_control)
        if modem in ("4800", "9600"):
            self._engine.start_sdr_direwolf(_ENGINE_OWNER, pipeline, modem=modem)
        else:
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
            plain=packet.plain or packet.comment,
            plain_callsign=packet.plain_callsign,
        )

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
    # Via path choices (built-in aliases + persisted custom entries)
    # ------------------------------------------------------------------ #

    def _load_via_choices(self) -> None:
        """Add previously-typed custom Via values to the dropdown."""
        if not hasattr(self._conn, "execute"):
            return
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_VIA_CUSTOM_SETTING_KEY,),
        ).fetchone()
        if not row or not row["value"]:
            return
        try:
            custom = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(custom, list):
            return
        for value in custom:
            if isinstance(value, str) and value and self._via_edit.findText(value) < 0:
                self._via_edit.addItem(value)

    def _save_custom_via_choices(self) -> None:
        """Persist every dropdown entry that isn't one of the built-in aliases."""
        if not hasattr(self._conn, "execute"):
            return
        custom = [
            self._via_edit.itemText(i)
            for i in range(self._via_edit.count())
            if self._via_edit.itemText(i) not in _VIA_PATH_CHOICES
        ]
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (_VIA_CUSTOM_SETTING_KEY, json.dumps(custom)),
        )
        self._conn.commit()

    def _on_via_edited(self) -> None:
        """Remember a manually-typed Via value in the dropdown once committed
        (Enter pressed, or focus leaves the field)."""
        value = self._via_edit.currentText().strip()
        if not value or self._via_edit.findText(value) >= 0:
            return
        self._via_edit.addItem(value)
        self._save_custom_via_choices()

    def _on_via_context_menu(self, pos: Any) -> None:
        """Standard line-edit context menu, plus a "Remove" entry for the
        currently-shown value if it's a user-added (non-built-in) one."""
        line_edit = self._via_edit.lineEdit()
        assert line_edit is not None  # always present: setEditable(True) in _setup_ui()
        menu = line_edit.createStandardContextMenu()
        current = self._via_edit.currentText().strip()
        if current and current not in _VIA_PATH_CHOICES and self._via_edit.findText(current) >= 0:
            menu.addSeparator()
            action = menu.addAction(_('Remove "{value}" from list').format(value=current))
            action.triggered.connect(lambda: self._remove_via_choice(current))
        menu.exec(line_edit.mapToGlobal(pos))

    def _remove_via_choice(self, value: str) -> None:
        idx = self._via_edit.findText(value)
        if idx >= 0:
            self._via_edit.removeItem(idx)
        self._save_custom_via_choices()
        if self._via_edit.currentText().strip() == value:
            self._via_edit.setCurrentText(_DEFAULT_VIA)

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
            self._via_edit.setCurrentText(str(via))
        self._load_log_from_db()

    def _save_settings(self) -> None:
        """Persist callsign / SSID / via to app_settings."""
        if not hasattr(self._conn, "execute"):
            return
        data = {
            "callsign": self._callsign_edit.text().strip().upper(),
            "ssid": self._ssid_spin.value(),
            "via": self._via_edit.currentText().strip(),
        }
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('aprs_settings', ?, CURRENT_TIMESTAMP)",
            (json.dumps(data),),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # AX.25 baud mode (shared with the Telemetry tab's Direwolf (AX.25) mode)
    # ------------------------------------------------------------------ #

    def _load_baud_mode(self) -> None:
        """Restore the Auto/1200/4800/9600 selection from app_settings."""
        from comms.aprs.engine import AX25_BAUD_MODE_CHOICES, AX25_BAUD_SETTING_KEY

        mode = "auto"
        if hasattr(self._conn, "execute"):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (AX25_BAUD_SETTING_KEY,),
            ).fetchone()
            if row and row["value"] in AX25_BAUD_MODE_CHOICES:
                mode = row["value"]
        idx = self._baud_combo.findData(mode)
        self._baud_combo.blockSignals(True)
        self._baud_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._baud_combo.blockSignals(False)

    def _on_baud_mode_changed(self, _index: int) -> None:
        """Persist the Auto/1200/4800/9600 selection and apply it immediately."""
        from comms.aprs.engine import AX25_BAUD_SETTING_KEY

        mode = self._baud_combo.currentData()
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (AX25_BAUD_SETTING_KEY, mode),
            )
            self._conn.commit()
        self._apply_baud_change()

    def _on_transmitter_changed(self, _xpdr: object) -> None:
        """Restart the AX.25 pipeline if the newly selected transponder's baud differs."""
        self._apply_baud_change()

    def _apply_baud_change(self) -> None:
        """Re-resolve the target baud and apply it to whichever source is active.

        Rig + Sound Card (Direwolf) sessions are restarted in place via
        restart_if_modem_changed(); SDR sessions may need a full mechanism
        switch (AfskDemodulator <-> SDR-fed Direwolf) via sync_sdr_baud().
        Both methods only act on the session type they own (checked via the
        engine's internal state, not this tab's), so calling both
        unconditionally is safe — whichever doesn't apply is a no-op.
        """
        from comms.aprs.engine import resolve_ax25_modem

        modem = resolve_ax25_modem(self._conn, self._radio_control)
        self._engine.restart_if_modem_changed(modem)
        pipeline = self._get_sdr_pipeline()
        if pipeline is not None:
            self._engine.sync_sdr_baud(pipeline, modem)

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
        plain: str | None = None,
        plain_callsign: str | None = None,
    ) -> None:
        """Add a decoded APRS packet to the receive log widget.

        *comment* is the legacy short summary (still what gets persisted /
        exported). The receive log itself shows either *plain* (a full
        human-readable line; falls back to *comment*) or *raw_frame* (the
        on-air information field; falls back to *comment*), per the Show
        toggle. *plain_callsign*, when set (third-party / I-Gate-relayed
        packets), replaces *callsign* in the Plain-mode prefix so the row
        reads as the originating station rather than the relaying I-Gate;
        Raw mode always keeps the on-air *callsign*/*via*. Persists to the
        DB only when *log* is True.
        """
        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._append_log_item(
            ts,
            callsign,
            via,
            plain=plain if plain is not None else comment,
            raw=raw_frame or comment,
            lat=lat,
            lon=lon,
            plain_callsign=plain_callsign,
        )

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

    def _append_log_item(
        self,
        ts: str,
        callsign: str,
        via: str,
        *,
        plain: str,
        raw: str,
        lat: float | None = None,
        lon: float | None = None,
        plain_callsign: str | None = None,
    ) -> None:
        via_str = f",{via}" if via else ""
        raw_text = f"{ts}  {callsign}{via_str}: {raw}"
        # Raw always shows the on-air source (e.g. the relaying I-Gate). Plain
        # swaps in the originating station when this was a third-party /
        # I-Gate-relayed packet — the outer via (e.g. "NOGATE") describes the
        # relay, not the origin's path, so it's dropped rather than shown
        # attached to the wrong callsign.
        plain_text = (
            f"{ts}  {plain_callsign}: {plain}"
            if plain_callsign
            else f"{ts}  {callsign}{via_str}: {plain}"
        )
        item = QListWidgetItem(plain_text if self._display_mode == "plain" else raw_text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setData(_ROLE_PLAIN, plain_text)
        item.setData(_ROLE_RAW, raw_text)
        # Stash the position (if any) so the right-click menu can offer a map link.
        if lat is not None and lon is not None:
            item.setData(_ROLE_COORDS, (float(lat), float(lon)))
        self._log_list.addItem(item)
        self._log_list.scrollToBottom()

    # ------------------------------------------------------------------ #
    # Receive-log display toggle (Plain / Raw)
    # ------------------------------------------------------------------ #

    def _load_display_mode(self) -> None:
        """Restore the Plain/Raw selection from app_settings."""
        mode = "plain"
        if hasattr(self._conn, "execute"):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_DISPLAY_MODE_KEY,)
            ).fetchone()
            if row and row["value"] in ("plain", "raw"):
                mode = row["value"]
        self._display_mode = mode
        idx = self._display_combo.findData(mode)
        self._display_combo.blockSignals(True)
        self._display_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._display_combo.blockSignals(False)

    def _on_display_mode_changed(self, _index: int) -> None:
        """Persist the Plain/Raw selection and re-render the existing rows."""
        mode = self._display_combo.currentData() or "plain"
        self._display_mode = mode
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_DISPLAY_MODE_KEY, mode),
            )
            self._conn.commit()
        role = _ROLE_PLAIN if mode == "plain" else _ROLE_RAW
        for i in range(self._log_list.count()):
            it = self._log_list.item(i)
            text = it.data(role)
            if isinstance(text, str):
                it.setText(text)

    def _on_log_context_menu(self, pos: Any) -> None:
        """Show "Open in Google Maps" for a received packet that carries a position."""
        from PySide6.QtWidgets import QMenu

        item = self._log_list.itemAt(pos)
        if self._item_coords(item) is None:
            return
        menu = QMenu(self._log_list)
        action = menu.addAction(_("Open in Google Maps"))
        if menu.exec(self._log_list.mapToGlobal(pos)) is action:
            self._open_item_on_map(item)

    @staticmethod
    def _item_coords(item: QListWidgetItem | None) -> tuple[float, float] | None:
        """Return the (lat, lon) stashed on *item*, or None when it has no position."""
        if item is None:
            return None
        coords = item.data(_ROLE_COORDS)
        if isinstance(coords, tuple) and len(coords) == 2:
            return float(coords[0]), float(coords[1])
        return None

    def _open_item_on_map(self, item: QListWidgetItem | None) -> None:
        """Emit open_map_url with a Google Maps link for *item*'s position."""
        coords = self._item_coords(item)
        if coords is not None:
            self.open_map_url.emit(_google_maps_url(coords[0], coords[1]))

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
        via = self._via_edit.currentText().strip()

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
        via = self._via_edit.currentText().strip()
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


def _google_maps_url(lat: float, lon: float) -> str:
    """Build a Google Maps URL that drops a pin at (lat, lon) and sets a zoom.

    The ``?q=<lat>,<lon>&z=<n>`` form is undocumented but works across
    desktop and mobile; the documented ``search/?api=1&query=`` form drops a
    pin but ignores zoom, which is too wide for a precise APRS fix.
    """
    return f"https://www.google.com/maps?q={float(lat):.6f},{float(lon):.6f}&z=15"


def _latlon_to_grid(lat: float, lon: float) -> str:
    """Convert latitude / longitude to a 4-character Maidenhead locator."""
    lon_adj = lon + 180.0
    lat_adj = lat + 90.0
    field_lon = int(lon_adj / 20)
    field_lat = int(lat_adj / 10)
    sq_lon = int((lon_adj % 20) / 2)
    sq_lat = int(lat_adj % 10)
    return chr(ord("A") + field_lon) + chr(ord("A") + field_lat) + str(sq_lon) + str(sq_lat)
