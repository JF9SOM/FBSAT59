"""Telemetry tab widget — Communications > Telemetry.

Receives AX.25 frames from:
  - Bell 202 AFSK Python demodulator (SDR receive path)
  - Direwolf / KISS (via Rig + Sound Card)
  - gr-satellites subprocess (SDR path, 300+ satellites including 9k6 FSK)
  - the CW Decoder tab ("CW TLM" mode): Morse-coded hexadecimal housekeeping
    frames, cut out of the decoded CW text (see comms.telemetry.cw_frames)

Decodes frames using JSON format definitions in
src/data/telemetry_formats/{norad}.json.
Satellites without a definition show raw hex.

All received frames are persisted to the ``telemetry_log`` SQLite table
and can be exported as CSV.
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import json
import logging
import math
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QShortcut,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.engine import get_aprs_engine
from comms.aprs.parser import decode_ax25
from comms.signal_clock import replay_time, signal_time
from comms.telemetry.ascii_view import (
    addressed_ascii,
    ax25_split,
    describe_parts,
    hex_text_to_bytes,
    looks_like_text,
    row_ascii,
    summary_text,
)
from comms.telemetry.baud_detect import (
    BAUD_CHOICES,
    detect_baud_from_satyaml,
    detect_baud_from_transmitter,
)
from comms.telemetry.cw_frames import (
    build_lenient_satnogs_frame,
    decode_cw_frame,
    fields_from_saved,
    is_near_miss,
    load_cw_frames,
    near_miss_hex,
    normalize_block,
    repair_block,
    split_frame_blocks,
)
from comms.telemetry.cw_upload import (
    SendReport,
    auto_send,
    ensure_columns,
    mark_uploaded,
    reset_unconfirmed_marks,
    send_frames,
)
from comms.telemetry.decoder import (
    TelemetryFrame,
    decode_telemetry,
    get_telemetry_id_defs,
    list_formats,
    load_format,
)
from comms.telemetry.gr_satellites_backend import (
    GrSatellitesBackend,
    detect_gr_satellites,
    get_satellite_info,
    list_gr_satellites_with_names,
    map_provisional_to_tracked,
)
from comms.telemetry.hades_fields import telemetry_frame as hades_telemetry_frame
from comms.telemetry.hades_fields import type_tabs as hades_type_tabs
from comms.telemetry.satnogs_uploader import (
    get_satnogs_uploader,
    get_station_callsign,
    get_station_latlon,
    load_satnogs_upload_settings,
    save_satnogs_upload_settings,
    upload_blocker,
)
from i18n import _
from ui.sat_search_dialog import SatSearchDialog

logger = logging.getLogger(__name__)

# Named after the protocol rather than the backend: SDR sessions now run our
# own coherent MSK detector alongside Direwolf (2026-09-24), so "Direwolf"
# no longer describes every decoder in use.
_MODE_AFSK = "AX.25"
_MODE_GR = "gr-satellites"
# Housekeeping frames sent as Morse-coded hex, decoded by the CW Decoder tab
# (see telemetry_formats/{norad}.json's "cw_frames"; ARICA-2, OrigamiSat-2).
_MODE_CW = "CW TLM"
# 200 baud 2FSK bursts of the AMSAT-EA "Genesis" family (UNNE-1B), demodulated from the SDR's
# I/Q by comms.telemetry.hades_fsk. Verified frames are uploaded to the SatNOGS DB like the
# other modes' (the descrambled payload without its CRC, the form SatNOGS Network shows).
_MODE_HADES = "HADES FSK"
# How long a Start waits for the SDR to finish connecting before it is dropped.
_PENDING_START_TIMEOUT_S = 30.0
# app_settings key for the "Speed:" choice of Decode IQ Recording in HADES FSK mode.
_DECODE_SPEED_KEY = "telemetry_decode_speed"
# AX.25 bauds with a file decoder: the coherent MSK detector at 4800 / 9600, Direwolf fed with the
# SDR audio front end's output at 1200
_FAST_AX25_BAUDS = ("1200", "4800", "9600")

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine).
# The APRS tab shares the same engine under its own "aprs" tag so closing
# one tab doesn't stop the other's reception.
_ENGINE_OWNER = "telemetry"
# Own setting (not the APRS tab's shared ax25_baud_mode): this combo now follows
# the selected satellite/transponder instead of an Auto mode.
_BAUD_SETTING_KEY = "telemetry_ax25_baud"
# app_settings key of the mode (AX.25 / gr-satellites / CW TLM / HADES FSK) the tab was left in.
_MODE_SETTING_KEY = "telemetry_mode"
# app_settings key prefix for the last satellite picked per mode ("afsk"/"gr"/"cw_tlm").
_LAST_SAT_KEY_PREFIX = "telemetry_last_sat_"


class _SatnogsApiKeyDialog(QDialog):
    """Small popup for entering the SatNOGS DB API key (plain text)."""

    def __init__(self, current: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("SatNOGS DB API Key"))
        layout = QVBoxLayout(self)

        info = QLabel(
            _(
                "Log in at db.satnogs.org, open your account Settings, and copy "
                "the API Key shown there. It is required to upload telemetry "
                "frames to the SatNOGS database."
            )
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self._edit = QLineEdit(current)
        self._edit.setPlaceholderText(_("Paste your SatNOGS DB API key"))
        self._edit.setMinimumWidth(360)
        layout.addWidget(self._edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def api_key(self) -> str:
        return self._edit.text().strip()


def format_frame_hex(raw: bytes) -> str:
    """*raw* as upper-case hex bytes separated by spaces (``94 A6 62 ...``)."""
    return raw.hex(" ").upper()


def parse_gr_pdu_dump(text: str) -> bytes | None:
    """The frame's bytes from a gr-satellites "VERBOSE PDU DEBUG PRINT" hex dump, else None.

    gr-satellites prints a frame whose telemetry format it does not know as a dump, one
    ``0000: 12 98 46 ...`` line per 16 bytes, instead of a decoded packet.
    """
    data = bytearray()
    for line in text.splitlines():
        match = re.fullmatch(r"[0-9a-fA-F]{4}:((?: [0-9a-fA-F]{2})+) ?", line.strip())
        if match:
            data += bytes.fromhex(match.group(1).replace(" ", ""))
    return bytes(data) if data else None


def parse_gr_packet(text: str) -> tuple[list[str], bytes | None]:
    """Pull the AX.25 addresses and the info field out of a gr-satellites packet printout.

    gr-satellites prints a decoded AX.25 packet as a construct dump: one ``callsign = u'X'``
    and ``ssid = N`` per address (destination, source, then digipeaters) and the payload as
    ``info = b'...' (total N)``. Returns the addresses as ``CALL`` / ``CALL-N`` strings and
    the info bytes; an empty list / None when the text has no such part (a non-AX.25 packet).
    """
    addresses: list[str] = []
    ssids: list[int] = []
    info: bytes | None = None
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"callsign = u?(['\"])(.*)\1", stripped)
        if match:
            addresses.append(match.group(2).strip())
            ssids.append(0)
            continue
        match = re.fullmatch(r"ssid = (\d+)", stripped)
        if match and ssids:
            ssids[-1] = int(match.group(1))
            continue
        match = re.match(r"info = (b(['\"]).*\2)(?: \(total \d+\))?$", stripped)
        if match:
            try:
                value = ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, bytes):
                info = value
    labels = [f"{c}-{n}" if n else c for c, n in zip(addresses, ssids, strict=True)]
    return labels, info


# Rows logged before 2026-10-22 carry this label in their saved text (in the language of the
# time); it is dropped when they are shown again.
_HADES_REPAIRED_LABEL = re.compile(r" \((?:repaired, not verified|修復済み・未検証)\)")
_AX25_ADDRESS = re.compile(r"[A-Z0-9]{1,6}")


def gr_packet_is_bogus(text: str) -> bool:
    """True when a gr-satellites AX.25 printout cannot be a real frame (noise that passed the CRC).

    A CRC-16 matches by chance about once in 65536 candidates, and noise holds tens of thousands
    of flag-like patterns, so a long recording of nothing decodes the odd frame whose "addresses"
    are binary junk. A real AX.25 header has a destination and a source made of A-Z / 0-9.
    Output without any address (a framing gr-satellites does not parse as AX.25) is not judged.
    """
    addresses, _info = parse_gr_packet(text)
    if not addresses and "callsign = " not in text:
        return False
    if len(addresses) < 2:
        return True
    return any(not _AX25_ADDRESS.fullmatch(a.split("-")[0]) for a in addresses)


# Item role marking a Received Frames row as a rejected candidate (drawn muted).
_DIM_ROLE = Qt.ItemDataRole.UserRole + 1
# Row role holding the _Ax25Row of a received AX.25 frame (for "Send selected").
_AX25_ROLE = Qt.ItemDataRole.UserRole + 2
# Row role holding the ``telemetry_log`` id of a logged AX.25/HDLC row (no duplicates on load).
_LOGGED_ROLE = Qt.ItemDataRole.UserRole + 3
_DECODED_ROLE = Qt.ItemDataRole.UserRole + 4  # a CW row's decoded TelemetryFrame
_GR_TEXT_ROLE = Qt.ItemDataRole.UserRole + 5  # a gr-satellites row's full decoded text
# gr-satellites prints a decoded packet and, on its KISS port, the same frame; the two reach
# the GUI separately. A row and a frame arriving within this many seconds are one packet.
_GR_PAIR_WINDOW_S = 5.0
# Decoding the same recording twice logs the same frame again with a reception time that
# differs by a fraction of a second (up to ~2 s measured); frames of equal content within
# this many seconds are one reception, not two.
_DUPLICATE_WINDOW_S = 5.0
# A gr-satellites fast decode only estimates a frame's time (a few seconds off, more on a long
# recording), so the same frame from two runs over one recording is up to this far apart.
_GR_FAST_DUPLICATE_WINDOW_S = 60.0


@dataclass
class _Ax25Row:
    """A received AX.25 frame as shown in the table, kept for a manual SatNOGS send.

    *raw* is the full frame (FCS already stripped) -- exactly what SatNOGS wants;
    the logged ``telemetry_log.raw_hex`` holds only the payload. *norad* is None
    when the source callsign could not be mapped to a satellite.
    """

    raw: bytes
    when: datetime.datetime
    reliable: bool
    norad: int | None
    sent: bool = False  # SatNOGS accepted it
    pending: bool = False  # handed to the uploader, no answer yet
    manual: bool = False  # the user asked for this send
    log_id: int | None = None  # its ``telemetry_log`` row
    payload: bytes = b""  # the AX.25 info field; re-decoded when the row is selected


class _HighlightTabBar(QTabBar):
    """A tab bar that tints the selected tab with the theme's selection colour.

    The default look of the selected tab differs only slightly from the others (hardly
    visible in a dark theme), which made it hard to see which of a satellite's many
    "Decoded Fields" sub-tabs is open. The tint is painted over the normal tab, so any style
    works and the label stays readable.
    """

    _TINT_ALPHA = 110  # 0..255

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        super().paintEvent(event)
        index = self.currentIndex()
        if index < 0:
            return
        tint = QColor(self.palette().color(QPalette.ColorRole.Highlight))
        tint.setAlpha(self._TINT_ALPHA)
        painter = QPainter(self)
        painter.fillRect(self.tabRect(index).adjusted(1, 1, -1, 0), tint)
        painter.end()


class _DecodeTabs(QTabWidget):
    """The "Decoded Fields" sub-tabs, with the selected one tinted (see _HighlightTabBar)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTabBar(_HighlightTabBar(self))


class _DimRowDelegate(QStyledItemDelegate):
    """Draws rows marked with _DIM_ROLE in a muted, italic style that stays readable.

    A fixed grey foreground (the earlier approach) is unreadable whenever it
    lands on a background of a similar grey -- the selection highlight, say.
    Here the muted colour is a blend of the *theme's* text and base colours,
    and a selected row keeps the theme's own selection colours (only italics
    mark it as muted).
    """

    _TEXT_WEIGHT = 0.6  # share of the text colour in the muted colour

    def initStyleOption(self, option: QStyleOptionViewItem, index: Any) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        if not index.data(_DIM_ROLE):
            return
        option.font.setItalic(True)
        if option.state & QStyle.StateFlag.State_Selected:
            return
        text = option.palette.color(QPalette.ColorRole.Text)
        base = option.palette.color(QPalette.ColorRole.Base)
        w = self._TEXT_WEIGHT
        muted = QColor(
            round(text.red() * w + base.red() * (1 - w)),
            round(text.green() * w + base.green() * (1 - w)),
            round(text.blue() * w + base.blue() * (1 - w)),
        )
        option.palette.setColor(QPalette.ColorRole.Text, muted)


class _ProcessLogDialog(QDialog):
    """Modeless window showing a backend's own console log file.

    Reused for both comms.aprs.direwolf_log (Direwolf's stdout, only
    populated during SDR-fed reception -- a Rig + Sound Card session plays
    that same stdout back as audio instead) and
    comms.telemetry.gr_satellites_log (gr_satellites' stdout + stderr). The
    log file is written by a background thread outside this dialog's
    control, so this just re-reads it on open/refresh rather than streaming
    live.
    """

    def __init__(self, title: str, log_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._log_path = log_path
        self.setWindowTitle(title)
        self.resize(640, 320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(5000)
        self._view.setStyleSheet("font-family: monospace; font-size: 10px;")
        layout.addWidget(self._view)
        btn_row = QHBoxLayout()
        btn_refresh = QPushButton(_("🔄 Refresh"))
        btn_refresh.clicked.connect(self.reload)
        btn_row.addStretch()
        btn_row.addWidget(btn_refresh)
        layout.addLayout(btn_row)
        self.reload()

    def reload(self) -> None:
        path = Path(self._log_path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        except OSError as exc:
            text = f"[failed to read {path}: {exc}]"
        self._view.setPlainText(text)
        scrollbar = self._view.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        # Hide rather than destroy so re-opening doesn't recreate the window
        event.ignore()
        self.hide()


class TelemetryTab(QWidget):
    """Non-resident tab opened from Communications > Telemetry."""

    # emitted when user picks a satellite in either combo: (norad, mode_str)
    satellite_selected = Signal(int, str)
    # emitted when the "SatNOGS ↗" footer button is clicked: (norad, name)
    open_satnogs_requested = Signal(int, str)
    # CW TLM mode's ▶ Start / ■ Stop: MainWindow opens the CW Decoder tab, hands it to
    # attach_cw_tab() and starts / stops its decoding.
    cw_tlm_start_requested = Signal()
    cw_tlm_stop_requested = Signal()
    # Fast decode of an IQ recording in CW TLM mode: MainWindow opens the CW Decoder tab
    # and hands it the file (a pathlib.Path).
    cw_tlm_fast_requested = Signal(object)
    # HADES FSK mode, UNNE-1B voice (type 15): the first packet of a run asks MainWindow to open
    # the Digital Voice tab (behind this one); every verified packet is then handed to it as
    # (packet number, the 35 CODEC2 bytes with the mask removed, stream time in seconds).
    voice_tab_requested = Signal()
    voice_packet_ready = Signal(int, bytes, float)
    # SatNOGS's answer to one upload, emitted from the uploader's thread (delivered on the
    # GUI thread): (telemetry_log id or None for an AX.25 frame, accepted, HTTP status, body).
    _upload_result = Signal(object, bool, int, str)
    _ax25_send_result = Signal(object, bool, int, str)

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
        # duplicate Direwolf processes.
        self._engine = get_aprs_engine(conn)
        self._afsk_source: str | None = None  # "direwolf" | "sdr_direwolf" | None
        self._sdr_pipeline: object | None = None
        self._rig_connected = False
        self._sdr_connected = False
        # Start pressed while the SDR was still connecting: begin decoding as soon
        # as it is up (see _auto_connect_sdr / _resume_pending_start).
        self._pending_start = False
        # Set by "Decode IQ Recording": start decoding once the user clicks the overview.
        self._start_on_iq_seek = False
        self._pending_start_at = 0.0
        self._resuming_start = False

        # gr-satellites backend
        self._gr_backend = GrSatellitesBackend(self)
        self._gr_backend.telemetry_received.connect(self._on_gr_telemetry)
        self._gr_backend.status_changed.connect(self._on_gr_status)
        self._gr_backend.raw_frame_received.connect(self._on_gr_raw_frame)
        self._gr_sat_list: list[tuple[int, str]] = []  # (norad, name) sorted by name
        # This app's NORAD -> the id gr-satellites' catalog uses for it, only for
        # satellites the catalog still lists under a provisional id (see
        # _populate_gr_combo()).
        self._gr_catalog_ids: dict[int, int] = {}

        # Selected satellite from main satellite list (set_satellite from main_window)
        self._selected_norad: int | None = None
        # The satellite the Received Frames table currently shows (None: not chosen yet).
        self._table_norad: int | None = None
        # gr-satellites rows still waiting for their frame, and frames for their row (FIFO).
        self._gr_rows: deque[tuple[float, _Ax25Row]] = deque()
        self._gr_frames: deque[tuple[float, _Ax25Row]] = deque()
        # Times of gr-satellites packets rejected as noise whose frame has not arrived yet.
        self._gr_bogus_pending: deque[float] = deque()
        self._gr_bogus_count = 0
        # Owned by the tab so it dies with it (a bare QTimer.singleShot on a bound method
        # fired after the widget was gone and crashed later tests).
        self._gr_prune_timer = QTimer(self)
        self._gr_prune_timer.setSingleShot(True)
        self._gr_prune_timer.timeout.connect(self._gr_prune)
        self._selected_name: str = ""

        # HADES FSK mode: the demodulator thread, the pipeline it is subscribed to and the
        # satellite its frames belong to while it runs.
        self._hades_demod: Any | None = None
        self._hades_demod_pipeline: Any | None = None
        self._hades_norad: int | None = None
        # "Import CSV…": frames from SatNOGS DB are being added (their times come from the
        # file, they are never sent back, and the log is checked for copies).
        self._importing = False
        self._import_time: datetime.datetime | None = None
        self._import_shown: set[int] = set()  # log ids already in the table during an import
        self._import_log_ids: list[int] = []
        self._voice_tab_asked = False  # voice_tab_requested was emitted in this run
        # HADES FSK "Fast" decode of a recording file (no SDR pipeline): the worker thread,
        # the recording's start time and whether that time is known (read from its file name).
        self._fast_job: Any | None = None
        # Estimated time of the gr-satellites frame being added by a fast decode (see
        # comms.telemetry.gr_satellites_file); None otherwise.
        self._gr_fast_when: datetime.datetime | None = None
        self._fast_start: datetime.datetime = datetime.datetime.now(datetime.UTC)
        self._fast_time_known = False
        # time of the frame a coherent MSK fast decode is adding (exact, unlike _gr_fast_when)
        self._fast_frame_when: datetime.datetime | None = None

        # CW TLM mode: the CW Decoder tab feeding this one (attach_cw_tab()) and the
        # satellite whose frames are being collected while it runs.
        self._cw_tab: Any = None
        self._cw_tlm_norad: int | None = None
        # True once the "recording start time not set" upload notice was shown this run.
        self._warned_time_unconfirmed = False
        # telemetry_log ids handed to the uploader that SatNOGS has not answered yet, and
        # whether a failure of an AX.25 upload was already shown this run.
        self._upload_pending: set[int] = set()
        self._warned_upload_failure = False
        self._upload_result.connect(self._on_upload_result)
        self._ax25_send_result.connect(self._on_ax25_send_result)

        self._frame_count = 0
        self._direwolf_log_window: _ProcessLogDialog | None = None
        self._gr_log_window: _ProcessLogDialog | None = None

        # Off while the tab is being built: the mode it restores or is forced into must not be
        # saved as the user's own choice.
        self._persist_mode = False
        self._ensure_db_table()
        self._setup_ui()
        self._load_baud_mode()
        self._connect_signals()
        self._populate_afsk_combo()
        self._populate_cw_combo()
        self._populate_hades_combo()
        if detect_gr_satellites():
            self._gr_sat_list = list_gr_satellites_with_names()
            self._populate_gr_combo()
        self._restore_last_satellites()
        self._restore_mode()
        self._load_decode_speed()
        self._update_decode_speed_visibility()
        self._detect_already_connected()
        self._refresh_input_combo()
        self._refresh_status()
        self._persist_mode = True

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
        # Upload tracking (satnogs_uploaded_at, time_reliable), added later.
        ensure_columns(self._conn)
        reset_unconfirmed_marks(self._conn)
        # The whole received frame and the kind of frame, for "Load saved".
        columns = {str(r[1]) for r in self._conn.execute("PRAGMA table_info(telemetry_log)")}
        for column in ("frame_hex", "kind"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE telemetry_log ADD COLUMN {column} TEXT")
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
        self._combo_mode.addItem(_MODE_CW)
        self._combo_mode.addItem(_MODE_HADES)
        self._combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        row1.addWidget(self._combo_mode)
        self._combo_afsk_sat = QComboBox()
        self._combo_afsk_sat.setMinimumWidth(280)
        self._combo_afsk_sat.currentIndexChanged.connect(self._on_afsk_sat_changed)
        row1.addWidget(self._combo_afsk_sat)
        self._btn_afsk_sat_search = QPushButton("🔍")
        self._btn_afsk_sat_search.setToolTip(_("Search satellites…"))
        self._btn_afsk_sat_search.setFixedWidth(28)
        self._btn_afsk_sat_search.clicked.connect(self._on_afsk_sat_search_clicked)
        row1.addWidget(self._btn_afsk_sat_search)
        self._combo_gr_sat = QComboBox()
        self._combo_gr_sat.setMinimumWidth(280)
        self._combo_gr_sat.setVisible(False)
        self._combo_gr_sat.currentIndexChanged.connect(self._on_gr_sat_changed)
        row1.addWidget(self._combo_gr_sat)
        self._btn_gr_sat_search = QPushButton("🔍")
        self._btn_gr_sat_search.setToolTip(_("Search satellites…"))
        self._btn_gr_sat_search.setFixedWidth(28)
        self._btn_gr_sat_search.setVisible(False)
        self._btn_gr_sat_search.clicked.connect(self._on_gr_sat_search_clicked)
        row1.addWidget(self._btn_gr_sat_search)
        self._combo_cw_sat = QComboBox()
        self._combo_cw_sat.setMinimumWidth(280)
        self._combo_cw_sat.setVisible(False)
        self._combo_cw_sat.currentIndexChanged.connect(self._on_cw_sat_changed)
        row1.addWidget(self._combo_cw_sat)
        self._btn_cw_sat_search = QPushButton("🔍")
        self._btn_cw_sat_search.setToolTip(_("Search satellites…"))
        self._btn_cw_sat_search.setFixedWidth(28)
        self._btn_cw_sat_search.setVisible(False)
        self._btn_cw_sat_search.clicked.connect(self._on_cw_sat_search_clicked)
        row1.addWidget(self._btn_cw_sat_search)
        self._combo_hades_sat = QComboBox()
        self._combo_hades_sat.setMinimumWidth(280)
        self._combo_hades_sat.setVisible(False)
        self._combo_hades_sat.currentIndexChanged.connect(self._on_hades_sat_changed)
        row1.addWidget(self._combo_hades_sat)
        self._btn_hades_sat_search = QPushButton("🔍")
        self._btn_hades_sat_search.setToolTip(_("Search satellites…"))
        self._btn_hades_sat_search.setFixedWidth(28)
        self._btn_hades_sat_search.setVisible(False)
        self._btn_hades_sat_search.clicked.connect(self._on_hades_sat_search_clicked)
        row1.addWidget(self._btn_hades_sat_search)

        row1.addSpacing(12)
        self._baud_combo = QComboBox()
        self._baud_combo.addItem("1200", "1200")
        self._baud_combo.addItem("4800", "4800")
        self._baud_combo.addItem("9600", "9600")
        self._baud_combo.setToolTip(
            _(
                "AX.25 baud rate for Direwolf (AX.25) mode's Rig + Sound\n"
                "Card (Direwolf) or SDR reception. Selecting a satellite\n"
                "or transponder sets this automatically from the baud rate\n"
                "in its name (e.g. 1k2 / 9k6); you can still change it by\n"
                "hand. Has no effect on gr-satellites decoding."
            )
        )
        self._baud_combo.currentIndexChanged.connect(self._on_baud_mode_changed)
        self._lbl_baud = QLabel(_("Baud:"))
        row1.addWidget(self._lbl_baud)
        row1.addWidget(self._baud_combo)

        self._btn_backend_log = QPushButton(_("📋 Log"))
        self._btn_backend_log.setToolTip(
            _(
                "Show the active backend's own console output. Direwolf\n"
                "(AX.25) mode: startup messages and a line per decoded\n"
                "AX.25 frame with its own audio-level quality assessment —\n"
                "only populated during SDR reception (Rig + Sound Card\n"
                "sessions play this same output back as audio instead).\n"
                "gr-satellites mode: its stdout/stderr, including errors\n"
                "not shown anywhere else."
            )
        )
        self._btn_backend_log.clicked.connect(self._on_backend_log_clicked)
        row1.addWidget(self._btn_backend_log)

        row1.addStretch()
        input_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self._btn_start = QPushButton(_("▶ Start"))
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop = QPushButton(_("■ Stop"))
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_stop.setEnabled(False)
        self._lbl_status = QLabel(_("—"))
        self._lbl_status.setStyleSheet("color: #aaa;")
        # Without word wrap, a long error string (e.g. a sound-card or
        # gr-satellites exception message) forces QLabel's
        # minimumSizeHint to fit the whole line, widening the window and
        # blocking shrinking it back.
        self._lbl_status.setWordWrap(True)
        row2.addWidget(self._btn_start)
        row2.addWidget(self._btn_stop)
        row2.addWidget(self._lbl_status)
        row2.addStretch()
        self._btn_decode_iq = QPushButton(_("📂 Decode IQ Recording…"))
        self._btn_decode_iq.setToolTip(
            _(
                "Open a recorded .iq.wav file and play it into the decoder. Click the "
                "spectrogram to play from there; decoding starts automatically."
            )
        )
        self._btn_decode_iq.clicked.connect(self._on_decode_iq)
        row2.addWidget(self._btn_decode_iq)
        # HADES FSK mode only: decode the recording at once instead of playing it in real time.
        self._lbl_decode_speed = QLabel(_("Speed:"))
        self._combo_decode_speed = QComboBox()
        self._combo_decode_speed.addItem(_("Normal (real time)"), "normal")
        self._combo_decode_speed.addItem(_("Fast"), "fast")
        self._combo_decode_speed.setToolTip(
            _(
                "Normal: play the recording through the SDR like a live reception and\n"
                "decode it as it goes (pick the start on the spectrogram).\n"
                "Fast: decode the whole recording file at once, in seconds."
            )
        )
        self._combo_decode_speed.currentIndexChanged.connect(self._on_decode_speed_changed)
        row2.addWidget(self._lbl_decode_speed)
        row2.addWidget(self._combo_decode_speed)
        self._lbl_decode_speed.setVisible(False)
        self._combo_decode_speed.setVisible(False)
        row2.addStretch()
        self._lbl_count = QLabel(_("Frames: 0 received"))
        row2.addWidget(self._lbl_count)
        input_layout.addLayout(row2)

        root.addWidget(input_box)

        # --- Receive log ---
        # No title: the inner tabs ("Received Frames" / "Decoded Fields")
        # already label the content, so a group-box title would just repeat
        # the first tab's name.
        log_box = QGroupBox()
        log_layout = QVBoxLayout(log_box)

        self._log_tabs = QTabWidget()
        log_layout.addWidget(self._log_tabs)

        self._raw_page = QWidget()
        raw_page_layout = QVBoxLayout(self._raw_page)
        raw_page_layout.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            [_("Time (UTC)"), _("Callsign"), _("Satellite"), _("Data")]
        )
        hdr = self._table.horizontalHeader()
        # The first three columns are narrow and wrap (the time at its space, a name at
        # its spaces) so the data column gets the width; drag a header edge to change them.
        for column in range(3):
            hdr.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setItemDelegate(_DimRowDelegate(self._table))
        # A frame is shown as hex bytes, which can run to hundreds of characters:
        # wrap it (at the spaces between bytes) and grow the row to fit rather
        # than eliding it.
        self._table.setWordWrap(True)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.itemSelectionChanged.connect(self._on_table_selection_changed)
        # Enlarge the frame-log font ~1.5x to match the APRS Received Packets
        # list; the decoded data column is dense and hard to read at the
        # default size. setFont() on the table also propagates to the header
        # views, so pin them back to the base size explicitly -- only the data
        # rows should grow, not the column titles or row numbers. (Re-applying
        # an unmodified copy of the base font would not work: with an empty
        # resolve mask the header keeps inheriting the scaled table font.)
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
        metrics = self._table.fontMetrics()
        self._table.setColumnWidth(0, metrics.horizontalAdvance("2026-10-19") + 24)
        self._table.setColumnWidth(1, metrics.horizontalAdvance("★ Lobachevsky ") + 24)
        self._table.setColumnWidth(2, metrics.horizontalAdvance("★ Lobachevsky ") + 24)
        raw_page_layout.addWidget(self._table)
        # gr-satellites mode: the full decoded text of the selected row -- one field per
        # line, as gr-satellites prints it -- is the last sub-tab of "Decoded Fields" (the only
        # one for a satellite without a format file; see _rebuild_decode_tabs()).
        self._gr_detail = QPlainTextEdit()
        self._gr_detail.setReadOnly(True)
        self._gr_detail.setPlaceholderText(_("Select a frame to see all its decoded fields."))
        self._log_tabs.addTab(self._raw_page, _("Received Frames"))

        # "ASCII View": every row of "Received Frames" as the text a satellite log lists
        # (SRC>DEST,DIGI*:payload) -- between the hex and the per-field "Decoded Fields".
        self._ascii_page = QWidget()
        ascii_layout = QVBoxLayout(self._ascii_page)
        ascii_layout.setContentsMargins(0, 0, 0, 0)
        self._ascii_table = QTableWidget(0, 4)
        self._ascii_table.setHorizontalHeaderLabels(
            [_("Time (UTC)"), _("Callsign"), _("Satellite"), _("ASCII")]
        )
        ascii_header = self._ascii_table.horizontalHeader()
        for column in range(3):
            ascii_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self._ascii_table.setColumnWidth(column, self._table.columnWidth(column))
        ascii_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._ascii_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._ascii_table.setItemDelegate(_DimRowDelegate(self._ascii_table))
        self._ascii_table.setWordWrap(True)
        self._ascii_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._ascii_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._ascii_table.itemSelectionChanged.connect(self._on_ascii_selection_changed)
        self._ascii_table.setFont(self._table.font())  # the same enlarged size as the hex table
        self._ascii_table.horizontalHeader().setFont(self._table.horizontalHeader().font())
        self._ascii_table.verticalHeader().setFont(self._table.verticalHeader().font())
        QShortcut(QKeySequence.StandardKey.Copy, self._ascii_table).activated.connect(
            self._copy_ascii_rows
        )
        ascii_layout.addWidget(self._ascii_table)
        self._log_tabs.addTab(self._ascii_page, _("ASCII View"))
        # Word-wrapped rows are measured with the column width of that moment. A table that has
        # not been shown yet still has its narrow start-up widths, so its rows came out many lines
        # tall and stayed so after the columns were stretched (seen on Linux the first time the
        # ASCII View was opened). So the rows are measured again the moment a column is resized
        # -- before the table is painted, so the stale heights are never seen -- and once more
        # shortly after a page switch as a backstop. A long table is measured after a short
        # pause instead, so dragging the window edge stays smooth.
        self._row_fits = {"raw": 0, "ascii": 0}  # how often each table's rows were measured
        self._fit_rows_timer = QTimer(self)
        self._fit_rows_timer.setSingleShot(True)
        self._fit_rows_timer.setInterval(30)
        self._fit_rows_timer.timeout.connect(lambda: self._fit_visible_rows(force=True))
        self._log_tabs.currentChanged.connect(lambda _i: self._on_log_page_changed())
        for header in (self._table.horizontalHeader(), self._ascii_table.horizontalHeader()):
            header.sectionResized.connect(lambda *_a: self._fit_visible_rows())

        # "Decoded Fields" page: one inner sub-tab per telemetry ID, built
        # from the satellite's telemetry_ids format definition (if any) and
        # updated live as matching frames arrive. Only satellites with the
        # newer per-ID schema get this tab enabled — see
        # get_telemetry_id_defs().
        self._decode_page = QWidget()
        decode_page_layout = QVBoxLayout(self._decode_page)
        decode_page_layout.setContentsMargins(0, 0, 0, 0)
        # Says so when a sub-tab shows an older frame than the newest of its type.
        self._decode_banner = QLabel("")
        self._decode_banner.setStyleSheet("color: #e67e22;")
        self._decode_banner.setVisible(False)
        decode_page_layout.addWidget(self._decode_banner)
        self._decode_id_tabs = _DecodeTabs()
        self._decode_id_tabs.currentChanged.connect(self._refresh_decode_banner)
        decode_page_layout.addWidget(self._decode_id_tabs)
        self._log_tabs.addTab(self._decode_page, _("Decoded Fields"))
        self._decode_tables: dict[str, QTableWidget] = {}
        self._decode_field_rows: dict[str, dict[str, int]] = {}
        self._decode_tabs_norad: int | None = None
        # Links between the three views: the frame type ("norad:id") of each decoded row, the
        # newest row of each type (drawn bold), and the row each decode sub-tab shows.
        self._row_type: dict[int, str] = {}
        self._latest_row: dict[str, int] = {}
        self._shown_row: dict[str, int] = {}
        self._syncing_selection = False
        self._rebuild_decode_tabs(None)

        root.addWidget(log_box, 1)

        # --- Footer ---
        # Left cluster: SatNOGS DB upload toggle + API-key popup + a link to
        # the selected satellite's SatNOGS page. Right cluster: Load saved / Import CSV / Clear.
        # Kept to a single row (no extra height) at the user's request.
        footer = QHBoxLayout()

        self._btn_satnogs_toggle = QPushButton()
        self._btn_satnogs_toggle.setCheckable(True)
        self._btn_satnogs_toggle.setToolTip(
            _(
                "Upload every decoded frame to the SatNOGS DB (SiDS).\n"
                "Needs a SatNOGS DB API key (API button) plus your callsign\n"
                "and station location (File → Set QTH)."
            )
        )
        self._btn_satnogs_toggle.toggled.connect(self._on_satnogs_toggled)
        footer.addWidget(self._btn_satnogs_toggle)

        self._btn_satnogs_api = QPushButton(_("API"))
        self._btn_satnogs_api.setToolTip(_("Enter your SatNOGS DB API key"))
        self._btn_satnogs_api.clicked.connect(self._on_satnogs_api)
        footer.addWidget(self._btn_satnogs_api)

        self._btn_satnogs_link = QPushButton(_("SatNOGS ↗"))
        self._btn_satnogs_link.setToolTip(_("Open the selected satellite's page on db.satnogs.org"))
        self._btn_satnogs_link.clicked.connect(self._on_open_satnogs)
        footer.addWidget(self._btn_satnogs_link)

        # Send frames by hand (every mode).
        self._btn_satnogs_send = QPushButton(_("Send selected"))
        self._btn_satnogs_send.setToolTip(
            _(
                "Send the frames selected in the table to the SatNOGS DB now,\n"
                "even if the Upload switch is off (CW: even if received only once).\n"
                "Frames without a reliable time or already sent are skipped."
            )
        )
        self._btn_satnogs_send.clicked.connect(self._on_send_selected)
        self._btn_satnogs_send.setVisible(True)
        footer.addWidget(self._btn_satnogs_send)

        footer.addStretch()

        self._btn_load = QPushButton(_("Load saved"))
        self._btn_load.setToolTip(
            _(
                "Show the frames saved earlier for the selected satellite again\n"
                "(the current mode's: AX.25, gr-satellites or CW TLM),\n"
                "without replaying a recording."
            )
        )
        self._btn_load.clicked.connect(self._on_load_saved)
        footer.addWidget(self._btn_load)
        self._btn_import = QPushButton(_("Import CSV…"))
        self._btn_import.setToolTip(
            _(
                "Add the frames of a CSV downloaded from SatNOGS DB (the satellite page's\n"
                "frame download) to the table and the log, for the selected satellite.\n"
                "AX.25 and HADES FSK modes; frames are never sent back to SatNOGS."
            )
        )
        self._btn_import.clicked.connect(self._on_import_csv)
        footer.addWidget(self._btn_import)
        self._btn_clear = QPushButton(_("Clear"))
        self._btn_clear.clicked.connect(self._on_clear)
        footer.addWidget(self._btn_clear)
        root.addLayout(footer)

        self._refresh_satnogs_toggle()
        self._update_satnogs_link_enabled()

    # ------------------------------------------------------------------ #
    # Public API — called by main_window when satellite selection changes
    # ------------------------------------------------------------------ #

    def set_satellite(self, norad: int | None, name: str) -> None:
        """Update the currently tracked satellite.

        The satellite name itself is already shown in the Satellite Detail
        panel next to this tab, so this method's only visible effect is
        auto-selecting the matching entry in the active mode's satellite
        combo (if supported) and rebuilding the "Decoded Fields" sub-tabs
        for the new satellite's telemetry_ids format, if it has one.
        """
        # The Received Frames table belongs to one satellite: showing another one starts it
        # afresh, so frames of two satellites are never listed together. The frames are all
        # in the log; "Load saved" brings them back.
        if norad and norad != self._table_norad:
            if self._table_norad is not None and self._table.rowCount():
                self._on_clear()
            self._table_norad = norad
        self._selected_norad = norad
        self._selected_name = name
        self._rebuild_decode_tabs(norad)
        if norad:
            for kind, combo in self._sat_combos():
                for i in range(combo.count()):
                    if combo.itemData(i) == norad:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(i)
                        combo.blockSignals(False)
                        self._save_last_satellite(kind, norad)
                        break
        self._refresh_input_combo()
        self._update_satnogs_link_enabled()

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

    def _on_decode_iq(self) -> None:
        """Open an IQ recording as SDR Control's "Open…" does; Start on the spectrogram click.

        HADES FSK / gr-satellites mode with Speed "Fast": ask for a file and decode all of it at
        once instead.
        """
        if (
            self._combo_decode_speed.isVisibleTo(self)
            and self._combo_decode_speed.currentData() == "fast"
        ):
            self._on_fast_decode()
            return
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        if sdr_ctrl is None or not hasattr(sdr_ctrl, "open_recording"):
            QMessageBox.information(
                self, _("Decode IQ Recording"), _("SDR Control is not available.")
            )
            return
        self._start_on_iq_seek = True
        with contextlib.suppress(RuntimeError, TypeError):
            sdr_ctrl.overview_seek_done.disconnect(self._on_iq_seek_done)
        sdr_ctrl.overview_seek_done.connect(self._on_iq_seek_done)
        sdr_ctrl.open_recording()

    def _on_iq_seek_done(self, _position_s: float) -> None:
        """The user clicked the recording overview: start decoding if not running yet."""
        if not self._start_on_iq_seek:
            return
        self._start_on_iq_seek = False
        if self._btn_start.isEnabled():
            self._on_start()

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
        old_pipeline = self._sdr_pipeline
        if rig1 is not None and getattr(rig1, "is_sdr", False):
            self._sdr_connected = True
            self._sdr_pipeline = getattr(rig1, "_pipeline", None)
        else:
            self._rig_connected = True
        self._refresh_input_combo()
        self._restart_on_new_pipeline(old_pipeline)
        self._resume_pending_start()
        self._refresh_status()

    def _restart_on_new_pipeline(self, old_pipeline: object | None) -> None:
        """Move a running SDR session onto a replaced pipeline.

        Playing an IQ recording (or reconnecting the SDR) swaps the slot's
        pipeline without a disconnect signal, leaving a running decoder
        subscribed to the dead one — it would receive nothing.
        """
        new_pipeline = self._sdr_pipeline
        if new_pipeline is None or new_pipeline is old_pipeline:
            return
        if (
            self._afsk_source != "sdr_direwolf"
            and not self._gr_backend.is_running
            and self._hades_demod is None
        ):
            return
        self._on_stop()
        self._on_start()

    def _resume_pending_start(self) -> None:
        """Finish a Start that was waiting for the SDR to connect."""
        if not self._pending_start:
            return
        self._pending_start = False
        if time.monotonic() - self._pending_start_at > _PENDING_START_TIMEOUT_S:
            return
        if not (self._sdr_connected and self._sdr_pipeline is not None):
            return
        self._resuming_start = True
        try:
            self._on_start()
        finally:
            self._resuming_start = False

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
        old_pipeline = self._sdr_pipeline
        if rig2 is not None and getattr(rig2, "is_sdr", False):
            self._sdr_connected = True
            self._sdr_pipeline = getattr(rig2, "_pipeline", None)
        else:
            self._rig_connected = True
        self._refresh_input_combo()
        self._restart_on_new_pipeline(old_pipeline)
        self._resume_pending_start()
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

    def _sat_combos(self) -> list[tuple[str, QComboBox]]:
        """Return (mode key, satellite combo) pairs; keys match satellite_selected."""
        return [
            ("afsk", self._combo_afsk_sat),
            ("gr", self._combo_gr_sat),
            ("cw_tlm", self._combo_cw_sat),
            ("hades", self._combo_hades_sat),
        ]

    def _save_last_satellite(self, kind: str, norad: int) -> None:
        """Remember the satellite picked for ``kind`` so the next launch restores it."""
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_LAST_SAT_KEY_PREFIX + kind, str(int(norad))),
            )
            self._conn.commit()

    def _restore_mode(self) -> None:
        """Put the mode combo back to the mode the tab was left in (if it is still offered)."""
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_MODE_SETTING_KEY,)
            ).fetchone()
            idx = self._combo_mode.findText(str(row["value"])) if row else -1
            model = self._combo_mode.model()
            item = model.item(idx) if isinstance(model, QStandardItemModel) and idx >= 0 else None
            if idx > 0 and (item is None or item.isEnabled()):
                self._combo_mode.setCurrentIndex(idx)  # runs _on_mode_changed()

    def _save_mode(self, mode: str) -> None:
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_MODE_SETTING_KEY, mode),
            )
            self._conn.commit()

    def _restore_last_satellites(self) -> None:
        """Re-select the last satellite of each mode (silently), then announce the active one."""
        if not hasattr(self._conn, "execute"):
            return
        for kind, combo in self._sat_combos():
            with contextlib.suppress(Exception):
                row = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (_LAST_SAT_KEY_PREFIX + kind,),
                ).fetchone()
                if not row:
                    continue
                idx = combo.findData(int(row["value"]))
                if idx >= 0:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
        # Let MainWindow (which connects satellite_selected after construction)
        # follow the restored pick, as if the user had chosen it.
        QTimer.singleShot(0, self._announce_restored_satellite)

    def _announce_restored_satellite(self) -> None:
        mode = self._current_mode()
        active = (
            "gr"
            if mode == _MODE_GR
            else "cw_tlm"
            if mode == _MODE_CW
            else "hades"
            if mode == _MODE_HADES
            else "afsk"
        )
        for kind, combo in self._sat_combos():
            norad = combo.currentData()
            if kind == active and norad is not None:
                self.satellite_selected.emit(int(norad), kind)

    def _populate_afsk_combo(self) -> None:
        """Fill the AFSK/Direwolf satellite combo from this app's own DB.

        Lists every satellite carrying a live transmitter this tab can
        actually decode — 1200 baud (Bell 202 AFSK) or 4800/9600 baud
        (G3RUH-style scrambled FSK/GMSK) — via
        mode_detection.is_ax25_telemetry_transmitter(), whose DB join
        already excludes hidden/decayed satellites and dead transmitters.
        A received frame is still decoded with named fields when a
        matching src/data/telemetry_formats/{norad}.json happens to exist
        (see comms.telemetry.decoder.decode_telemetry()) — that's
        unaffected by this method, which only decides what's offered here.

        Previously this also merged in every telemetry_formats/*.json
        satellite unconditionally, even ones with no satellites/
        transmitters DB row at all — e.g. GOLF-TEE (AO-109, 47783.json)
        has no SATNOGS transmitter registration and no TLE anywhere, so
        picking it silently did nothing (no satellite to select, no
        transmitters to show). Cross-checking confirmed only 1 of those 6
        hand-written definitions (LilacSat-2, 40908) had a matching live
        transmitter here anyway, so the two-source merge wasn't worth the
        selectable-but-broken entries it produced (2026-09-05 decision).
        """
        self._combo_afsk_sat.blockSignals(True)
        self._combo_afsk_sat.clear()

        entries: dict[int, str] = {}
        if hasattr(self._conn, "execute"):
            with contextlib.suppress(Exception):
                from comms.mode_detection import get_norads_for_tab

                norads = get_norads_for_tab(self._conn, "telemetry")
                if norads:
                    placeholders = ",".join("?" * len(norads))
                    rows = self._conn.execute(
                        f"SELECT norad_cat_id, name FROM satellites "
                        f"WHERE norad_cat_id IN ({placeholders})",
                        tuple(norads),
                    ).fetchall()
                    for row in rows:
                        entries[int(row["norad_cat_id"])] = str(row["name"])

        for norad, name in sorted(entries.items(), key=lambda kv: kv[1].upper()):
            self._combo_afsk_sat.addItem(f"{name}  ({norad})", userData=norad)
        self._combo_afsk_sat.blockSignals(False)

    def _populate_cw_combo(self) -> None:
        """Fill the CW TLM satellite combo by searching this app's own DB.

        Lists every visible satellite with an alive CW transmitter whose SATNOGS
        description mentions telemetry ("CW TLM", "TLM CW", "CW Telemetry", ...)
        or that this app has a CW frame definition for (mode_detection.
        is_cw_telemetry_transmitter(); ARICA-2's entry is just "Mode U - CW", so
        the definition is what finds it). Satellites that can actually be decoded
        (a ``cw_frames`` format exists) come first; the others are listed too
        because the DB says they carry CW telemetry, but ▶ Start explains that
        no frame format is known for them yet.
        """
        self._combo_cw_sat.blockSignals(True)
        self._combo_cw_sat.clear()

        entries: dict[int, str] = {}
        if hasattr(self._conn, "execute"):
            with contextlib.suppress(Exception):
                from comms.mode_detection import get_norads_matching, is_cw_telemetry_transmitter

                norads = get_norads_matching(self._conn, is_cw_telemetry_transmitter)
                if norads:
                    placeholders = ",".join("?" * len(norads))
                    rows = self._conn.execute(
                        f"SELECT norad_cat_id, name FROM satellites "
                        f"WHERE norad_cat_id IN ({placeholders})",
                        tuple(norads),
                    ).fetchall()
                    for row in rows:
                        entries[int(row["norad_cat_id"])] = str(row["name"])

        def sort_key(item: tuple[int, str]) -> tuple[int, str]:
            norad, name = item
            return (0 if load_cw_frames(norad) else 1, name.upper())

        for norad, name in sorted(entries.items(), key=sort_key):
            self._combo_cw_sat.addItem(f"{name}  ({norad})", userData=norad)
        self._combo_cw_sat.blockSignals(False)

    def _populate_hades_combo(self) -> None:
        """Fill the HADES FSK satellite combo: satellites with a 200 baud FSK transmitter.

        Found by mode_detection.is_hades_fsk_transmitter() in this app's own DB, so a
        satellite appears only once its SatNOGS transmitters are synced (the same rule as
        the other combos, which avoids entries that select nothing).
        """
        self._combo_hades_sat.blockSignals(True)
        self._combo_hades_sat.clear()
        entries: dict[int, str] = {}
        if hasattr(self._conn, "execute"):
            with contextlib.suppress(Exception):
                from comms.mode_detection import get_norads_matching, is_hades_fsk_transmitter

                norads = get_norads_matching(self._conn, is_hades_fsk_transmitter)
                if norads:
                    placeholders = ",".join("?" * len(norads))
                    rows = self._conn.execute(
                        f"SELECT norad_cat_id, name FROM satellites "
                        f"WHERE norad_cat_id IN ({placeholders})",
                        tuple(norads),
                    ).fetchall()
                    for row in rows:
                        entries[int(row["norad_cat_id"])] = str(row["name"])
        for norad, name in sorted(entries.items(), key=lambda kv: kv[1].upper()):
            self._combo_hades_sat.addItem(f"{name}  ({norad})", userData=norad)
        self._combo_hades_sat.blockSignals(False)

    def _hidden_norads(self) -> set[int]:
        """NORAD ids satellites.is_hidden marks as no longer tracked.

        Used by _populate_gr_combo() so a satellite this app's own
        SATNOGS/CelesTrak-driven tracking has flagged as decayed/removed
        doesn't linger there — gr-satellites' own bundled YAML catalog is
        static and never gets cleaned up on its own. (_populate_afsk_combo()
        no longer needs this: it now draws directly from this app's DB via
        mode_detection.get_norads_for_tab(), whose join already excludes
        hidden satellites.) Fails open (empty set) if the query itself fails.
        """
        if not hasattr(self._conn, "execute"):
            return set()
        with contextlib.suppress(Exception):
            return {
                int(row["norad_cat_id"])
                for row in self._conn.execute(
                    "SELECT norad_cat_id FROM satellites WHERE is_hidden != 0"
                ).fetchall()
            }
        return set()

    def _norads_with_live_transmitter(self) -> set[int]:
        """NORAD ids carrying at least one alive transmitter in this app's own DB.

        Used by _populate_gr_combo() to filter out gr-satellites catalog
        entries this app has no actual frequency data for — its 400+
        satellite YAML bundle includes many satellites we've simply never
        synced (no satellites/transmitters row at all) or that currently
        have zero live SATNOGS transmitter registrations. Picking one of
        those previously did nothing: _on_telemetry_satellite_requested()'s
        gr-mode branch finds RadioControlWidget's transmitter list empty
        and returns before touching anything (same "ghost entry" class of
        bug as _populate_afsk_combo(), 2026-09-05). Checked against this
        app's live `transmitters` table every time this tab is opened (a
        non-resident tab, rebuilt from scratch on each open), so it
        self-corrects as the existing 7-day SATNOGS transmitter sync job
        runs — no separate scheduled job needed here. Fails open (empty
        set) if the query itself fails.
        """
        if not hasattr(self._conn, "execute"):
            return set()
        with contextlib.suppress(Exception):
            return {
                int(row["norad_cat_id"])
                for row in self._conn.execute(
                    "SELECT DISTINCT norad_cat_id FROM transmitters WHERE alive = 1"
                ).fetchall()
            }
        return set()

    def _live_satellite_names(self) -> list[tuple[int, str]]:
        """(norad, name) of non-hidden satellites with an alive transmitter in this DB.

        Used by _populate_gr_combo() to find the real NORAD id of a satellite
        gr-satellites' catalog still lists under a provisional id. Fails open
        (empty list) if the query itself fails.
        """
        if not hasattr(self._conn, "execute"):
            return []
        with contextlib.suppress(Exception):
            return [
                (int(row["norad_cat_id"]), str(row["name"]))
                for row in self._conn.execute(
                    "SELECT norad_cat_id, name FROM satellites WHERE is_hidden = 0 "
                    "AND norad_cat_id IN (SELECT norad_cat_id FROM transmitters WHERE alive = 1)"
                ).fetchall()
            ]
        return []

    def _populate_gr_combo(self) -> None:
        """Fill the gr-satellites satellite combo from the loaded list.

        Excludes:
          - Satellites our own tracking has confirmed are no longer valid
            (satellites.is_hidden != 0) — gr-satellites' own YAML catalog
            is independent of our SATNOGS/CelesTrak-driven tracking and
            never gets pruned as satellites decay, so without this a
            satellite already flagged hidden elsewhere in the app could
            still show up here.
          - Satellites with no live transmitter in this app's own DB (see
            _norads_with_live_transmitter()) — selecting one would be a
            silent no-op, same as the telemetry_formats-only "ghost"
            entries _populate_afsk_combo() used to show.

        A catalog entry still filed under a SATNOGS provisional id (>= 90000)
        after this app migrated the satellite to its real NORAD id (e.g.
        Foresail-1p: catalog 98467, DB 66778) is matched by name (see
        map_provisional_to_tracked()) and listed under the real id, so the
        combo, the satellite list and Radio Control all agree. The catalog id
        is kept in self._gr_catalog_ids because gr_satellites must still be
        launched with it.
        """
        self._combo_gr_sat.clear()
        self._gr_catalog_ids = {}
        hidden = self._hidden_norads()
        tracked = self._norads_with_live_transmitter()
        renamed = map_provisional_to_tracked(
            [(n, name) for n, name in self._gr_sat_list if n not in tracked],
            self._live_satellite_names(),
        )
        shown: set[int] = set()
        for catalog_norad, name in self._gr_sat_list:
            # The hidden check must follow the remap: a migrated satellite keeps
            # a hidden row under its old provisional id, which says nothing
            # about the live row under the real id.
            if catalog_norad in tracked:
                norad = catalog_norad
            elif catalog_norad in renamed:
                norad = renamed[catalog_norad]
            else:
                continue
            if norad in hidden or norad in shown:
                continue
            shown.add(norad)
            if norad != catalog_norad:
                self._gr_catalog_ids[norad] = catalog_norad
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
        self._pending_start = False
        # The table lists one kind of frame (AX.25, gr-satellites or CW): a different mode
        # starts it afresh. The frames are in the log; "Load saved" brings them back.
        if self._table.rowCount():
            self._on_clear()
        mode = self._current_mode()
        is_gr = mode == _MODE_GR
        is_cw = mode == _MODE_CW
        is_hades = mode == _MODE_HADES
        is_afsk = not is_gr and not is_cw and not is_hades
        self._combo_afsk_sat.setVisible(is_afsk)
        self._btn_afsk_sat_search.setVisible(is_afsk)
        self._combo_gr_sat.setVisible(is_gr)
        self._btn_gr_sat_search.setVisible(is_gr)
        self._combo_cw_sat.setVisible(is_cw)
        self._btn_cw_sat_search.setVisible(is_cw)
        self._combo_hades_sat.setVisible(is_hades)
        self._btn_hades_sat_search.setVisible(is_hades)
        self._update_decode_speed_visibility()
        self._stop_fast_decode()
        # Baud and the backend log are Direwolf / gr-satellites matters; CW TLM has
        # neither (its text is in the CW Decoder tab) and HADES FSK has a fixed baud and
        # no backend process.
        for widget in (self._lbl_baud, self._baud_combo, self._btn_backend_log):
            widget.setVisible(not is_cw and not is_hades)
        self._btn_satnogs_send.setVisible(True)
        # Both sub-tabs exist in every mode. In gr-satellites mode "Decoded Fields" shows this
        # project's own format file for the satellite when it has one, plus gr-satellites'
        # own full text of the selected frame.
        self._rebuild_decode_tabs(self._selected_norad)
        self._log_tabs.setCurrentWidget(self._raw_page)
        if is_cw and self._combo_cw_sat.count():
            # Select the combo's satellite the way changing the combo would, so the
            # satellite list, Radio Control and the Decoded Fields tabs follow.
            self._on_cw_sat_changed(self._combo_cw_sat.currentIndex())
        if is_hades and self._combo_hades_sat.count():
            self._on_hades_sat_changed(self._combo_hades_sat.currentIndex())
        if self._persist_mode:
            self._save_mode(mode)
        # SatNOGS DB upload now covers both paths (Phase 2 added the
        # gr-satellites --kiss_server raw-frame route; see
        # _on_gr_raw_frame()), so the upload cluster stays visible in
        # gr-satellites mode too.
        self._refresh_status()

    def _on_afsk_sat_changed(self, _index: int) -> None:
        norad = self._combo_afsk_sat.currentData()
        self._update_satnogs_link_enabled()
        if norad is not None:
            self._save_last_satellite("afsk", int(norad))
            self.satellite_selected.emit(int(norad), "afsk")

    def _on_gr_sat_changed(self, _index: int) -> None:
        norad = self._combo_gr_sat.currentData()
        if norad is not None:
            self._save_last_satellite("gr", int(norad))
            self.satellite_selected.emit(int(norad), "gr")
            self._set_baud_from_satyaml(int(norad))

    def _set_baud_from_satyaml(self, norad: int) -> None:
        """Set the Baud combo from the gr-satellites satyaml for ``norad``."""
        info = get_satellite_info(self._gr_catalog_ids.get(norad, norad))
        if info is None:
            return
        names = info.get("transmitters")
        rates = info.get("baudrates")
        self._set_baud_combo(
            detect_baud_from_satyaml(
                names if isinstance(names, list) else [],
                rates if isinstance(rates, list) else [],
            )
        )

    def _on_cw_sat_changed(self, _index: int) -> None:
        norad = self._combo_cw_sat.currentData()
        self._update_satnogs_link_enabled()
        if norad is not None:
            self._save_last_satellite("cw_tlm", int(norad))
            self.satellite_selected.emit(int(norad), "cw_tlm")

    def _on_hades_sat_changed(self, _index: int) -> None:
        norad = self._combo_hades_sat.currentData()
        self._update_satnogs_link_enabled()
        if norad is not None:
            self._save_last_satellite("hades", int(norad))
            self.satellite_selected.emit(int(norad), "hades")

    def _on_hades_sat_search_clicked(self) -> None:
        self._open_sat_search(self._combo_hades_sat)

    def _on_cw_sat_search_clicked(self) -> None:
        self._open_sat_search(self._combo_cw_sat)

    def _on_afsk_sat_search_clicked(self) -> None:
        self._open_sat_search(self._combo_afsk_sat)

    def _on_gr_sat_search_clicked(self) -> None:
        self._open_sat_search(self._combo_gr_sat)

    def _open_sat_search(self, combo: QComboBox) -> None:
        """Open the search dialog for `combo`'s satellite list and apply the pick.

        Reads (norad, name) pairs back out of the already-populated combo
        instead of re-querying, so the search dialog always matches
        exactly what the combo currently offers.
        """
        entries = []
        for i in range(combo.count()):
            norad = int(combo.itemData(i))
            # Item text is "{name}  ({norad})" (see _populate_afsk_combo /
            # _populate_gr_combo); strip that exact suffix so
            # SatSearchDialog's own "(norad)" formatting doesn't double up.
            name = combo.itemText(i).rsplit(f"({norad})", 1)[0].strip()
            entries.append((norad, name))
        if not entries:
            return
        dlg = SatSearchDialog(entries, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected_norad is None:
            return
        idx = combo.findData(dlg.selected_norad)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _current_mode(self) -> str:
        return self._combo_mode.currentText()

    def _update_decode_speed_visibility(self) -> None:
        """Show the Speed choice where a recording can be decoded at once.

        HADES FSK, gr-satellites and CW TLM always; AX.25 at every baud: the coherent MSK
        detector at 4800 / 9600, Direwolf fed with the satellite AFSK front end at 1200.
        """
        mode = self._current_mode()
        visible = mode in (_MODE_GR, _MODE_HADES, _MODE_CW) or (
            mode == _MODE_AFSK and self._baud_combo.currentData() in _FAST_AX25_BAUDS
        )
        self._lbl_decode_speed.setVisible(visible)
        self._combo_decode_speed.setVisible(visible)

    # ------------------------------------------------------------------ #
    # AX.25 baud mode (shared with the APRS tab)
    # ------------------------------------------------------------------ #

    def _load_baud_mode(self) -> None:
        """Restore the 1200/4800/9600 selection from app_settings."""
        mode = "1200"
        if hasattr(self._conn, "execute"):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (_BAUD_SETTING_KEY,),
            ).fetchone()
            if row and row["value"] in BAUD_CHOICES:
                mode = row["value"]
        idx = self._baud_combo.findData(mode)
        self._baud_combo.blockSignals(True)
        self._baud_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._baud_combo.blockSignals(False)

    def _on_baud_mode_changed(self, _index: int) -> None:
        """Persist the 1200/4800/9600 selection and apply it immediately."""
        mode = self._baud_combo.currentData()
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_BAUD_SETTING_KEY, mode),
            )
            self._conn.commit()
        self._update_decode_speed_visibility()
        self._apply_baud_change()

    def _on_backend_log_clicked(self) -> None:
        """Open (or bring forward and refresh) the active backend's console log window.

        Direwolf (AX.25) mode and gr-satellites mode each get their own
        cached window (see _direwolf_log_window / _gr_log_window) so
        switching modes doesn't repoint an already-open dialog out from
        under the user.
        """
        if self._current_mode() == _MODE_GR:
            from comms.telemetry.gr_satellites_log import gr_satellites_log_path

            if self._gr_log_window is None:
                self._gr_log_window = _ProcessLogDialog(
                    _("gr-satellites Log"), gr_satellites_log_path(), self
                )
            else:
                self._gr_log_window.reload()
            window = self._gr_log_window
        else:
            from comms.aprs.direwolf_log import direwolf_log_path

            if self._direwolf_log_window is None:
                self._direwolf_log_window = _ProcessLogDialog(_("Log"), direwolf_log_path(), self)
            else:
                self._direwolf_log_window.reload()
            window = self._direwolf_log_window
        window.show()
        window.raise_()
        window.activateWindow()

    def _current_modem(self) -> str:
        """Return the Direwolf MODEM value ("1200"/"4800"/"9600") from the Baud combo."""
        data = self._baud_combo.currentData()
        return str(data) if data in BAUD_CHOICES else "1200"

    def _set_baud_combo(self, baud: str | None) -> bool:
        """Select ``baud`` in the combo (firing the change handler); True if it changed."""
        if baud is None:
            return False
        idx = self._baud_combo.findData(baud)
        if idx < 0 or idx == self._baud_combo.currentIndex():
            return False
        self._baud_combo.setCurrentIndex(idx)
        return True

    def _on_transmitter_changed(self, xpdr: object) -> None:
        """Follow the selected transponder's baud, then restart AX.25 if it changed.

        gr-satellites mode takes its baud from the satyaml instead (see
        _on_gr_sat_changed()), so the DB transponder is ignored there.
        """
        if (
            self._current_mode() not in (_MODE_GR, _MODE_HADES)
            and isinstance(xpdr, dict)
            and self._set_baud_combo(detect_baud_from_transmitter(xpdr))
        ):
            return  # the combo's change handler already applied it
        self._apply_baud_change()

    def _apply_baud_change(self) -> None:
        """Re-resolve the target baud and apply it to whichever source is active.

        Rig + Sound Card (Direwolf) sessions are restarted in place via
        restart_if_modem_changed(); SDR-fed Direwolf sessions via
        sync_sdr_baud(). Both methods only act on the session type they
        own, so calling both unconditionally is safe — whichever doesn't
        apply is a no-op.
        """
        modem = self._current_modem()
        self._engine.restart_if_modem_changed(modem)
        if self._sdr_pipeline is not None:
            self._engine.sync_sdr_baud(self._sdr_pipeline, modem, satellite=True)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    def _on_start(self) -> None:
        self._warned_time_unconfirmed = False
        self._warned_upload_failure = False
        mode = self._current_mode()
        if mode == _MODE_CW:
            if not self._start_cw_tlm():
                return
        elif mode == _MODE_GR:
            if not self._start_gr_satellites():
                return  # Start stays pressable; the reason is in the status label
        elif mode == _MODE_HADES:
            if not self._start_hades():
                return
        else:
            self._try_start_afsk()
            if self._afsk_source is None:
                return
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

    def _on_stop(self) -> None:
        self._pending_start = False
        self._stop_gr_satellites()
        self._stop_hades()
        self._stop_fast_decode()
        self._stop_engine()
        self._stop_cw_tlm()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # CW TLM (frames cut out of the CW Decoder tab's text)
    # ------------------------------------------------------------------ #

    def attach_cw_tab(self, cw_tab: Any) -> None:
        """Receive the CW Decoder tab's finished text blocks (called by MainWindow)."""
        if cw_tab is self._cw_tab:
            return
        if self._cw_tab is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self._cw_tab.frame_block_ready.disconnect(self._on_cw_block)
                self._cw_tab.fast_decode_progress.disconnect(self._on_cw_fast_progress)
                self._cw_tab.fast_decode_finished.disconnect(self._on_cw_fast_finished)
        self._cw_tab = cw_tab
        cw_tab.frame_block_ready.connect(self._on_cw_block)
        cw_tab.fast_decode_progress.connect(self._on_cw_fast_progress)
        cw_tab.fast_decode_finished.connect(self._on_cw_fast_finished)

    def _start_cw_tlm(self) -> bool:
        """Ask MainWindow to open and start the CW Decoder tab. False if it cannot be done."""
        norad = self._combo_cw_sat.currentData()
        if not isinstance(norad, int):
            self._set_error("⚠ " + _("Select a satellite first."))
            return False
        if load_cw_frames(norad) is None:
            self._set_error(
                "⚠ " + _("No CW telemetry frame format is defined for this satellite yet.")
            )
            return False
        if self._decode_tabs_norad != norad:
            self._rebuild_decode_tabs(norad)
        self._cw_tlm_norad = norad
        self.cw_tlm_start_requested.emit()
        self._lbl_status.setText(_("CW TLM — decoding in the CW Decoder tab"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        return True

    def _start_cw_fast_decode(self, path: Path) -> bool:
        """Decode *path* at once in the CW Decoder tab; False (reason on screen) if it cannot."""
        if self._cw_tlm_norad is not None:
            self._set_error("⚠ " + _("Stop the running decoder first."))
            return False
        norad = self._combo_cw_sat.currentData()
        if not isinstance(norad, int):
            self._set_error("⚠ " + _("Select a satellite first."))
            return False
        if load_cw_frames(norad) is None:
            self._set_error(
                "⚠ " + _("No CW telemetry frame format is defined for this satellite yet.")
            )
            return False
        if self._decode_tabs_norad != norad:
            self._rebuild_decode_tabs(norad)
        self._cw_tlm_norad = norad
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_decode_iq.setEnabled(False)
        self._lbl_status.setText(_("Fast decode: starting…"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        self.cw_tlm_fast_requested.emit(path)
        return True

    def _on_cw_fast_progress(self, fraction: float) -> None:
        if self._cw_tlm_norad is None:
            return
        self._lbl_status.setText(_("Fast decode: {pct}%").format(pct=int(fraction * 100)))

    def _on_cw_fast_finished(self, message: str) -> None:
        """The CW Decoder tab finished (or could not start) a fast decode."""
        self._cw_tlm_norad = None
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_decode_iq.setEnabled(True)
        self._lbl_status.setText(message)
        self._lbl_status.setStyleSheet("color: #27ae60;")

    def _stop_cw_tlm(self) -> None:
        if self._cw_tlm_norad is None:
            return
        # Stopping the CW tab hands over the block in progress, which still needs
        # _cw_tlm_norad -- clear it only afterwards.
        self.cw_tlm_stop_requested.emit()
        self._cw_tlm_norad = None

    def _on_cw_block(self, text: str, start: Any, end: Any) -> None:
        """One finished block of CW text from the CW Decoder tab.

        A block that is exactly a known frame's length of hex digits and passes
        that frame's plausibility checks becomes a received frame (table row,
        Decoded Fields update, log entry). A block that looks like a mis-read
        frame is listed greyed out with a "?" and never used as data -- CW has
        no CRC, so a wrong digit can not be told from a right one otherwise.
        Anything else (the beacon's ID text, noise) is ignored.
        """
        norad = self._cw_tlm_norad
        if norad is None:
            return
        # A block can hold the tail of the previous frame and/or several whole frames.
        pieces = split_frame_blocks(norad, text)
        for position, piece in pieces:
            piece_start = start
            if (
                len(pieces) > 1
                and isinstance(start, datetime.datetime)
                and isinstance(end, datetime.datetime)
            ):
                piece_start = start + (end - start) * position
            self._on_cw_frame(norad, piece, piece_start)

    def _on_cw_frame(self, norad: int, text: str, start: Any) -> None:
        """One frame's worth of CW text (see _on_cw_block())."""
        fmt = load_format(norad) or {}
        callsign = str(fmt.get("callsign", ""))
        sat_name = str(fmt.get("name", f"NORAD {norad}"))
        # A frame is timed by when its transmission *started*: that is how the operator's
        # own station stamps its ARICA-2 frames on the SatNOGS DB (a CW frame lasts
        # ~17 s, so the end would be off by that much from theirs). The same time is shown,
        # logged and uploaded.
        ts = start if isinstance(start, datetime.datetime) else datetime.datetime.now(datetime.UTC)
        reliable_fn = getattr(self._cw_tab, "signal_time_reliable", None)
        reliable = bool(reliable_fn()) if callable(reliable_fn) else True

        # Fill in unreadable digits the frame definition can supply, and check the frame's
        # own time stamp against the receive time -- only when that time is trustworthy.
        when = ts if reliable else None
        text = repair_block(norad, text, when)
        result = decode_cw_frame(norad, text, when)
        if result is None:
            if is_near_miss(norad, text):
                note = _("length or characters not valid, not used")
                self._append_row(
                    callsign=callsign,
                    sat_name=sat_name,
                    data=f"[?] {near_miss_hex(norad, text) or normalize_block(text)}  ({note})",
                    norad=norad,
                    ts=ts,
                    dim=True,
                )
                # Sent to the SatNOGS DB all the same: the operator can tell a wrong frame.
                wire = build_lenient_satnogs_frame(norad, text)
                if wire is not None and load_satnogs_upload_settings(self._conn).get("enabled"):
                    self._submit_raw_frame(wire, norad, ts, reliable)
            else:
                # Not a frame-length hex block and not a near-miss either (e.g. the
                # beacon's callsign/name text, or a block where the name and the hex
                # data never got split apart by the block extractor's pause — see
                # docs/telemetry.md "CW TLM" for why that can happen). Nothing is
                # shown in the table for this case, so log it: it is the only trace
                # of a block that silently produced no telemetry.
                logger.info(
                    "CW block ignored (norad=%s, not a known frame length or near-miss): %r",
                    norad,
                    normalize_block(text),
                )
            return
        tf = TelemetryFrame(
            norad=norad,
            callsign=callsign,
            satellite_name=sat_name,
            raw_hex=result.hex_text,
            fields=result.fields,
            telemetry_id=result.key,
            telemetry_label=result.label,
        )
        log_id = self._persist_frame(tf, ts, reliable, kind="cw")
        if not result.valid:
            # Implausible readings are shown muted but logged and sent like any other.
            self._append_row(
                callsign=callsign,
                sat_name=sat_name,
                data=f"[?] {result.key} {result.hex_text}  ({'; '.join(result.problems)})",
                norad=norad,
                ts=ts,
                dim=True,
                log_id=log_id,
            )
        else:
            self._append_row(
                callsign=callsign,
                sat_name=sat_name,
                data=f"[{result.key}] {result.hex_text}",
                norad=norad,
                ts=ts,
                log_id=log_id,
                decoded=tf,
            )
            self._update_decode_tab(tf)
        if log_id is not None:
            self._auto_send_cw(norad, log_id)

    # ------------------------------------------------------------------ #
    # SatNOGS upload of CW frames (rules: comms.telemetry.cw_upload)
    # ------------------------------------------------------------------ #

    def _auto_send_cw(self, norad: int, log_id: int) -> None:
        """Send a just-logged CW frame if the upload switch is on and a repeat confirms it."""
        if not load_satnogs_upload_settings(self._conn).get("enabled"):
            return
        report = auto_send(
            self._conn,
            get_satnogs_uploader(),
            norad,
            log_id,
            on_result=self._on_cw_frame_result,
            pending=self._upload_pending,
        )
        self._show_send_report(report, automatic=True)

    def _selected_log_ids(self) -> list[int]:
        ids: list[int] = []
        selection = self._table.selectionModel()
        for index in selection.selectedRows() if selection is not None else []:
            item = self._table.item(index.row(), 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(value, int):
                ids.append(value)
        return ids

    def _on_send_selected(self) -> None:
        if self._current_mode() != _MODE_CW:
            self._send_selected_ax25()
            return
        norad = self._active_norad()
        ids = self._selected_log_ids()
        if norad is None or not ids:
            self._set_error(_("Select received CW frames in the table first."))
            return
        report = send_frames(
            self._conn,
            get_satnogs_uploader(),
            norad,
            ids,
            force=True,
            require_repeat=False,
            on_result=self._on_cw_frame_result,
            pending=self._upload_pending,
        )
        self._show_send_report(report)

    def _selected_ax25_rows(self) -> list[_Ax25Row]:
        rows: list[_Ax25Row] = []
        selection = self._table.selectionModel()
        for index in selection.selectedRows() if selection is not None else []:
            item = self._table.item(index.row(), 0)
            value = item.data(_AX25_ROLE) if item is not None else None
            if isinstance(value, _Ax25Row):
                rows.append(value)
        return rows

    def _satellite_label(self, norad: int) -> str:
        name = ""
        if hasattr(self._conn, "execute"):
            row = self._conn.execute(
                "SELECT name FROM satellites WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            name = str(row[0]) if row else ""
        return f"{name} (NORAD {norad})" if name else f"NORAD {norad}"

    def _send_selected_ax25(self) -> None:
        """Send the selected AX.25 frames to the SatNOGS DB by hand.

        A frame whose satellite could not be worked out from its callsign is
        filed under the satellite selected in this tab; the user confirms the
        target first, since the SatNOGS DB is public.
        """
        rows = self._selected_ax25_rows()
        if not rows:
            self._set_error(_("Select received AX.25 frames in the table first."))
            return
        report = SendReport(blocker=upload_blocker(self._conn, force=True))
        if report.blocker is not None:
            self._show_send_report(report)
            return
        fallback = self._active_norad()
        todo: list[tuple[_Ax25Row, int]] = []
        for row in rows:
            if row.sent:
                report.duplicates += 1
            elif not row.raw:
                report.unsupported += 1  # loaded from an old log: payload only
            elif row.pending:
                report.pending += 1
            elif not row.reliable:
                report.unreliable += 1
            else:
                norad = row.norad if row.norad is not None else fallback
                if norad is None:
                    self._set_error(_("Select the satellite these frames came from first."))
                    return
                todo.append((row, norad))
        if todo:
            counts: dict[int, int] = {}
            for _row, norad in todo:
                counts[norad] = counts.get(norad, 0) + 1
            lines = "\n".join(
                f"{self._satellite_label(norad)}: {n}" for norad, n in sorted(counts.items())
            )
            answer = QMessageBox.question(
                self,
                _("Send to SatNOGS"),
                _("Send {n} frame(s) to the SatNOGS DB?\n\n{lines}").format(
                    n=len(todo), lines=lines
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        uploader = get_satnogs_uploader()
        for row, norad in todo:
            row.manual = True
            row.pending = True

            def _result(accepted: bool, status: int, body: str, r: _Ax25Row = row) -> None:
                self._ax25_send_result.emit(r, accepted, status, body)

            if uploader.submit(self._conn, row.raw, norad, row.when, force=True, on_result=_result):
                report.queued += 1
            else:
                row.pending = False
        self._show_send_report(report)

    def _on_ax25_send_result(self, row: object, accepted: bool, status: int, body: str) -> None:
        """GUI thread: SatNOGS answered the upload of one AX.25 frame."""
        if not isinstance(row, _Ax25Row):
            return
        row.pending = False
        if accepted:
            row.sent = True
            if row.log_id is not None:
                mark_uploaded(self._conn, row.log_id)
            if row.manual:
                self._lbl_status.setText(
                    _("SatNOGS accepted the frame (HTTP {n})").format(n=status)
                )
                self._lbl_status.setStyleSheet("color: #27ae60;")
            return
        if row.manual:
            self._set_error(self._upload_failure_text(status, body))
        else:
            self._on_upload_result(None, False, status, body)

    def _on_cw_frame_result(self, frame_id: int, accepted: bool, status: int, body: str) -> None:
        """Uploader thread: SatNOGS answered the upload of CW frame *frame_id*."""
        self._upload_result.emit(frame_id, accepted, status, body)

    def _on_upload_result(self, frame_id: object, accepted: bool, status: int, body: str) -> None:
        """GUI thread: show SatNOGS's answer to an upload.

        A CW frame is marked as sent only when SatNOGS accepted it; a rejected
        one (e.g. HTTP 401 for a wrong API key) stays unsent, can be sent again,
        and the reason is shown. An AX.25 upload reports failures only (once per run).
        """
        if isinstance(frame_id, int):
            self._upload_pending.discard(frame_id)
            if accepted:
                mark_uploaded(self._conn, frame_id)
        if accepted:
            if isinstance(frame_id, int):
                self._lbl_status.setText(
                    _("SatNOGS accepted the frame (HTTP {n})").format(n=status)
                )
                self._lbl_status.setStyleSheet("color: #27ae60;")
            return
        if frame_id is None:
            if self._warned_upload_failure:
                return
            self._warned_upload_failure = True
        self._set_error(self._upload_failure_text(status, body))

    @staticmethod
    def _upload_failure_text(status: int, body: str) -> str:
        """User-facing text for a failed SatNOGS upload (401/403 mean a bad API key)."""
        detail = body.strip()
        with contextlib.suppress(ValueError, TypeError):
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("detail"):
                detail = str(parsed["detail"])
        detail = detail[:200]
        if status == 0:
            return "⚠ " + _("SatNOGS upload failed: {detail}").format(detail=detail)
        text = _("SatNOGS rejected the upload (HTTP {status}): {detail}").format(
            status=status, detail=detail
        )
        if status in (401, 403):
            text += " — " + _("check the API key (API button)")
        return "⚠ " + text

    def _show_send_report(self, report: SendReport, automatic: bool = False) -> None:
        """Say what a send did in the status label. An automatic upload stays quiet
        unless something was sent or the user needs to act."""
        if report.blocker is not None:
            missing = {
                "no_api_key": _("API key"),
                "no_callsign": _("callsign"),
                "no_location": _("station location"),
            }.get(report.blocker)
            if missing is not None:
                self._set_error(_("SatNOGS upload not possible — missing: ") + missing)
            return
        if automatic and not report.queued and not report.unreliable and not report.pending:
            return
        parts: list[str] = []
        if report.queued:
            parts.append(_("{n} queued for upload").format(n=report.queued))
        if report.pending:
            parts.append(_("{n} awaiting SatNOGS's reply").format(n=report.pending))
        if report.duplicates:
            parts.append(_("{n} already sent").format(n=report.duplicates))
        if report.waiting:
            parts.append(_("{n} waiting for a second reception").format(n=report.waiting))
        if report.unreliable:
            parts.append(_("{n} skipped: recording start time not set").format(n=report.unreliable))
        if report.unsupported:
            parts.append(_("{n} without a SatNOGS format").format(n=report.unsupported))
        text = "SatNOGS: " + (", ".join(parts) if parts else _("nothing to send"))
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(
            "color: #27ae60;"
            if report.queued
            else "color: #e67e22;"
            if report.unreliable
            else "color: #aaa;"
        )

    # ------------------------------------------------------------------ #
    # gr-satellites lifecycle
    # ------------------------------------------------------------------ #

    def _start_gr_satellites(self) -> bool:
        """Start gr-satellites; False (with the reason on screen) if it did not start."""
        norad = self._combo_gr_sat.currentData()
        if norad is None:
            self._set_error(_("⚠ No satellite selected"))
            return False

        pipeline = self._sdr_pipeline
        if pipeline is None:
            pipeline = self._auto_connect_sdr()
            if pipeline is None:
                return False

        try:
            samp_rate = int(pipeline._device.sample_rate)  # type: ignore[attr-defined]
        except AttributeError:
            samp_rate = 2_400_000

        ok, err = self._gr_backend.start(
            norad, samp_rate, pipeline, catalog_norad=self._gr_catalog_ids.get(int(norad))
        )
        if not ok:
            self._set_error(f"⚠ {err}")
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
            return False
        return True

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
            if self._resuming_start:
                # Connected but no pipeline: connecting again would only toggle it.
                self._set_error(_("⚠ SDR connected but not ready"))
                return None
            # Delegate to Radio Control's connect button handler so the UI
            # stays consistent (button state, status label, signals, etc.)
            self._pending_start = True
            self._pending_start_at = time.monotonic()
            self._lbl_status.setText(_("Connecting SDR…"))
            connect_fn = getattr(
                rc, "_on_connect_rig1" if attr == "_rig1" else "_on_connect_rig2", None
            )
            if connect_fn is not None:
                connect_fn()
            self._lbl_status.setText(_("Connecting SDR… decoding starts once it is connected"))
            self._lbl_status.setStyleSheet("color: #f39c12;")
            return None
        self._set_error(_("⚠ No SDR or rig connected"))
        return None

    def _stop_gr_satellites(self) -> None:
        if self._gr_backend.is_running:
            self._gr_backend.stop()

    # ------------------------------------------------------------------ #
    # HADES FSK (200 baud 2FSK bursts demodulated from the SDR's I/Q)
    # ------------------------------------------------------------------ #

    def _start_hades(self) -> bool:
        """Subscribe a HadesFskSdrDemod to the SDR pipeline; False (reason on screen) if not."""
        if self._hades_demod is not None:
            return True
        norad = self._combo_hades_sat.currentData()
        if not isinstance(norad, int):
            self._set_error("⚠ " + _("Select a satellite first."))
            return False
        pipeline = self._sdr_pipeline
        if pipeline is None:
            pipeline = self._auto_connect_sdr()
            if pipeline is None:
                return False
        from comms.telemetry.hades_fsk_sdr import HadesFskSdrDemod

        try:
            demod = HadesFskSdrDemod(
                int(pipeline._device.sample_rate),  # type: ignore[attr-defined]
                parent=self,
                norad=norad,
            )
            demod.frame_received.connect(self._on_hades_frame)
            demod.start()
            self._hades_demod = demod  # held before subscribing so a failure is cleaned up
            pipeline.subscribe(demod.push_samples)  # type: ignore[attr-defined]
            self._hades_demod_pipeline = pipeline
        except Exception:
            logger.exception("HADES FSK: could not attach to the SDR pipeline")
            self._stop_hades()
            self._set_error("⚠ " + _("Could not start the HADES FSK demodulator."))
            return False
        self._hades_norad = norad
        self._voice_tab_asked = False
        if self._decode_tabs_norad != norad:
            self._rebuild_decode_tabs(norad)
        if norad == 68446:
            self._lbl_status.setText(_("HADES FSK — listening for HADES-SA (800 baud 2FSK)"))
        elif norad == 98206:
            self._lbl_status.setText(
                _("HADES FSK — listening for HADES-L (800 baud 2FSK bursts, ±20 kHz)")
            )
        else:
            self._lbl_status.setText(_("HADES FSK — listening for 200 baud bursts (±20 kHz)"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        return True

    def _stop_hades(self) -> None:
        """Detach and stop the HADES FSK demodulator, if running."""
        if self._hades_demod_pipeline is not None and self._hades_demod is not None:
            with contextlib.suppress(Exception):
                self._hades_demod_pipeline.unsubscribe(self._hades_demod.push_samples)
        self._hades_demod_pipeline = None
        if self._hades_demod is not None:
            with contextlib.suppress(Exception):
                self._hades_demod.frame_received.disconnect()
            self._hades_demod.stop()
            self._hades_demod = None
        self._hades_norad = None

    @staticmethod
    def _hades_row_text(frame: Any) -> str:
        """The Data column of a HADES FSK row: type, uptime, carrier offset, then the bytes."""
        head = f"[{frame.type_name}]"
        uptime = frame.uptime_s
        uptime_label = _("uptime")  # not inside the f-string: xgettext cannot see it there
        extra = f"  {uptime_label} {uptime} s" if uptime is not None else ""
        carrier = "" if math.isnan(frame.carrier_hz) else f"  {frame.carrier_hz / 1000:+.1f} kHz"
        return f"{head}{extra}{carrier}  {format_frame_hex(frame.payload)}"

    def _on_hades_frame(self, frame: Any, age_s: float) -> None:
        """A packet from the HADES FSK demodulator: show it and log it for "Load saved"."""
        norad = self._hades_norad
        if norad is None:
            return
        when, reliable = self._frame_time()
        when -= datetime.timedelta(seconds=float(age_s))
        self._add_hades_frame(frame, norad, when, reliable, stream_time=float(frame.time_s))

    def _add_hades_frame(
        self,
        frame: Any,
        norad: int,
        when: datetime.datetime,
        reliable: bool,
        submit: bool = True,
        stream_time: float = 0.0,
        imported: bool = False,
    ) -> None:
        """Show, log and (when *submit* and the switch is on) upload one frame.

        *imported*: the frame comes from a SatNOGS DB CSV, so it is never sent back and is not
        given to the Digital Voice tab.

        *stream_time*: seconds since the stream (or the recording file) started, which a voice
        packet carries to the Digital Voice tab to tell one message from the next.
        """
        sat_name = self._satellite_name(norad) or self._selected_name or "—"
        text = self._hades_row_text(frame)
        payload = bytes(frame.payload)
        log_id = self._persist_hades_row(frame, norad, sat_name, text, when, reliable)
        # Every frame is sent to the SatNOGS DB, a bit-repaired one too (shown muted).
        ax25_row = _Ax25Row(raw=payload, when=when, reliable=reliable, norad=norad, log_id=log_id)
        decoded = hades_telemetry_frame(payload, norad, sat_name)
        self._append_row(
            callsign="HADES",
            sat_name=sat_name,
            data=text,
            norad=norad,
            ts=when,
            dim=bool(frame.repaired),
            ax25_row=ax25_row,
            decoded=decoded,
        )
        self._mark_row_logged(self._table.rowCount() - 1, log_id)
        if decoded is not None and not frame.repaired:
            self._update_decode_tab(decoded)  # the newest verified frame of each type
        if submit:
            self._submit_raw_frame(payload, norad, when, reliable, ax25_row)
        if not frame.repaired and not imported:
            self._forward_voice_packet(payload, stream_time)

    # -- Fast decode of a recording file (HADES FSK, no SDR pipeline) ---------------------- #

    def _load_decode_speed(self) -> None:
        """Restore the Speed choice (Normal / Fast) from app_settings."""
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_DECODE_SPEED_KEY,)
            ).fetchone()
            if row:
                idx = self._combo_decode_speed.findData(str(row["value"]))
                if idx >= 0:
                    self._combo_decode_speed.blockSignals(True)
                    self._combo_decode_speed.setCurrentIndex(idx)
                    self._combo_decode_speed.blockSignals(False)

    def _on_decode_speed_changed(self, _index: int) -> None:
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_DECODE_SPEED_KEY, str(self._combo_decode_speed.currentData())),
            )
            self._conn.commit()

    def _on_fast_decode(self) -> None:
        """Ask for a recording file and decode all of it at once."""
        start_dir = Path.home() / "iq_recordings"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Decode IQ Recording"),
            str(start_dir if start_dir.is_dir() else Path.home()),
            "IQ WAV (*.wav);;All files (*)",
        )
        if path:
            self._start_fast_decode(Path(path))

    def _start_fast_decode(self, path: Path) -> bool:
        """Start decoding *path* in a worker thread; False (reason on screen) if it cannot."""
        if self._current_mode() == _MODE_CW:
            return self._start_cw_fast_decode(path)
        if self._current_mode() == _MODE_GR:
            return self._start_gr_fast_decode(path)
        if self._current_mode() == _MODE_AFSK:
            return self._start_ax25_fast_decode(path)
        if self._fast_job is not None or self._hades_demod is not None:
            self._set_error("⚠ " + _("Stop the running decoder first."))
            return False
        norad = self._combo_hades_sat.currentData()
        if not isinstance(norad, int):
            self._set_error("⚠ " + _("Select a satellite first."))
            return False
        from comms.telemetry.hades_fsk_sdr import HadesFileDecoder
        from sdr.file_device import parse_start_time_from_filename

        start = parse_start_time_from_filename(path.name)
        self._fast_time_known = start is not None
        self._fast_start = start or datetime.datetime.now(datetime.UTC)
        self._hades_norad = norad
        self._voice_tab_asked = False
        if self._decode_tabs_norad != norad:
            self._rebuild_decode_tabs(norad)
        job = HadesFileDecoder(path, parent=self, norad=norad)
        job.frame_found.connect(self._on_fast_frame)
        job.progress.connect(self._on_fast_progress)
        job.decoded.connect(self._on_fast_done)
        job.failed.connect(self._on_fast_failed)
        self._fast_job = job
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_decode_iq.setEnabled(False)
        self._lbl_status.setText(_("Fast decode: starting…"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        job.start()
        return True

    def _start_gr_fast_decode(self, path: Path) -> bool:
        """Decode *path* with gr_satellites at once; False (reason on screen) if it cannot."""
        if self._fast_job is not None or self._gr_backend.is_running:
            self._set_error("⚠ " + _("Stop the running decoder first."))
            return False
        norad = self._combo_gr_sat.currentData()
        if not isinstance(norad, int):
            self._set_error("⚠ " + _("Select a satellite first."))
            return False
        if not detect_gr_satellites():
            self._set_error("⚠ " + _("gr-satellites is not installed — see Help > gr-satellites…"))
            return False
        from comms.telemetry.gr_satellites_file import GrFileDecoder
        from sdr.file_device import parse_start_time_from_filename

        start = parse_start_time_from_filename(path.name)
        self._gr_bogus_count = 0
        self._gr_bogus_pending.clear()
        self._fast_time_known = start is not None
        self._fast_start = start or datetime.datetime.now(datetime.UTC)
        self._selected_norad = norad
        self._selected_name = self._satellite_name(norad) or self._selected_name
        job = GrFileDecoder(path, norad, catalog_norad=self._gr_catalog_ids.get(norad), parent=self)
        job.frame_found.connect(self._on_gr_fast_frame)
        job.progress.connect(self._on_fast_progress)
        job.decoded.connect(self._on_gr_fast_done)
        job.failed.connect(self._on_fast_failed)
        self._fast_job = job
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_decode_iq.setEnabled(False)
        self._lbl_status.setText(_("Fast decode: starting…"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        job.start()
        return True

    def _start_ax25_fast_decode(self, path: Path) -> bool:
        """Decode *path* at once (AX.25): the coherent MSK detector at 4800 / 9600 baud, Direwolf
        fed with the satellite AFSK front end's audio at 1200 baud."""
        if self._fast_job is not None or self._engine.is_running:
            self._set_error("⚠ " + _("Stop the running decoder first."))
            return False
        from sdr.file_device import parse_start_time_from_filename

        start = parse_start_time_from_filename(path.name)
        self._fast_time_known = start is not None
        self._fast_start = start or datetime.datetime.now(datetime.UTC)
        job: Any
        if self._current_modem() == "1200":
            from comms.aprs.afsk_file_decoder import Afsk1200FileDecoder

            job = Afsk1200FileDecoder(path, parent=self)
        else:
            from comms.aprs.coherent_msk import CoherentMskFileDecoder

            job = CoherentMskFileDecoder(path, int(self._current_modem()), parent=self)
        job.frame_found.connect(self._on_ax25_fast_frame)
        job.progress.connect(self._on_fast_progress)
        job.decoded.connect(self._on_fast_done)
        job.failed.connect(self._on_fast_failed)
        self._fast_job = job
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_decode_iq.setEnabled(False)
        self._lbl_status.setText(_("Fast decode: starting…"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        job.start()
        return True

    def _on_ax25_fast_frame(self, raw: bytes, seconds: float) -> None:
        """One frame of a coherent MSK fast decode, with its time in the recording."""
        if self._fast_job is None:
            return
        self._fast_frame_when = self._fast_start + datetime.timedelta(seconds=float(seconds))
        try:
            self._on_ax25_frame(raw)
        finally:
            self._fast_frame_when = None

    def _on_gr_fast_frame(self, text: str, seconds: float, raw: bytes) -> None:
        """One frame of a fast decode: add it with its estimated time.

        The frame comes first, as it does live, so the row finds it and "Send selected" and
        the automatic SatNOGS upload have its bytes. The time is only an estimate (a few
        seconds off) and is trusted only when the recording's start time is known.
        """
        if self._fast_job is None:
            return
        self._gr_fast_when = self._fast_start + datetime.timedelta(seconds=float(seconds))
        try:
            if raw:
                self._on_gr_raw_frame(raw)
            self._on_gr_telemetry(text)
        finally:
            self._gr_fast_when = None

    def _on_gr_fast_done(self, duration_s: float, elapsed_s: float, frames: int) -> None:
        note = (
            "  ⚠ " + _("Frame times are estimated (a few seconds off).")
            if self._fast_time_known
            else "  ⚠ " + _("No start time in the file name: not sent to SatNOGS.")
        )
        self._finish_fast_decode()
        noise = min(self._gr_bogus_count, frames)
        text = _("Decoded {dur} s of recording in {el} s — {n} frame(s)").format(
            dur=round(duration_s), el=round(elapsed_s, 1), n=frames - noise
        )
        if noise:
            text += "  ⚠ " + _(
                "{k} more frame(s) ignored as noise (invalid AX.25 header): "
                "not shown, not logged, not sent to SatNOGS."
            ).format(k=noise)
        self._lbl_status.setText(text + note)
        self._lbl_status.setStyleSheet("color: #f39c12;")

    def _on_fast_frame(self, frame: Any, seconds: float) -> None:
        norad = self._hades_norad
        if norad is None or self._fast_job is None:
            return
        when = self._fast_start + datetime.timedelta(seconds=float(seconds))
        # A recording whose start time is not in its file name has no trustworthy time:
        # its frames are shown and logged, never sent.
        self._add_hades_frame(
            frame,
            norad,
            when,
            self._fast_time_known,
            submit=self._fast_time_known,
            stream_time=float(seconds),
        )

    def _on_fast_progress(self, fraction: float, frames: int) -> None:
        if self._fast_job is None:
            return
        if fraction < 0:
            self._lbl_status.setText(_("Fast decode: running — {n} frame(s)").format(n=frames))
            return
        self._lbl_status.setText(
            _("Fast decode: {pct}% — {n} frame(s)").format(pct=int(fraction * 100), n=frames)
        )

    def _on_fast_done(self, duration_s: float, elapsed_s: float, frames: int) -> None:
        note = (
            ""
            if self._fast_time_known
            else "  ⚠ " + _("No start time in the file name: not sent to SatNOGS.")
        )
        self._finish_fast_decode()
        self._lbl_status.setText(
            _("Decoded {dur} s of recording in {el} s — {n} frame(s)").format(
                dur=round(duration_s), el=round(elapsed_s, 1), n=frames
            )
            + note
        )
        self._lbl_status.setStyleSheet(
            "color: #27ae60;" if self._fast_time_known else "color: #f39c12;"
        )

    def _on_fast_failed(self, message: str) -> None:
        self._finish_fast_decode()
        self._set_error("⚠ " + _("Could not decode the recording: ") + message)

    def _finish_fast_decode(self) -> None:
        """The worker is done (or gone): release it and restore the buttons."""
        job = self._fast_job
        self._fast_job = None
        if job is not None:
            for signal in (job.frame_found, job.progress, job.decoded, job.failed):
                with contextlib.suppress(Exception):
                    signal.disconnect()
            job.wait(3000)
        if self._hades_demod is None:
            self._hades_norad = None
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_decode_iq.setEnabled(True)

    def _stop_fast_decode(self) -> None:
        """Cancel a running fast decode (Stop, mode change, close)."""
        if self._fast_job is None:
            return
        self._fast_job.cancel()
        self._finish_fast_decode()
        self._lbl_status.setText(_("Fast decode stopped"))
        self._lbl_status.setStyleSheet("color: #aaa;")

    def _forward_voice_packet(self, payload: bytes, stream_time: float) -> None:
        """Hand a verified UNNE-1B voice packet (type 15) to the Digital Voice tab.

        The tab is opened behind this one on the first packet of a run. A packet is
        ``type/address, packet number, 35 XOR-masked CODEC2 bytes``.
        """
        from comms.telemetry.hades_fsk import UNNE1B_ADDRESS

        if len(payload) < 37 or payload[0] != 0xF0 | UNNE1B_ADDRESS:
            return
        from comms.voice.hades_voice import unmask

        if not self._voice_tab_asked:
            self._voice_tab_asked = True
            self.voice_tab_requested.emit()
            self._lbl_status.setText(_("Voice packets are sent to the Digital Voice tab."))
            self._lbl_status.setStyleSheet("color: #27ae60;")
        self.voice_packet_ready.emit(payload[1], unmask(bytes(payload[2:37])), float(stream_time))

    def _persist_hades_row(
        self,
        frame: Any,
        norad: int,
        sat_name: str,
        text: str,
        when: datetime.datetime,
        reliable: bool,
    ) -> int | None:
        """Log a HADES FSK frame (its descrambled payload, no CRC) with kind "hades"."""
        if not hasattr(self._conn, "execute"):
            return None
        payload_hex = bytes(frame.payload).hex()
        known = self._find_logged_duplicate("frame_hex", payload_hex, norad, when, "hades")
        if known is not None:
            return known[0]
        parsed = json.dumps(
            {
                "text": text,
                "sat": sat_name,
                "type": frame.packet_type,
                "uptime": frame.uptime_s,
                "carrier_hz": None
                if math.isnan(float(frame.carrier_hz))
                else round(float(frame.carrier_hz)),
                "repaired": bool(frame.repaired),
            }
        )
        cursor = self._conn.execute(
            """INSERT INTO telemetry_log
               (received_at, norad_cat_id, callsign, raw_hex, parsed_json, time_reliable,
                frame_hex, kind)
               VALUES (?, ?, 'HADES', ?, ?, ?, ?, 'hades')""",
            (when.isoformat(), norad, payload_hex, parsed, int(reliable), payload_hex),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        if self._importing and row_id is not None:
            self._import_log_ids.append(int(row_id))
        return int(row_id) if row_id is not None else None

    def _load_saved_hades(
        self,
        norad: int,
        parsed_json: str | None,
        when: datetime.datetime,
        log_id: int,
        whole: bytes,
        uploaded: object,
        reliable: bool,
    ) -> None:
        """Show one logged HADES FSK frame again; a verified one stays sendable."""
        try:
            saved = json.loads(parsed_json) if parsed_json else {}
        except (ValueError, TypeError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        sat_name = str(saved.get("sat") or self._satellite_name(norad))
        repaired = bool(saved.get("repaired"))
        ax25_row = (
            None
            if not whole
            else _Ax25Row(
                raw=whole,
                when=when,
                reliable=reliable,
                norad=norad,
                sent=uploaded is not None,
                log_id=log_id,
            )
        )
        decoded = hades_telemetry_frame(whole, norad, sat_name) if whole else None
        self._append_row(
            callsign="HADES",
            sat_name=sat_name or "—",
            data=_HADES_REPAIRED_LABEL.sub("", str(saved.get("text", ""))),
            norad=norad,
            ts=when,
            dim=repaired,
            ax25_row=ax25_row,
            decoded=decoded,
        )
        if decoded is not None and not repaired:
            self._update_decode_tab(decoded)
        self._mark_row_logged(self._table.rowCount() - 1, log_id)

    def _on_gr_status(self, msg: str) -> None:
        self._lbl_status.setText(msg)
        color = "#27ae60" if self._gr_backend.is_running else "#aaa"
        self._lbl_status.setStyleSheet(f"color: {color};")

    def _on_gr_telemetry(self, text: str) -> None:
        """Parse a gr-satellites stdout block and add it to the table."""
        if gr_packet_is_bogus(text):
            self._reject_gr_noise_frame()
            return
        callsign = ""
        data_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if line.startswith("-> Packet from"):
                callsign = line.replace("-> Packet from", "").strip()
            elif stripped and stripped != "Container:":
                data_lines.append(stripped)

        # The row belongs to the satellite being decoded, not to whatever is selected in the main
        # list: a Fast decode of a recording leaves ``_selected_name`` at the earlier selection
        # (a CroCube recording was listed as PARUS-T2).
        decoded_norad = self._gr_backend.started_norad or self._selected_norad
        sat_name = self._satellite_name(decoded_norad) if decoded_norad else ""
        if not sat_name and decoded_norad == self._selected_norad:
            sat_name = self._selected_name
        if not sat_name and decoded_norad:
            info = get_satellite_info(self._gr_catalog_ids.get(decoded_norad, decoded_norad))
            sat_name = str(info.get("name", "")) if info else ""
        data_text = "  |  ".join(data_lines) if data_lines else text[:120]
        addresses, info_bytes = parse_gr_packet(text)
        if info_bytes is None:
            info_bytes = parse_gr_pdu_dump(text)  # a framing gr-satellites cannot parse
        ascii_text: str | None = None
        gr_decoded = self._decode_gr_text(text)
        gr_summary = summary_text(gr_decoded)
        if info_bytes is not None:
            # like the AX.25 mode: the data column is the frame's bytes in HEX; the decoded
            # fields are in the "Decoded Fields" tab
            data_text = format_frame_hex(info_bytes)
            # the "ASCII View" form: the log line for a parsed AX.25 packet, else a dotted dump
            ascii_text = (
                addressed_ascii(addresses, info_bytes, gr_summary)
                if len(addresses) > 1
                else describe_parts(None, info_bytes, gr_summary)
            )
        if len(addresses) > 1:
            callsign = addresses[1]  # the source address; the first is the destination
        full_text = "\n".join(
            line.rstrip()
            for line in text.splitlines()
            if line.strip() and line.strip() != "Container:"
        )

        when, reliable = self._frame_time()
        self._gr_prune()
        if self._gr_frames:
            # the packet's frame arrived first
            row_data = self._gr_frames.popleft()[1]
            self._submit_raw_frame(
                row_data.raw, row_data.norad, row_data.when, row_data.reliable, row_data
            )
        else:
            row_data = _Ax25Row(
                raw=b"",
                when=when,
                reliable=reliable,
                norad=self._gr_backend.started_norad or self._selected_norad,
            )
            self._gr_rows.append((time.monotonic(), row_data))
        self._append_row(
            callsign=callsign or sat_name or "—",
            sat_name=sat_name or "—",
            data=data_text,
            norad=self._selected_norad,
            ts=when,
            ax25_row=row_data,
            gr_text=full_text,
            ascii_text=ascii_text,
        )
        row_data.log_id = self._persist_gr_row(callsign, sat_name, data_text, row_data, full_text)
        self._mark_row_logged(self._table.rowCount() - 1, row_data.log_id)
        if gr_decoded is not None and gr_decoded.has_fields:
            self._update_decode_tab(gr_decoded)

    def _on_gr_raw_frame(self, raw: bytes) -> None:
        """Forward a gr-satellites --kiss_server data frame to SatNOGS DB.

        No-op unless the footer toggle is on and callsign / location / API
        key are all set (see SatnogsUploader.submit()). The subprocess
        targets exactly one satellite per run, so its NORAD is
        ``started_norad`` rather than something resolved per-frame — mirrors
        _on_ax25_frame()'s forwarding for the AFSK/Direwolf path.
        """
        norad = self._gr_backend.started_norad
        if norad is None and self._gr_fast_when is not None:
            norad = self._selected_norad  # a fast decode runs no backend process
        if norad is None:
            return
        horizon = time.monotonic() - _GR_PAIR_WINDOW_S
        while self._gr_bogus_pending and self._gr_bogus_pending[0] < horizon:
            self._gr_bogus_pending.popleft()
        if self._gr_bogus_pending:
            self._gr_bogus_pending.popleft()  # the text of this frame was rejected as noise
            return
        when, reliable = self._frame_time()
        frame = _Ax25Row(raw=raw, when=when, reliable=reliable, norad=norad)
        self._gr_prune()
        if self._gr_rows:
            # the packet's text came first and was sound: send now
            self._submit_raw_frame(raw, norad, when, reliable, frame)
            self._pair_gr_frame(frame)
        else:
            # Wait for the text: a noise frame is told apart by its header (see
            # gr_packet_is_bogus()), so it must not reach SatNOGS before that is known. A frame
            # whose text never comes is sent when its time is up (_gr_prune()).
            self._pair_gr_frame(frame)
            self._gr_prune_timer.start(int(_GR_PAIR_WINDOW_S * 1000) + 250)

    def _persist_gr_row(
        self,
        callsign: str,
        sat_name: str,
        data_text: str,
        row_data: _Ax25Row,
        full_text: str = "",
    ) -> int | None:
        """Log a gr-satellites row (its text, and its frame once it has one) for "Load saved"."""
        if not hasattr(self._conn, "execute"):
            return None
        norad = row_data.norad if row_data.norad is not None else self._selected_norad
        parsed = json.dumps({"text": data_text, "sat": sat_name, "full": full_text})
        known = self._find_logged_duplicate(
            "parsed_json",
            parsed,
            norad,
            row_data.when,
            "gr",
            # a fast decode's times are estimates that differ by seconds between runs
            window_s=(
                _GR_FAST_DUPLICATE_WINDOW_S
                if self._gr_fast_when is not None
                else _DUPLICATE_WINDOW_S
            ),
        )
        if known is not None:
            return known[0]
        cursor = self._conn.execute(
            """INSERT INTO telemetry_log
               (received_at, norad_cat_id, callsign, raw_hex, parsed_json, time_reliable,
                frame_hex, kind, satnogs_uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'gr', ?)""",
            (
                row_data.when.isoformat(),
                norad,
                callsign,
                row_data.raw.hex(),
                parsed,
                int(row_data.reliable),
                row_data.raw.hex() or None,
                datetime.datetime.now(datetime.UTC).isoformat() if row_data.sent else None,
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        return int(row_id) if row_id is not None else None

    def _save_gr_frame(self, frame: _Ax25Row) -> None:
        """A logged gr-satellites row has just been given its frame: record it."""
        if frame.log_id is None or not hasattr(self._conn, "execute"):
            return
        self._conn.execute(
            "UPDATE telemetry_log SET frame_hex = ?, raw_hex = ?, received_at = ?,"
            " time_reliable = ? WHERE id = ?",
            (frame.raw.hex(), frame.raw.hex(), frame.when.isoformat(), int(frame.reliable),
             frame.log_id),
        )  # fmt: skip
        self._conn.commit()
        if frame.sent:
            mark_uploaded(self._conn, frame.log_id)

    def _reject_gr_noise_frame(self) -> None:
        """Drop a gr-satellites packet that is noise: no row, no log entry, no SatNOGS upload."""
        self._gr_prune()
        if self._gr_frames:
            self._gr_frames.popleft()
        else:
            self._gr_bogus_pending.append(time.monotonic())
        self._gr_bogus_count += 1
        self._set_error(
            "⚠ "
            + _(
                "Ignored %(n)d frame(s) whose AX.25 header is invalid (noise that passed the CRC):"
                " not logged, not sent to SatNOGS."
            )
            % {"n": self._gr_bogus_count}
        )

    def _gr_prune(self) -> None:
        """Drop rows that waited too long for their frame; send frames that waited for a row."""
        horizon = time.monotonic() - _GR_PAIR_WINDOW_S
        while self._gr_rows and self._gr_rows[0][0] < horizon:
            self._gr_rows.popleft()
        while self._gr_frames and self._gr_frames[0][0] < horizon:
            frame = self._gr_frames.popleft()[1]
            self._submit_raw_frame(frame.raw, frame.norad, frame.when, frame.reliable, frame)

    def _pair_gr_frame(self, frame: _Ax25Row) -> None:
        """Give *frame* to the oldest table row still waiting for one, else keep it waiting.

        The row then carries the frame (and, from the automatic upload, whether SatNOGS has
        it), which is what "Send selected" sends.
        """
        self._gr_prune()
        while self._gr_rows:
            placeholder = self._gr_rows.popleft()[1]
            item = self._find_ax25_item(placeholder)
            if item is not None:
                item.setData(_AX25_ROLE, frame)
                frame.log_id = placeholder.log_id
                self._save_gr_frame(frame)
                return
            # that row was cleared from the table meanwhile
        self._gr_frames.append((time.monotonic(), frame))

    def _find_ax25_item(self, row_data: _Ax25Row) -> QTableWidgetItem | None:
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None and item.data(_AX25_ROLE) is row_data:
                return item
        return None

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
        modem = self._current_modem()
        ok, err = self._engine.start_rig(_ENGINE_OWNER, "N0CALL", 0, "", modem=modem)
        if not ok:
            self._set_error(f"⚠ {err}")
            return
        self._engine.raw_frame_received.connect(self._on_ax25_frame)
        self._engine.error_occurred.connect(self._set_error)
        self._afsk_source = "direwolf"
        self._refresh_status()

    def _try_start_sdr(self, pipeline: object) -> None:
        """Start AX.25 reception on the SDR pipeline (receive only).

        Direwolf's own built-in decoder for the resolved baud does the
        actual demod, fed by SDR-derived audio — see
        AprsEngine.start_sdr_direwolf().
        """
        modem = self._current_modem()
        ok, err = self._engine.start_sdr_direwolf(
            _ENGINE_OWNER, pipeline, modem=modem, satellite=True
        )
        if not ok:
            self._set_error(f"⚠ {err}")
            return
        self._sdr_pipeline = pipeline
        self._engine.raw_frame_received.connect(self._on_ax25_frame)
        self._afsk_source = "sdr_direwolf"
        self._refresh_status()

    def _stop_engine(self) -> None:
        """Release this tab's interest in the shared AprsEngine.

        Only actually stops Direwolf once no other tab (e.g. APRS) still
        needs it — see AprsEngine.stop().
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
            self._on_non_ax25_frame(raw)
            return
        norad = self._callsign_to_norad(frame.src)
        if norad is None:
            # The callsign is not one the app knows (T.Microsat-1, say): the frame
            # belongs to the satellite the user picked, so it is still decoded with
            # that satellite's format, logged and uploaded under it.
            norad = self._active_norad()
        tf = decode_telemetry(frame.src, frame.payload, norad, ax25=frame)
        when, reliable = self._frame_time()
        ax25_row = _Ax25Row(
            raw=raw, when=when, reliable=reliable, norad=norad, payload=frame.payload
        )
        # The Data column shows the whole received frame as hex bytes -- the
        # same form as SatNOGS Network's "Data" tab, so it can be compared with
        # other stations' frames and the bytes copied out (image data, say).
        # The per-field values are in the "Decoded Fields" tab.
        self._append_row(
            callsign=tf.callsign,
            sat_name=tf.satellite_name,
            data=format_frame_hex(raw),
            norad=tf.norad,
            ts=when,
            ax25_row=ax25_row,
        )
        self._update_decode_tab(tf)
        ax25_row.log_id = self._persist_frame(tf, when, reliable, raw.hex(), "ax25")
        self._mark_row_logged(self._table.rowCount() - 1, ax25_row.log_id)
        # Forward the raw frame (full AX.25 frame, FCS already stripped by the
        # demodulator / KISS) to the SatNOGS DB. No-op unless the footer
        # toggle is on and callsign / location / API key are all set.
        self._submit_raw_frame(raw, norad, when, reliable, ax25_row)

    def _on_non_ax25_frame(self, raw: bytes) -> None:
        """Show and log a CRC-valid HDLC frame that is not an AX.25 frame.

        Some satellites (ARICA-2, for one) use AX.25's framing but put their own
        data where the address field would be, so decode_ax25() rejects them.
        They are shown as hex like any other frame, attributed to the satellite
        the user selected, logged, and -- like AX.25 frames -- uploaded to SatNOGS
        automatically (the frame is CRC-verified; SatNOGS takes the raw bytes).
        """
        norad = self._selected_norad
        name = ""
        if norad is not None and hasattr(self._conn, "execute"):
            row = self._conn.execute(
                "SELECT name FROM satellites WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            name = str(row["name"]) if row else ""
        when, reliable = self._frame_time()
        tf = TelemetryFrame(norad=norad, callsign="", satellite_name=name, raw_hex=raw.hex())
        log_id = self._persist_frame(tf, when, reliable, raw.hex(), "hdlc")
        ax25_row = _Ax25Row(raw=raw, when=when, reliable=reliable, norad=norad, log_id=log_id)
        self._append_row(
            callsign="—",
            sat_name=name or "—",
            data=format_frame_hex(raw),
            norad=norad,
            ts=when,
            log_id=log_id,
            ax25_row=ax25_row,
        )
        self._submit_raw_frame(raw, norad, when, reliable, ax25_row)

    def _frame_time(self) -> tuple[datetime.datetime, bool]:
        """(UTC time of the frame just decoded, time trustworthy?).

        The wall clock live; for a played-back IQ recording the recording's start
        time plus the playback position (comms.signal_clock). A recording whose
        start time is only a placeholder gives an untrustworthy time.
        """
        if self._import_time is not None:
            return self._import_time, True
        if self._fast_frame_when is not None:
            # a frame of a coherent MSK fast decode: its position in the recording is exact
            return self._fast_frame_when, self._fast_time_known
        if self._gr_fast_when is not None:
            # estimated from the output's timing; the recording's own start must be known
            return self._gr_fast_when, self._fast_time_known
        return signal_time(self._sdr_pipeline)

    def _submit_raw_frame(
        self,
        raw: bytes | str,
        norad: int | None,
        when: datetime.datetime,
        reliable: bool,
        ax25_row: _Ax25Row | None = None,
    ) -> None:
        """Queue *raw* for the SatNOGS DB with its real reception time.

        A frame whose time is only a placeholder (a recording with no start time
        yet) is not sent -- it would be published with a wrong time. The user is
        told once per run.
        """
        if self._importing:
            if ax25_row is not None:
                ax25_row.sent = True  # it comes from SatNOGS DB: never send it back
            return
        if reliable:
            known = (
                self._find_logged_duplicate(
                    "frame_hex",
                    (raw if isinstance(raw, str) else raw.hex()),
                    norad,
                    when,
                    window_s=(
                        _GR_FAST_DUPLICATE_WINDOW_S
                        if self._gr_fast_when is not None
                        else _DUPLICATE_WINDOW_S
                    ),
                )
                if norad is not None
                else None
            )
            if known is not None and known[1]:
                # The same reception (an earlier run over the same recording) is already
                # in the SatNOGS DB: do not send it a second time.
                if ax25_row is not None:
                    ax25_row.sent = True
                return
            if ax25_row is not None:
                # Reported per row so a frame the automatic upload already
                # delivered is not sent a second time by "Send selected".
                row = ax25_row

                def _result(accepted: bool, status: int, body: str) -> None:
                    self._ax25_send_result.emit(row, accepted, status, body)

                queued = get_satnogs_uploader().submit(
                    self._conn, raw, norad, when, on_result=_result
                )
                row.pending = queued
                return
            get_satnogs_uploader().submit(
                self._conn,
                raw,
                norad,
                when,
                on_result=lambda accepted, status, body: self._upload_result.emit(
                    None, accepted, status, body
                ),
            )
            return
        if load_satnogs_upload_settings(self._conn).get("enabled") and not (
            self._warned_time_unconfirmed
        ):
            self._warned_time_unconfirmed = True
            self._set_error(
                _("SatNOGS upload skipped: set the recording's start time in SDR Control first.")
            )

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
        if row:
            return int(row["norad_cat_id"])
        # The SatNOGS DB lists a satellite's callsign among its alternative names
        # (KNACKSAT-2 -> "HS0K"); accept a match only if it is unambiguous.
        matches: list[int] = []
        try:
            candidates = self._conn.execute(
                "SELECT norad_cat_id, alt_names FROM satellites WHERE alt_names LIKE ?",
                (f'%"{call_upper}"%',),
            ).fetchall()
        except sqlite3.Error:
            return None
        for cand in candidates:
            with contextlib.suppress(ValueError, TypeError):
                names = json.loads(str(cand["alt_names"]))
                if isinstance(names, list) and call_upper in (str(n).upper() for n in names):
                    matches.append(int(cand["norad_cat_id"]))
        return matches[0] if len(matches) == 1 else None

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
        ts: datetime.datetime | None = None,
        dim: bool = False,
        log_id: int | None = None,
        ax25_row: _Ax25Row | None = None,
        decoded: TelemetryFrame | None = None,
        gr_text: str | None = None,
        ascii_text: str | None = None,
    ) -> None:
        """Add a row to the Received Frames table.

        *ts* is the time to show (UTC, with the date -- a played-back recording
        can be from any day); the default is now. *dim* mutes the row (italic, a
        theme-relative colour -- see _DimRowDelegate) and leaves it out of the
        frame count (a rejected CW candidate). *log_id* is the frame's
        ``telemetry_log`` id, kept on the row for "Send selected" (CW); *ax25_row*
        is the same for a received AX.25 frame. *decoded* is a CW frame's decoded
        fields, shown in the "Decoded Fields" tab when the row is selected; *gr_text* is a
        gr-satellites row's full decoded text, shown below the table when it is selected.
        *ascii_text* is the row's text for the "ASCII View" tab when it is already known
        (a gr-satellites packet whose addresses and info field were parsed); otherwise it is
        made from the frame's bytes or the Data column.
        """
        when = ts if ts is not None else datetime.datetime.now(datetime.UTC)
        row = self._table.rowCount()
        self._table.insertRow(row)
        for column, text in enumerate(
            (when.strftime("%Y-%m-%d %H:%M:%S"), callsign, sat_name, data)
        ):
            item = QTableWidgetItem(text)
            if column == 3:
                item.setToolTip(text)  # the column is elided when the frame is long
            if dim:
                item.setData(_DIM_ROLE, True)
            if column == 0 and log_id is not None:
                item.setData(Qt.ItemDataRole.UserRole, log_id)
                item.setData(_LOGGED_ROLE, log_id)
            if column == 0 and ax25_row is not None:
                item.setData(_AX25_ROLE, ax25_row)
            if column == 0 and decoded is not None:
                item.setData(_DECODED_ROLE, decoded)
            if column == 0 and gr_text is not None:
                item.setData(_GR_TEXT_ROLE, gr_text)
            self._table.setItem(row, column, item)
        self._table.scrollToBottom()
        if ascii_text is None:
            raw = ax25_row.raw if ax25_row is not None and ax25_row.raw else None
            summary = summary_text(decoded) or self._binary_summary(
                raw or hex_text_to_bytes(data), callsign, norad
            )
            ascii_text = row_ascii(raw, data, summary)
        self._append_ascii_row(when, callsign, sat_name, ascii_text, dim)
        if not dim:
            self._frame_count += 1
            self._lbl_count.setText(_("Frames: ") + str(self._frame_count) + _(" received"))

    _SYNC_FIT_ROWS = 300  # up to this many rows are re-measured at once (about 10 ms)

    def _on_log_page_changed(self) -> None:
        """A sub-tab became current: fit its rows now and again after layout has settled."""
        self._fit_visible_rows()
        self._fit_rows_timer.start()

    def _fit_visible_rows(self, force: bool = False) -> None:
        """Measure the rows of the frame tables that are on screen again (see _fit_rows_timer).

        A table with more than _SYNC_FIT_ROWS rows is left to the timer unless *force*.
        """
        pending = False
        for table in (self._table, self._ascii_table):
            if not table.isVisible():
                continue
            if force or table.rowCount() <= self._SYNC_FIT_ROWS:
                table.resizeRowsToContents()
                self._row_fits["ascii" if table is self._ascii_table else "raw"] += 1
            else:
                pending = True
        if pending:
            self._fit_rows_timer.start()

    @staticmethod
    def _binary_summary(raw: bytes | None, callsign: str, norad: int | None) -> str | None:
        """A decoded-fields summary for the "ASCII View" of a binary frame, if it can be decoded.

        Text frames need none (they are listed as text), so nothing is decoded for them.
        """
        if not raw or norad is None:
            return None
        parts = ax25_split(raw)
        info = parts[1] if parts is not None else raw
        if looks_like_text(info):
            return None
        return summary_text(decode_telemetry(callsign, info, norad))

    def _append_ascii_row(
        self, when: datetime.datetime, callsign: str, sat_name: str, text: str, dim: bool
    ) -> None:
        """Add the "ASCII View" counterpart of a Received Frames row."""
        row = self._ascii_table.rowCount()
        self._ascii_table.insertRow(row)
        for column, cell in enumerate(
            (when.strftime("%Y-%m-%d %H:%M:%S"), callsign, sat_name, text)
        ):
            item = QTableWidgetItem(cell)
            if column == 3:
                item.setToolTip(cell)
            if dim:
                item.setData(_DIM_ROLE, True)
            self._ascii_table.setItem(row, column, item)
        self._ascii_table.scrollToBottom()

    def _copy_ascii_rows(self) -> None:
        """Copy the selected "ASCII View" rows as log lines (``[0 time UTC] SRC>DEST:...``).

        That is the form of a satellite log's lines, so they can be pasted into
        Help > ASCII Decode.
        """
        rows = sorted({index.row() for index in self._ascii_table.selectedIndexes()})
        lines = []
        for r in rows:
            stamp = self._ascii_table.item(r, 0)
            text = self._ascii_table.item(r, 3)
            if stamp is not None and text is not None:
                lines.append(f"[0 {stamp.text()} UTC] {text.text()}")
        if lines:
            QApplication.clipboard().setText("\n".join(lines))

    def _rebuild_decode_tabs(self, norad: int | None) -> None:
        """(Re)build the "Decoded Fields" sub-tabs for *norad*.

        Each telemetry ID in the satellite's ``telemetry_ids``/``csv_messages``
        format definition gets its own sub-tab, pre-populated with field
        labels (values filled in as matching frames arrive — see
        _update_decode_tab()). Satellites without either per-ID schema
        (including old flat-``fields`` format files) get the whole
        "Decoded Fields" tab disabled.
        """
        self._decode_id_tabs.clear()
        self._decode_tables = {}
        self._decode_field_rows = {}
        self._decode_tabs_norad = norad
        self._shown_row = {}
        self._decode_banner.setVisible(False)

        from comms.mode_detection import HADES_FSK_NORADS

        if self._current_mode() == _MODE_HADES and norad in HADES_FSK_NORADS:
            # one sub-tab per packet type (HADES-SA has its own status and BBS layouts)
            id_defs: dict[str, Any] | None = hades_type_tabs(3 if norad == 68446 else None)
        else:
            id_defs = get_telemetry_id_defs(norad)
        is_gr = self._current_mode() == _MODE_GR
        decode_tab_index = self._log_tabs.indexOf(self._decode_page)
        self._log_tabs.setTabEnabled(decode_tab_index, bool(id_defs) or is_gr)
        if not id_defs:
            if is_gr:
                self._decode_id_tabs.addTab(self._gr_detail, _("gr-satellites output"))
            return

        # Keys are either numeric (OrigamiSat-2's "65"/"100"/"130") or a
        # text prefix (Marina's "OBC"/"PSU"/...); sort numeric ones in
        # numeric order first, then text ones alphabetically, rather than
        # letting a plain string sort put "100" before "65".
        for id_str in sorted(id_defs.keys(), key=lambda k: (0, int(k)) if k.isdigit() else (1, k)):
            id_def = id_defs[id_str]
            fields = id_def.get("fields", [])
            table = QTableWidget(len(fields), 2)
            table.setHorizontalHeaderLabels([_("Field"), _("Value")])
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            row_map: dict[str, int] = {}
            for row, fd in enumerate(fields):
                table.setItem(row, 0, QTableWidgetItem(fd.get("label", fd["name"])))
                table.setItem(row, 1, QTableWidgetItem("—"))
                row_map[fd["name"]] = row
            table.itemSelectionChanged.connect(
                lambda i=id_str: self._on_decode_selection_changed(i)
            )
            self._decode_tables[id_str] = table
            self._decode_field_rows[id_str] = row_map
            self._decode_id_tabs.addTab(table, id_def.get("label", f"ID{id_str}"))
        if is_gr:
            self._decode_id_tabs.addTab(self._gr_detail, _("gr-satellites output"))

    def _on_table_selection_changed(self) -> None:
        """A Received Frames row was clicked: select the same frame everywhere."""
        if self._syncing_selection:
            return
        rows = self._table.selectionModel().selectedRows()
        if rows:
            self._select_frame(rows[0].row(), "raw")

    def _show_row_in_decode_tab(self, row: int) -> None:
        """Show row *row*'s decoded fields in the "Decoded Fields" tab, all highlighted.

        The tab otherwise holds only the newest frame of each telemetry ID, so an
        earlier frame could not be read once a later one of the same ID arrived.
        AX.25 rows are decoded again from their payload; CW rows carry their decoded
        fields; a gr-satellites row's full text goes to the pane under the table. Other
        rows (HDLC, rejected CW candidates) are left alone.
        """
        item = self._table.item(row, 0)
        if item is None:
            return
        gr_text = item.data(_GR_TEXT_ROLE)
        if isinstance(gr_text, str):
            self._gr_detail.setPlainText(gr_text)
            tf = self._decode_gr_text(gr_text)
            if tf is not None and tf.has_fields:
                self._show_frame_in_decode_tab(tf, row)
            return
        tf = item.data(_DECODED_ROLE)
        if not isinstance(tf, TelemetryFrame):
            tf = self._decode_ax25_row(item.data(_AX25_ROLE), row)
        if tf is None or not tf.has_fields or tf.norad != self._decode_tabs_norad:
            return
        self._show_frame_in_decode_tab(tf, row)

    def _show_frame_in_decode_tab(self, tf: TelemetryFrame, row: int) -> None:
        """Fill the sub-tab of *tf*'s type with the frame of *row*, bring it to the front."""
        self._update_decode_tab(tf, row, select=True)
        table = self._decode_tables.get(str(tf.telemetry_id))
        if table is not None:
            self._decode_id_tabs.setCurrentWidget(table)
        self._refresh_decode_banner()

    def _decode_gr_text(self, text: str) -> TelemetryFrame | None:
        """Decode a gr-satellites packet (info field, or PDU dump) with the satellite's format."""
        addresses, info = parse_gr_packet(text)
        if not info:
            info = parse_gr_pdu_dump(text)  # a framing gr-satellites cannot parse: the whole frame
        if not info or self._selected_norad is None:
            return None
        source = addresses[1] if len(addresses) > 1 else ""
        return decode_telemetry(source, info, self._selected_norad)

    def _decode_ax25_row(self, value: object, row: int) -> TelemetryFrame | None:
        """Decode a received AX.25 row again (None if *value* is not one)."""
        if not isinstance(value, _Ax25Row) or value.norad is None:
            return None
        payload = value.payload
        parsed = decode_ax25(value.raw) if value.raw else None
        if not payload and parsed is not None:
            payload = parsed.payload
        if not payload:
            return None
        callsign_item = self._table.item(row, 1)
        callsign = callsign_item.text() if callsign_item is not None else ""
        return decode_telemetry(callsign, payload, value.norad, ax25=parsed)

    def _update_decode_tab(
        self, tf: TelemetryFrame, row: int | None = None, *, select: bool = False
    ) -> None:
        """Push *tf*'s decoded field values into its "Decoded Fields" sub-tab.

        No-op unless tf carries a recognized telemetry ID for the satellite
        the sub-tabs are currently built for (_rebuild_decode_tabs() is
        called from set_satellite(), so this tracks the main satellite list
        selection, not necessarily the AFSK combo).

        Without *row* the frame has just arrived as the newest row of the Received Frames
        table: it becomes the newest of its type (drawn bold). With *row* it is the frame of
        that existing row, shown because the row was selected; *select* then highlights all
        its fields.
        """
        if tf.telemetry_id is None or not tf.has_fields:
            return
        if row is None:
            row = self._table.rowCount() - 1
            self._note_frame(row, f"{tf.norad}:{tf.telemetry_id}")
        if tf.norad != self._decode_tabs_norad:
            return
        shown = tf.record_ids or [str(tf.telemetry_id)]
        # A frame of several records feeds one sub-tab per record (field names are unique).
        for id_str in shown:
            table = self._decode_tables.get(id_str)
            row_map = self._decode_field_rows.get(id_str)
            if table is not None and row_map is not None:
                self._fill_decode_table(table, row_map, tf)
                self._shown_row[id_str] = row
        for id_str, table in self._decode_tables.items():
            if select and id_str in shown:
                table.selectAll()
            else:
                table.clearSelection()
        self._refresh_decode_banner()

    def _note_frame(self, row: int, key: str) -> None:
        """*row* is the newest frame of type *key*: draw it bold, the one before it regular."""
        previous = self._latest_row.get(key)
        self._row_type[row] = key
        self._latest_row[key] = row
        if previous is not None and previous != row:
            self._set_row_bold(previous, False)
        self._set_row_bold(row, True)

    def _set_row_bold(self, row: int, bold: bool) -> None:
        """Draw a row of Received Frames and of the ASCII View bold (newest) or regular."""
        for table in (self._table, self._ascii_table):
            if row >= table.rowCount():
                continue
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is None:
                    continue
                if bold:
                    font = QFont(table.font())
                    font.setBold(True)
                    item.setFont(font)
                else:
                    item.setData(Qt.ItemDataRole.FontRole, None)

    def _refresh_decode_banner(self, *_args: object) -> None:
        """Warn above the visible sub-tab when it shows a frame older than the type's newest."""
        current = self._decode_id_tabs.currentWidget()
        id_str = next((i for i, t in self._decode_tables.items() if t is current), None)
        row = self._shown_row.get(id_str) if id_str is not None else None
        latest = self._latest_row.get(f"{self._decode_tabs_norad}:{id_str}")
        if row is None or latest is None or row == latest:
            self._decode_banner.setVisible(False)
            return
        stamp = self._table.item(row, 0)
        self._decode_banner.setText(
            _("Showing frame #{n} ({time}) — not the newest of its type").format(
                n=row + 1, time=stamp.text() if stamp is not None else ""
            )
        )
        self._decode_banner.setVisible(True)

    def _on_decode_selection_changed(self, id_str: str) -> None:
        """A field of a "Decoded Fields" sub-tab was clicked: select the frame it came from."""
        if self._syncing_selection:
            return
        table = self._decode_tables.get(id_str)
        row = self._shown_row.get(id_str)
        if table is None or row is None or not table.selectedIndexes():
            return
        self._select_frame(row, "decode")

    def _on_ascii_selection_changed(self) -> None:
        """A row of the ASCII View was clicked: select the same frame everywhere."""
        if self._syncing_selection:
            return
        rows = self._ascii_table.selectionModel().selectedRows()
        if rows:
            self._select_frame(rows[0].row(), "ascii")

    def _select_frame(self, row: int, origin: str) -> None:
        """Select frame *row* in the views other than *origin* ("raw", "ascii" or "decode").

        The Received Frames and ASCII View rows are the same frame, so both are selected; the
        "Decoded Fields" sub-tab of the frame's type then shows that frame (see
        _show_row_in_decode_tab()), unless the click came from there.
        """
        self._syncing_selection = True
        try:
            if origin != "raw":
                self._table.selectRow(row)
            if origin != "ascii":
                self._ascii_table.selectRow(row)
            for table in (self._table, self._ascii_table):
                item = table.item(row, 0)
                if item is not None:
                    table.scrollToItem(item)
            if origin != "decode":
                self._show_row_in_decode_tab(row)
        finally:
            self._syncing_selection = False

    @staticmethod
    def _fill_decode_table(
        table: QTableWidget, row_map: dict[str, int], tf: TelemetryFrame
    ) -> None:
        """Write the values of *tf*'s fields into the rows of one "Decoded Fields" table."""
        for f in tf.fields:
            row = row_map.get(f.name)
            if row is None:
                continue
            if f.is_string:
                text = f.unit
            else:
                value = f.scaled_value
                if f.is_integer:
                    text = f"{round(value):,}"
                elif abs(value) >= 1000:
                    text = f"{value:,.2f}"
                elif abs(value) >= 1:
                    text = f"{value:.4f}"
                else:
                    text = f"{value:.6f}"
                if f.unit:
                    text += f" {f.unit}"
            item = table.item(row, 1)
            if item is None:
                item = QTableWidgetItem()
                table.setItem(row, 1, item)
            item.setText(text)

    def _find_logged_duplicate(
        self,
        column: str,
        value: str,
        norad: int | None,
        when: datetime.datetime,
        kind: str | None = None,
        window_s: float = _DUPLICATE_WINDOW_S,
    ) -> tuple[int, bool] | None:
        """The ``telemetry_log`` row already holding this reception, if any.

        Same satellite, same *value* in *column* (``frame_hex`` or ``parsed_json``), same
        *kind* (when given) and a reception time within ``_DUPLICATE_WINDOW_S``: replaying
        a recording that was decoded before finds its earlier rows instead of adding
        copies (live frames are never merged). Returns (log id, already sent to SatNOGS?).
        """
        if not hasattr(self._conn, "execute") or column not in ("frame_hex", "parsed_json"):
            return None
        if (
            replay_time(self._sdr_pipeline) is None
            and self._fast_job is None
            and not self._importing
        ):
            # Live reception: two equal frames are two receptions. Only a played-back
            # (or fast-decoded) recording can deliver the same reception twice.
            return None
        row = self._conn.execute(
            f"SELECT id, satnogs_uploaded_at FROM telemetry_log WHERE {column} = ?"
            " AND IFNULL(norad_cat_id, 0) = IFNULL(?, 0) AND (? IS NULL OR kind = ?)"
            " AND ABS(julianday(received_at) - julianday(?)) * 86400 <= ?"
            " ORDER BY id LIMIT 1",
            (value, norad, kind, kind, when.isoformat(), window_s),
        ).fetchone()
        return (int(row[0]), row[1] is not None) if row else None

    def _persist_frame(
        self,
        tf: TelemetryFrame,
        ts: datetime.datetime,
        reliable: bool = True,
        frame_hex: str | None = None,
        kind: str | None = None,
    ) -> int | None:
        """Log *tf* (*frame_hex*: the whole frame; *kind*: "ax25" / "hdlc" / "cw");
        returns its ``telemetry_log`` id. *reliable* is False when *ts*
        comes from a placeholder recording start time (such frames are never sent)."""
        if not hasattr(self._conn, "execute"):
            return None
        if frame_hex:
            known = self._find_logged_duplicate("frame_hex", frame_hex, tf.norad, ts, kind)
            if known is not None:
                return known[0]
        parsed = (
            json.dumps({f.name: {"value": f.scaled_value, "unit": f.unit} for f in tf.fields})
            if tf.fields
            else None
        )
        cursor = self._conn.execute(
            """INSERT INTO telemetry_log
               (received_at, norad_cat_id, callsign, raw_hex, parsed_json, time_reliable,
                frame_hex, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts.isoformat(),
                tf.norad,
                tf.callsign,
                tf.raw_hex,
                parsed,
                int(reliable),
                frame_hex,
                kind,
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
        if self._importing and row_id is not None:
            self._import_log_ids.append(int(row_id))
        return int(row_id) if row_id is not None else None

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #

    def _set_error(self, msg: str) -> None:
        self._lbl_status.setText(msg)
        self._lbl_status.setStyleSheet("color: #e74c3c;")

    def _refresh_status(self) -> None:
        if self._gr_backend.is_running:
            return  # managed by _on_gr_status
        if self._cw_tlm_norad is not None:
            return  # CW TLM is running; _start_cw_tlm() set the status
        if self._hades_demod is not None or self._fast_job is not None:
            return  # HADES FSK is running; _start_hades() / the fast decode set the status
        if self._afsk_source == "direwolf" and self._engine.is_running:
            modem = self._engine.current_modem
            suffix = f"  [{modem} baud]" if modem else ""
            self._lbl_status.setText(_("Rig + Direwolf (receiving)") + suffix)
            self._lbl_status.setStyleSheet("color: #27ae60;")
        elif self._afsk_source == "sdr_direwolf" and self._engine.is_running:
            modem = self._engine.current_modem
            suffix = f"  [{modem} baud]" if modem else ""
            self._lbl_status.setText(_("SDR — AX.25 (receive only)") + suffix)
            self._lbl_status.setStyleSheet("color: #4a9eff;")
        else:
            # gr-satellites is SDR-only (no Rig + Sound Card path, unlike
            # Direwolf mode above), so its idle hint must not mention Rig.
            if self._current_mode() in (_MODE_GR, _MODE_HADES):
                self._lbl_status.setText(_("—  (connect SDR, then click ▶ Start)"))
            elif self._current_mode() == _MODE_CW:
                self._lbl_status.setText(
                    _("—  (click ▶ Start; the CW Decoder tab opens automatically)")
                )
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

    def _mark_row_logged(self, row: int, log_id: int | None) -> None:
        """Remember on table *row* which ``telemetry_log`` row it shows."""
        item = self._table.item(row, 0)
        if item is not None and log_id is not None:
            item.setData(_LOGGED_ROLE, log_id)

    def _shown_log_ids(self) -> set[int]:
        ids: set[int] = set()
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            value = item.data(_LOGGED_ROLE) if item is not None else None
            if isinstance(value, int):
                ids.add(value)
        return ids

    def _on_load_saved(self) -> None:
        """Show the saved frames of the selected satellite again (no recording replay)."""
        norad = self._active_norad()
        if norad is None:
            self._set_error(_("Select a satellite first."))
            return
        mode = self._current_mode()
        n = self._load_saved_frames(
            norad, cw=mode == _MODE_CW, gr=mode == _MODE_GR, hades=mode == _MODE_HADES
        )
        self._lbl_status.setText(_("Loaded {n} saved frame(s)").format(n=n))
        self._lbl_status.setStyleSheet("color: #27ae60;" if n else "color: #aaa;")

    @staticmethod
    def _saved_kind(norad: int, kind: str | None, callsign: str, parsed_json: str | None) -> str:
        """Kind of a logged frame; rows from before the ``kind`` column are inferred."""
        if kind:
            return kind
        if not callsign:
            return "hdlc"
        # A CW frame's hex text has no marker of its own, but the field names saved with
        # it are the satellite's CW field names.
        frames = load_cw_frames(norad)
        if frames and parsed_json:
            try:
                saved = set(json.loads(parsed_json))
            except (ValueError, TypeError):
                return "ax25"
            cw_names = {
                str(f["name"])
                for frame_def in frames.values()
                for f in frame_def.get("fields", [])
                if isinstance(f, dict) and "name" in f
            }
            if saved and saved <= cw_names:
                return "cw"
        return "ax25"

    def _load_saved_frames(
        self, norad: int, cw: bool = False, gr: bool = False, hades: bool = False
    ) -> int:
        """Append the logged frames of *norad* to the table; returns how many.

        *cw*: the CW TLM frames (CW mode); *gr*: the gr-satellites rows (gr-satellites mode);
        *hades*: the HADES FSK frames; else the AX.25 / HDLC frames.

        A frame logged before its callsign was known (``norad_cat_id`` NULL) is
        included when its callsign now maps to *norad*. Frames already shown are
        skipped. A frame logged before the whole frame was saved shows its payload
        only and cannot be sent to the SatNOGS DB.
        """
        if not hasattr(self._conn, "execute"):
            return 0
        shown = self._shown_log_ids()
        rows = self._conn.execute(
            "SELECT id, received_at, norad_cat_id, callsign, raw_hex, frame_hex, kind,"
            " satnogs_uploaded_at, time_reliable, parsed_json FROM telemetry_log"
            " WHERE norad_cat_id = ? OR norad_cat_id IS NULL ORDER BY received_at, id",
            (norad,),
        ).fetchall()
        name = self._satellite_name(norad)
        loaded = 0
        for r in rows:
            (
                log_id,
                received,
                row_norad,
                callsign,
                raw_hex,
                frame_hex,
                kind,
                uploaded,
                reliable,
                parsed_json,
            ) = (
                int(r[0]),
                str(r[1]),
                r[2],
                str(r[3]),
                str(r[4]),
                r[5],
                r[6],
                r[7],
                bool(r[8]),
                r[9],
            )
            if log_id in shown:
                continue
            kind = self._saved_kind(norad, kind, callsign, parsed_json)
            if (kind == "cw") != cw or (kind == "gr") != gr or (kind == "hades") != hades:
                continue
            if row_norad is None and (not callsign or self._callsign_to_norad(callsign) != norad):
                continue
            try:
                when = datetime.datetime.fromisoformat(received)
                payload = b"" if kind == "cw" else bytes.fromhex(raw_hex)
                whole = bytes.fromhex(str(frame_hex)) if frame_hex else b""
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.UTC)
            if kind == "hades":
                self._load_saved_hades(norad, parsed_json, when, log_id, whole, uploaded, reliable)
                loaded += 1
                continue
            if kind == "gr":
                self._load_saved_gr(
                    norad, callsign, parsed_json, when, whole, uploaded, reliable, log_id
                )
                loaded += 1
                continue
            if kind == "cw":
                if self._load_saved_cw(norad, callsign, raw_hex, parsed_json, when, log_id):
                    loaded += 1
                continue
            if kind == "hdlc":
                self._append_row(
                    callsign="—",
                    sat_name=name or "—",
                    data=format_frame_hex(payload),
                    norad=norad,
                    ts=when,
                    log_id=log_id,
                )
                self._mark_row_logged(self._table.rowCount() - 1, log_id)
                loaded += 1
                continue
            tf = decode_telemetry(
                callsign, payload, norad, ax25=decode_ax25(whole) if whole else None
            )
            ax25_row = _Ax25Row(
                raw=whole,
                when=when,
                reliable=reliable,
                norad=norad,
                sent=uploaded is not None,
                log_id=log_id,
                payload=payload,
            )
            self._append_row(
                callsign=tf.callsign,
                sat_name=tf.satellite_name,
                data=format_frame_hex(whole or payload),
                norad=norad,
                ts=when,
                ax25_row=ax25_row,
            )
            self._mark_row_logged(self._table.rowCount() - 1, log_id)
            self._update_decode_tab(tf)
            loaded += 1
        return loaded

    def _load_saved_gr(
        self,
        norad: int,
        callsign: str,
        parsed_json: str | None,
        when: datetime.datetime,
        whole: bytes,
        uploaded: object,
        reliable: bool,
        log_id: int,
    ) -> None:
        """Show one logged gr-satellites row again; its frame makes it sendable."""
        try:
            saved = json.loads(parsed_json) if parsed_json else {}
        except (ValueError, TypeError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        row_data = _Ax25Row(
            raw=whole,
            when=when,
            reliable=reliable,
            norad=norad,
            sent=uploaded is not None,
            log_id=log_id,
        )
        sat_name = str(saved.get("sat") or self._satellite_name(norad))
        self._append_row(
            callsign=callsign or sat_name or "—",
            sat_name=sat_name or "—",
            data=str(saved.get("text", "")),
            norad=norad,
            ts=when,
            ax25_row=row_data,
            # rows logged before the full text was kept: the one-line text, a field per line
            gr_text=str(saved.get("full") or str(saved.get("text", "")).replace("  |  ", "\n")),
        )
        self._mark_row_logged(self._table.rowCount() - 1, log_id)

    def _load_saved_cw(
        self,
        norad: int,
        callsign: str,
        raw_hex: str,
        parsed_json: str | None,
        when: datetime.datetime,
        log_id: int,
    ) -> bool:
        """Show one logged CW frame again from its saved values; False if it can not be."""
        try:
            saved = json.loads(parsed_json) if parsed_json else {}
        except (ValueError, TypeError):
            return False
        rebuilt = fields_from_saved(norad, saved if isinstance(saved, dict) else {})
        if rebuilt is None:
            return False
        key, label, fields = rebuilt
        tf = TelemetryFrame(
            norad=norad,
            callsign=callsign,
            satellite_name=self._satellite_name(norad),
            raw_hex=raw_hex,
            fields=fields,
            telemetry_id=key,
            telemetry_label=label,
        )
        self._append_row(
            callsign=callsign,
            sat_name=tf.satellite_name,
            data=f"[{key}] {raw_hex}",
            norad=norad,
            ts=when,
            log_id=log_id,
            decoded=tf,
        )
        self._update_decode_tab(tf)
        return True

    def _satellite_name(self, norad: int) -> str:
        if not hasattr(self._conn, "execute"):
            return ""
        row = self._conn.execute(
            "SELECT name FROM satellites WHERE norad_cat_id = ?", (norad,)
        ).fetchone()
        return str(row[0]) if row else ""

    # -- Import CSV (frames other stations sent to SatNOGS DB) ------------------------------- #

    def _on_import_csv(self) -> None:
        """Add the frames of a SatNOGS DB CSV (AX.25 or HADES FSK mode) to the table and log."""
        norad = self._active_norad()
        if norad is None:
            self._set_error(_("Select a satellite first."))
            return
        mode = self._current_mode()
        if mode not in (_MODE_HADES, _MODE_AFSK):
            self._set_error(_("Import CSV works in the AX.25 and HADES FSK modes."))
            return
        downloads = Path.home() / "Downloads"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Import SatNOGS CSV"),
            str(downloads if downloads.is_dir() else Path.home()),
            _("CSV files (*.csv);;All files (*)"),
        )
        if path:
            self.import_csv_file(Path(path), norad, mode)

    def import_csv_file(self, path: Path, norad: int, mode: str) -> tuple[int, int]:
        """Import *path* for satellite *norad*; returns ``(frames read, rows added)``."""
        from comms.telemetry.csv_import import read_frames, unique_frames

        try:
            _lines, frames = read_frames(path)
        except OSError as exc:
            self._set_error(f"⚠ {exc}")
            return 0, 0
        before = self._table.rowCount()
        self._importing = True
        self._import_log_ids = []
        self._import_shown = self._shown_log_ids()
        self._table.setUpdatesEnabled(False)
        try:
            for item in unique_frames(frames):
                self._import_time = item.when
                if self._import_already_shown(item.frame, item.when):
                    continue
                if mode == _MODE_HADES:
                    self._import_hades_frame(item.frame, norad, item.when)
                else:
                    self._on_ax25_frame(item.frame)
                self._import_note_new_row()
        finally:
            self._import_time = None
            self._importing = False
            self._table.setUpdatesEnabled(True)
        self._mark_imported_as_public()
        added = self._table.rowCount() - before
        self._lbl_status.setText(
            _("CSV: {frames} frames read, {added} added to the table.").format(
                frames=len(frames), added=added
            )
        )
        self._lbl_status.setStyleSheet("color: #27ae60;" if added else "color: #aaa;")
        return len(frames), added

    def _import_already_shown(self, frame: bytes, when: datetime.datetime) -> bool:
        """True if this reception is already in the table (an earlier import of the same CSV)."""
        if not self._import_shown or not hasattr(self._conn, "execute"):
            return False
        rows = self._conn.execute(
            "SELECT id FROM telemetry_log WHERE frame_hex = ?"
            " AND ABS(julianday(received_at) - julianday(?)) * 86400 <= ?",
            (frame.hex(), when.isoformat(), _DUPLICATE_WINDOW_S),
        ).fetchall()
        return any(int(r[0]) in self._import_shown for r in rows)

    def _import_note_new_row(self) -> None:
        """Remember the log id of the row just added, so a copy later in the file is skipped."""
        last = self._table.rowCount() - 1
        item = self._table.item(last, 0) if last >= 0 else None
        value = item.data(_LOGGED_ROLE) if item is not None else None
        if isinstance(value, int):
            self._import_shown.add(value)

    def _import_hades_frame(self, frame: bytes, norad: int, when: datetime.datetime) -> None:
        """One CSV frame of a HADES-family satellite: telemetry packets only.

        SatNOGS DB holds the descrambled payload without its CRC, so a frame is accepted when
        its address belongs to the satellite and its length is the packet length minus the CRC.
        """
        from comms.telemetry.hades_fsk import (
            HADES_R_LENGTHS,
            HADES_SA_ADDRESS,
            HADES_SA_LENGTHS,
            UNNE1B_ADDRESS,
            HadesFrame,
        )
        from comms.telemetry.hades_fsk import packet_lengths as hades_packet_lengths
        from comms.telemetry.hades_l_fsk import HADES_L_ADDRESS

        address = (
            HADES_SA_ADDRESS
            if norad == 68446
            else HADES_L_ADDRESS
            if norad == 98206
            else UNNE1B_ADDRESS
        )
        if not frame or frame[0] & 15 != address:
            return
        packet_type = frame[0] >> 4
        if address == HADES_L_ADDRESS:
            # HADES-L's length table is not known apart from the types seen: accept either
            # family's (a wrong length would still have to match a CRC-checked upload).
            lengths = {HADES_R_LENGTHS.get(packet_type), HADES_SA_LENGTHS.get(packet_type)}
            if len(frame) + 2 not in lengths:
                return
        else:
            length = hades_packet_lengths(address).get(packet_type)
            if length is None or len(frame) != length - 2:
                return
        if address == UNNE1B_ADDRESS and packet_type == 15:
            return  # UNNE-1B's voice: the Digital Voice tab's business
        self._add_hades_frame(
            HadesFrame(0, 0.0, float("nan"), 0.0, packet_type, address, frame, False),
            norad,
            when,
            True,
            submit=False,
            imported=True,
        )

    def _mark_imported_as_public(self) -> None:
        """Rows imported from SatNOGS DB count as already sent (never offered for upload)."""
        ids = self._import_log_ids
        self._import_log_ids = []
        if not ids or not hasattr(self._conn, "execute"):
            return
        now = datetime.datetime.now(datetime.UTC).isoformat()
        self._conn.executemany(
            "UPDATE telemetry_log SET satnogs_uploaded_at = ? WHERE id = ?",
            [(now, i) for i in ids],
        )
        self._conn.commit()

    def _on_clear(self) -> None:
        self._gr_rows.clear()
        self._gr_frames.clear()
        self._gr_bogus_pending.clear()
        self._gr_bogus_count = 0
        self._table.setRowCount(0)
        self._ascii_table.setRowCount(0)
        self._row_type.clear()
        self._latest_row.clear()
        self._shown_row.clear()
        self._decode_banner.setVisible(False)
        self._frame_count = 0
        self._lbl_count.setText(_("Frames: 0 received"))

    # ------------------------------------------------------------------ #
    # SatNOGS DB upload (footer controls)
    # ------------------------------------------------------------------ #

    def _refresh_satnogs_toggle(self) -> None:
        """Sync the toggle button's checked state / label / colour from the
        saved ``satnogs_upload_settings``."""
        on = bool(load_satnogs_upload_settings(self._conn).get("enabled"))
        self._btn_satnogs_toggle.blockSignals(True)
        self._btn_satnogs_toggle.setChecked(on)
        self._btn_satnogs_toggle.blockSignals(False)
        self._btn_satnogs_toggle.setText(
            _("SatNOGS Upload: ON") if on else _("SatNOGS Upload: OFF")
        )
        # Green when on; neutral grey when off (grey rather than red so an
        # idle-but-not-broken state does not read as an error).
        colour = "#27ae60" if on else "#7f8c8d"
        self._btn_satnogs_toggle.setStyleSheet(
            f"QPushButton {{ background-color: {colour}; color: white; padding: 3px 10px; }}"
        )

    def _on_satnogs_toggled(self, checked: bool) -> None:
        settings = load_satnogs_upload_settings(self._conn)
        settings["enabled"] = checked
        save_satnogs_upload_settings(self._conn, settings)
        self._refresh_satnogs_toggle()
        if checked:
            self._warn_if_satnogs_unconfigured()

    def _on_satnogs_api(self) -> None:
        settings = load_satnogs_upload_settings(self._conn)
        dlg = _SatnogsApiKeyDialog(str(settings.get("api_key", "")), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            settings["api_key"] = dlg.api_key()
            save_satnogs_upload_settings(self._conn, settings)
            if bool(settings.get("enabled")):
                self._warn_if_satnogs_unconfigured()

    def _warn_if_satnogs_unconfigured(self) -> None:
        """If upload is on but a prerequisite is missing, say so in the status
        label — uploads are otherwise silently skipped."""
        settings = load_satnogs_upload_settings(self._conn)
        missing: list[str] = []
        if not str(settings.get("api_key", "")).strip():
            missing.append(_("API key"))
        if not get_station_callsign(self._conn):
            missing.append(_("callsign"))
        if get_station_latlon(self._conn) is None:
            missing.append(_("station location"))
        if missing:
            self._set_error(
                _("SatNOGS Upload is on but not sending — missing: ") + ", ".join(missing)
            )

    def _active_norad(self) -> int | None:
        """NORAD of the satellite the SatNOGS link should point at: the one
        selected in the active mode's combo, else the main-list selection."""
        mode = self._current_mode()
        combo = (
            self._combo_gr_sat
            if mode == _MODE_GR
            else self._combo_cw_sat
            if mode == _MODE_CW
            else self._combo_hades_sat
            if mode == _MODE_HADES
            else self._combo_afsk_sat
        )
        data = combo.currentData()
        if isinstance(data, int):
            return data
        return self._selected_norad

    def _update_satnogs_link_enabled(self) -> None:
        self._btn_satnogs_link.setEnabled(self._active_norad() is not None)

    def _on_open_satnogs(self) -> None:
        norad = self._active_norad()
        if norad is None:
            return
        self.open_satnogs_requested.emit(norad, self._selected_name or "")

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._on_stop()
        super().closeEvent(event)
