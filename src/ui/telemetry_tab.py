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

import contextlib
import csv
import datetime
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QStandardItemModel
from PySide6.QtWidgets import (
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.engine import (
    AX25_BAUD_MODE_CHOICES,
    AX25_BAUD_SETTING_KEY,
    get_aprs_engine,
    resolve_ax25_modem,
)
from comms.aprs.parser import decode_ax25
from comms.signal_clock import signal_time
from comms.telemetry.cw_frames import (
    decode_cw_frame,
    is_near_miss,
    load_cw_frames,
    normalize_block,
)
from comms.telemetry.cw_upload import (
    SendReport,
    auto_send,
    eligible_unsent,
    ensure_columns,
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
from comms.telemetry.satnogs_uploader import (
    get_satnogs_uploader,
    get_station_callsign,
    get_station_latlon,
    load_satnogs_upload_settings,
    save_satnogs_upload_settings,
)
from i18n import _
from ui.sat_search_dialog import SatSearchDialog

# Named after the backend software, matching _MODE_GR's convention — every
# baud (1200/4800/9600) and connection (Rig+Sound Card or SDR-fed) is now
# decoded by Direwolf itself, so "Direwolf" is accurate for all of them
# (2026-09-13: the SDR+1200 path used to run a from-scratch Python
# tone-detector + PLL + HDLC decoder instead — see comms.aprs.engine's
# module docstring for why that was replaced).
_MODE_AFSK = "Direwolf (AX.25)"
_MODE_GR = "gr-satellites"
# Housekeeping frames sent as Morse-coded hex (ARICA-2 so far); decoded by the CW Decoder tab.
_MODE_CW = "CW TLM"

# Owner tag for the shared AprsEngine singleton (see comms.aprs.engine).
# The APRS tab shares the same engine under its own "aprs" tag so closing
# one tab doesn't stop the other's reception.
_ENGINE_OWNER = "telemetry"


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


# Minimum SNR (dB) at which most frames (~90%) decode, per speed. Measured on
# synthetic AX.25 signals (see docs/communications.md, "デコードに必要な最低SNR
# の目安"): Direwolf through this app's own demodulator, gr-satellites with the
# ACRUX-1 / BOTAN / AmicalSat definitions and a 0.5 s preamble. The SNR is that
# of the signal in its own band: 11 kHz (9600), 5.5 kHz (4800), 10 kHz (1200).
_SNR_GUIDE_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    # speed, Direwolf SNR, gr-satellites SNR, signal band, Direwolf frequency tolerance
    ("1200 bps (AFSK)", "≈ 11 dB", "≈ 10 dB", "10 kHz", "±2 kHz"),
    ("4800 bps (G3RUH)", "≈ 13 dB", "≈ 16+ dB", "5.5 kHz", "±1.5 kHz"),
    ("9600 bps (G3RUH)", "≈ 12 dB", "≈ 14 dB", "11 kHz", "±2 kHz"),
)


def _snr_guide_html() -> str:
    """The body of the "minimum SNR" guide window (translated at call time)."""
    title = _("Minimum SNR needed to decode")
    intro = _(
        "Approximate signal-to-noise ratio (SNR) at which most frames (about 90%) "
        "decode. SNR here is the signal's power compared with the noise in the "
        "signal's own bandwidth (listed for each speed). The app does not show it "
        "as a number: judge it on the waterfall — the signal should stand out "
        "clearly from the noise. Aim for about 3 dB more than the values below."
    )
    col_speed = _("Speed")
    col_band = _("Signal band")
    col_tol = _("Direwolf frequency tolerance")
    rows = "".join(
        f"<tr><td>{speed}</td><td align='center'><b>{dw}</b></td>"
        f"<td align='center'><b>{gr}</b></td><td align='center'>{band}</td>"
        f"<td align='center'>{tol}</td></tr>"
        for speed, dw, gr, band, tol in _SNR_GUIDE_ROWS
    )
    notes = [
        _(
            "Direwolf (AX.25) mode: measured with synthetic AX.25 frames through this "
            "app's own demodulator. The frequency tolerance is how far off centre the "
            "signal may be and still decode (use the SDR Offset to centre it)."
        ),
        _(
            "gr-satellites needs a long preamble: it often misses a burst that starts "
            "with less than about 0.2 s of preamble even at high SNR, whereas Direwolf "
            "decodes a burst with a preamble of about 30 ms. Its frequency tolerance "
            "has not been measured."
        ),
        _(
            "These are estimates from synthetic signals. A real satellite's modulation, "
            "deviation and frame length differ, so treat them as a guide. A signal "
            "weaker than these values will not decode: improve the antenna or LNA, or "
            "wait for a higher-elevation pass."
        ),
        _(
            "Example: 9600 bps bursts seen at +4 to +8 dB (KNACKSAT-2, 2026-09-19) were "
            "too weak for either decoder."
        ),
    ]
    items = "".join(f"<li>{n}</li>" for n in notes)
    return (
        f"<h3>{title}</h3><p>{intro}</p>"
        "<table border='1' cellspacing='0' cellpadding='5'>"
        f"<tr><th>{col_speed}</th><th>Direwolf (AX.25)</th><th>gr-satellites</th>"
        f"<th>{col_band}</th><th>{col_tol}</th></tr>{rows}</table>"
        f"<ul>{items}</ul>"
    )


class _SnrGuideDialog(QDialog):
    """Modeless window explaining the SNR each decoder needs (see _snr_guide_html())."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(_("Decoding SNR guide"))
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self._view = QTextBrowser()
        self._view.setHtml(_snr_guide_html())
        layout.addWidget(self._view)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(_("Close"))
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)


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
        self._selected_name: str = ""

        # CW TLM mode: the CW Decoder tab feeding this one (attach_cw_tab()) and the
        # satellite whose frames are being collected while it runs.
        self._cw_tab: Any = None
        self._cw_tlm_norad: int | None = None
        # True once the "recording start time not set" upload notice was shown this run.
        self._warned_time_unconfirmed = False

        self._frame_count = 0
        self._direwolf_log_window: _ProcessLogDialog | None = None
        self._gr_log_window: _ProcessLogDialog | None = None
        self._snr_guide_window: _SnrGuideDialog | None = None

        self._ensure_db_table()
        self._setup_ui()
        self._load_baud_mode()
        self._connect_signals()
        self._populate_afsk_combo()
        self._populate_cw_combo()
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
        # Upload tracking (satnogs_uploaded_at, time_reliable), added later.
        ensure_columns(self._conn)

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

        row1.addSpacing(12)
        self._baud_combo = QComboBox()
        self._baud_combo.addItem(_("Auto"), "auto")
        self._baud_combo.addItem("1200", "1200")
        self._baud_combo.addItem("4800", "4800")
        self._baud_combo.addItem("9600", "9600")
        self._baud_combo.setToolTip(
            _(
                "AX.25 baud rate for Direwolf (AX.25) mode's Rig + Sound\n"
                "Card (Direwolf) or SDR reception. Auto reads the selected\n"
                "transponder's baud rate from SATNOGS (defaults to 1200 if\n"
                "unknown). Shared with the APRS tab — has no effect on\n"
                "gr-satellites mode."
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
        self._btn_snr_info = QToolButton()
        self._btn_snr_info.setText("ⓘ")
        self._btn_snr_info.setAutoRaise(True)
        self._btn_snr_info.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_snr_info.setToolTip(_("Minimum SNR needed to decode — click for details"))
        self._btn_snr_info.clicked.connect(self._on_snr_info_clicked)
        row1.addWidget(self._btn_snr_info)
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
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
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
        raw_page_layout.addWidget(self._table)
        self._log_tabs.addTab(self._raw_page, _("Received Frames"))

        # "Decoded Fields" page: one inner sub-tab per telemetry ID, built
        # from the satellite's telemetry_ids format definition (if any) and
        # updated live as matching frames arrive. Only satellites with the
        # newer per-ID schema get this tab enabled — see
        # get_telemetry_id_defs().
        self._decode_page = QWidget()
        decode_page_layout = QVBoxLayout(self._decode_page)
        decode_page_layout.setContentsMargins(0, 0, 0, 0)
        self._decode_id_tabs = QTabWidget()
        decode_page_layout.addWidget(self._decode_id_tabs)
        self._log_tabs.addTab(self._decode_page, _("Decoded Fields"))
        self._decode_tables: dict[str, QTableWidget] = {}
        self._decode_field_rows: dict[str, dict[str, int]] = {}
        self._decode_tabs_norad: int | None = None
        self._rebuild_decode_tabs(None)

        root.addWidget(log_box, 1)

        # --- Footer ---
        # Left cluster: SatNOGS DB upload toggle + API-key popup + a link to
        # the selected satellite's SatNOGS page. Right cluster: Clear / Export.
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

        # CW TLM mode only: send frames by hand, or the ones a repeat has confirmed.
        self._btn_satnogs_send = QPushButton(_("Send selected"))
        self._btn_satnogs_send.setToolTip(
            _(
                "Send the CW frames selected in the table to the SatNOGS DB now,\n"
                "even if they were received only once and the Upload switch is off.\n"
                "Frames without a reliable time or already sent are skipped."
            )
        )
        self._btn_satnogs_send.clicked.connect(self._on_send_selected)
        self._btn_satnogs_send.setVisible(False)
        footer.addWidget(self._btn_satnogs_send)
        self._btn_satnogs_send_unsent = QPushButton(_("Send unsent…"))
        self._btn_satnogs_send_unsent.setToolTip(
            _(
                "Send the logged CW frames of this satellite that were not sent yet\n"
                "and were received at least twice (a single reading may be a\n"
                "mis-read digit) with a reliable time."
            )
        )
        self._btn_satnogs_send_unsent.clicked.connect(self._on_send_unsent)
        self._btn_satnogs_send_unsent.setVisible(False)
        footer.addWidget(self._btn_satnogs_send_unsent)

        footer.addStretch()

        self._btn_clear = QPushButton(_("Clear Log"))
        self._btn_clear.clicked.connect(self._on_clear)
        footer.addWidget(self._btn_clear)
        self._btn_export = QPushButton(_("Export CSV…"))
        self._btn_export.clicked.connect(self._on_export_csv)
        footer.addWidget(self._btn_export)
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
        self._selected_norad = norad
        self._selected_name = name
        self._rebuild_decode_tabs(norad)
        if norad:
            for combo in (self._combo_afsk_sat, self._combo_gr_sat, self._combo_cw_sat):
                for i in range(combo.count()):
                    if combo.itemData(i) == norad:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(i)
                        combo.blockSignals(False)
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
        mode = self._current_mode()
        is_gr = mode == _MODE_GR
        is_cw = mode == _MODE_CW
        is_afsk = not is_gr and not is_cw
        self._combo_afsk_sat.setVisible(is_afsk)
        self._btn_afsk_sat_search.setVisible(is_afsk)
        self._combo_gr_sat.setVisible(is_gr)
        self._btn_gr_sat_search.setVisible(is_gr)
        self._combo_cw_sat.setVisible(is_cw)
        self._btn_cw_sat_search.setVisible(is_cw)
        # Baud and the backend log are Direwolf / gr-satellites matters; CW TLM has
        # neither (its text is in the CW Decoder tab).
        for widget in (self._lbl_baud, self._baud_combo, self._btn_backend_log):
            widget.setVisible(not is_cw)
        self._btn_satnogs_send.setVisible(is_cw)
        self._btn_satnogs_send_unsent.setVisible(is_cw)
        # gr-satellites already turns each frame into human-readable text
        # itself (see _on_gr_telemetry()'s "-> Packet from" parsing), so the
        # "Decoded Fields" sub-tab — built from this project's own
        # telemetry_ids format files — only applies to Direwolf (AX.25)
        # mode. Hide the sub-tab bar entirely in gr-satellites mode rather
        # than just disabling the tab, so it reads as a single plain table
        # like before this feature existed.
        self._log_tabs.tabBar().setVisible(not is_gr)
        if is_gr:
            self._log_tabs.setCurrentWidget(self._raw_page)
        if is_cw and self._combo_cw_sat.count():
            # Select the combo's satellite the way changing the combo would, so the
            # satellite list, Radio Control and the Decoded Fields tabs follow.
            self._on_cw_sat_changed(self._combo_cw_sat.currentIndex())
        # SatNOGS DB upload now covers both paths (Phase 2 added the
        # gr-satellites --kiss_server raw-frame route; see
        # _on_gr_raw_frame()), so the upload cluster stays visible in
        # gr-satellites mode too.
        self._refresh_status()

    def _on_afsk_sat_changed(self, _index: int) -> None:
        norad = self._combo_afsk_sat.currentData()
        self._update_satnogs_link_enabled()
        if norad is not None:
            self.satellite_selected.emit(int(norad), "afsk")

    def _on_gr_sat_changed(self, _index: int) -> None:
        norad = self._combo_gr_sat.currentData()
        if norad is not None:
            self.satellite_selected.emit(int(norad), "gr")

    def _on_cw_sat_changed(self, _index: int) -> None:
        norad = self._combo_cw_sat.currentData()
        self._update_satnogs_link_enabled()
        if norad is not None:
            self.satellite_selected.emit(int(norad), "cw_tlm")

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

    # ------------------------------------------------------------------ #
    # AX.25 baud mode (shared with the APRS tab)
    # ------------------------------------------------------------------ #

    def _load_baud_mode(self) -> None:
        """Restore the Auto/1200/4800/9600 selection from app_settings."""
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
        mode = self._baud_combo.currentData()
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (AX25_BAUD_SETTING_KEY, mode),
            )
            self._conn.commit()
        self._apply_baud_change()

    def _on_snr_info_clicked(self) -> None:
        """Open (or bring forward) the window explaining the SNR each decoder needs."""
        if self._snr_guide_window is None:
            self._snr_guide_window = _SnrGuideDialog(self)
        self._snr_guide_window.show()
        self._snr_guide_window.raise_()
        self._snr_guide_window.activateWindow()

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
                self._direwolf_log_window = _ProcessLogDialog(
                    _("Direwolf Log"), direwolf_log_path(), self
                )
            else:
                self._direwolf_log_window.reload()
            window = self._direwolf_log_window
        window.show()
        window.raise_()
        window.activateWindow()

    def _on_transmitter_changed(self, _xpdr: object) -> None:
        """Restart the AX.25 pipeline if the newly selected transponder's baud differs."""
        self._apply_baud_change()

    def _apply_baud_change(self) -> None:
        """Re-resolve the target baud and apply it to whichever source is active.

        Rig + Sound Card (Direwolf) sessions are restarted in place via
        restart_if_modem_changed(); SDR-fed Direwolf sessions via
        sync_sdr_baud(). Both methods only act on the session type they
        own, so calling both unconditionally is safe — whichever doesn't
        apply is a no-op.
        """
        modem = resolve_ax25_modem(self._conn, self._radio_control)
        self._engine.restart_if_modem_changed(modem)
        if self._sdr_pipeline is not None:
            self._engine.sync_sdr_baud(self._sdr_pipeline, modem)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    def _on_start(self) -> None:
        self._warned_time_unconfirmed = False
        mode = self._current_mode()
        if mode == _MODE_CW:
            if not self._start_cw_tlm():
                return
        elif mode == _MODE_GR:
            self._start_gr_satellites()
        else:
            self._try_start_afsk()
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

    def _on_stop(self) -> None:
        self._stop_gr_satellites()
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
        self._cw_tab = cw_tab
        cw_tab.frame_block_ready.connect(self._on_cw_block)

    def _start_cw_tlm(self) -> bool:
        """Ask MainWindow to open and start the CW Decoder tab. False if it cannot be done."""
        norad = self._combo_cw_sat.currentData()
        if not isinstance(norad, int):
            self._set_error("⚠ " + _("Select a satellite first."))
            return False
        if load_cw_frames(norad) is None:
            self._set_error(
                "⚠ "
                + _(
                    "No CW telemetry frame format is defined for this satellite yet "
                    "(ARICA-2 only so far)."
                )
            )
            return False
        if self._decode_tabs_norad != norad:
            self._rebuild_decode_tabs(norad)
        self._cw_tlm_norad = norad
        self.cw_tlm_start_requested.emit()
        self._lbl_status.setText(_("CW TLM — decoding in the CW Decoder tab"))
        self._lbl_status.setStyleSheet("color: #27ae60;")
        return True

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
        fmt = load_format(norad) or {}
        callsign = str(fmt.get("callsign", ""))
        sat_name = str(fmt.get("name", f"NORAD {norad}"))
        ts = end if isinstance(end, datetime.datetime) else datetime.datetime.now(datetime.UTC)
        reliable_fn = getattr(self._cw_tab, "signal_time_reliable", None)
        reliable = bool(reliable_fn()) if callable(reliable_fn) else True

        result = decode_cw_frame(norad, text)
        if result is None:
            if is_near_miss(norad, text):
                note = _("length or characters not valid, not used")
                self._append_row(
                    callsign=callsign,
                    sat_name=sat_name,
                    data=f"[?] {normalize_block(text)}  ({note})",
                    norad=norad,
                    ts=ts,
                    dim=True,
                )
            return
        if not result.valid:
            self._append_row(
                callsign=callsign,
                sat_name=sat_name,
                data=f"[?] {result.key} {result.hex_text}  ({'; '.join(result.problems)})",
                norad=norad,
                ts=ts,
                dim=True,
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
        log_id = self._persist_frame(tf, ts, reliable)
        self._append_row(
            callsign=callsign,
            sat_name=sat_name,
            data=f"[{result.key}] {result.hex_text}",
            norad=norad,
            ts=ts,
            log_id=log_id,
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
        report = auto_send(self._conn, get_satnogs_uploader(), norad, log_id)
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
        norad = self._active_norad()
        ids = self._selected_log_ids()
        if norad is None or not ids:
            self._set_error(_("Select received CW frames in the table first."))
            return
        report = send_frames(
            self._conn, get_satnogs_uploader(), norad, ids, force=True, require_repeat=False
        )
        self._show_send_report(report)

    def _on_send_unsent(self) -> None:
        norad = self._active_norad()
        if norad is None:
            self._set_error(_("Select a satellite first."))
            return
        frames = eligible_unsent(self._conn, norad)
        if not frames:
            self._lbl_status.setText(
                _(
                    "Nothing ready to send: a frame needs a second reception and a "
                    "reliable time, and must not be sent already."
                )
            )
            self._lbl_status.setStyleSheet("color: #aaa;")
            return
        question = _(
            "Send {n} logged frame(s) ({m} different) of {name} to the SatNOGS DB?\n\n"
            "Only frames received at least twice, with a reliable time, that were "
            "not sent before are included."
        ).format(
            n=len(frames), m=len({f.raw_hex for f in frames}), name=self._selected_name or norad
        )
        answer = QMessageBox.question(self, _("Send to SatNOGS"), question)
        if answer != QMessageBox.StandardButton.Yes:
            return
        report = send_frames(
            self._conn,
            get_satnogs_uploader(),
            norad,
            [f.id for f in frames],
            force=True,
            require_repeat=True,
        )
        self._show_send_report(report)

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
        if automatic and not report.queued and not report.unreliable:
            return
        parts: list[str] = []
        if report.queued:
            parts.append(_("{n} queued for upload").format(n=report.queued))
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

        ok, err = self._gr_backend.start(
            norad, samp_rate, pipeline, catalog_norad=self._gr_catalog_ids.get(int(norad))
        )
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
            info = get_satellite_info(
                self._gr_catalog_ids.get(self._selected_norad, self._selected_norad)
            )
            sat_name = str(info.get("name", "")) if info else ""
        data_text = "  |  ".join(data_lines) if data_lines else text[:120]

        self._append_row(
            callsign=callsign or sat_name or "—",
            sat_name=sat_name or "—",
            data=data_text,
            norad=self._selected_norad,
            ts=self._frame_time()[0],
        )

    def _on_gr_raw_frame(self, raw: bytes) -> None:
        """Forward a gr-satellites --kiss_server data frame to SatNOGS DB.

        No-op unless the footer toggle is on and callsign / location / API
        key are all set (see SatnogsUploader.submit()). The subprocess
        targets exactly one satellite per run, so its NORAD is
        ``started_norad`` rather than something resolved per-frame — mirrors
        _on_ax25_frame()'s forwarding for the AFSK/Direwolf path.
        """
        norad = self._gr_backend.started_norad
        if norad is None:
            return
        when, reliable = self._frame_time()
        self._submit_raw_frame(raw, norad, when, reliable)

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
        """Start AX.25 reception on the SDR pipeline (receive only).

        Direwolf's own built-in decoder for the resolved baud does the
        actual demod, fed by SDR-derived audio — see
        AprsEngine.start_sdr_direwolf().
        """
        modem = resolve_ax25_modem(self._conn, self._radio_control)
        ok, err = self._engine.start_sdr_direwolf(_ENGINE_OWNER, pipeline, modem=modem)
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
            return
        norad = self._callsign_to_norad(frame.src)
        tf = decode_telemetry(frame.src, frame.payload, norad)
        when, reliable = self._frame_time()
        self._append_row(
            callsign=tf.callsign,
            sat_name=tf.satellite_name,
            data=tf.summary(),
            norad=tf.norad,
            ts=when,
        )
        self._update_decode_tab(tf)
        self._persist_frame(tf, when, reliable)
        # Forward the raw frame (full AX.25 frame, FCS already stripped by the
        # demodulator / KISS) to the SatNOGS DB. No-op unless the footer
        # toggle is on and callsign / location / API key are all set.
        self._submit_raw_frame(raw, norad, when, reliable)

    def _frame_time(self) -> tuple[datetime.datetime, bool]:
        """(UTC time of the frame just decoded, time trustworthy?).

        The wall clock live; for a played-back IQ recording the recording's start
        time plus the playback position (comms.signal_clock). A recording whose
        start time is only a placeholder gives an untrustworthy time.
        """
        return signal_time(self._sdr_pipeline)

    def _submit_raw_frame(
        self, raw: bytes, norad: int | None, when: datetime.datetime, reliable: bool
    ) -> None:
        """Queue *raw* for the SatNOGS DB with its real reception time.

        A frame whose time is only a placeholder (a recording with no start time
        yet) is not sent -- it would be published with a wrong time. The user is
        told once per run.
        """
        if reliable:
            get_satnogs_uploader().submit(self._conn, raw, norad, when)
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
        ts: datetime.datetime | None = None,
        dim: bool = False,
        log_id: int | None = None,
    ) -> None:
        """Add a row to the Received Frames table.

        *ts* is the time to show (UTC, with the date -- a played-back recording
        can be from any day); the default is now. *dim* greys the row out and
        leaves it out of the frame count (a rejected CW candidate). *log_id* is
        the frame's ``telemetry_log`` id, kept on the row for "Send selected".
        """
        when = ts if ts is not None else datetime.datetime.now(datetime.UTC)
        row = self._table.rowCount()
        self._table.insertRow(row)
        for column, text in enumerate(
            (when.strftime("%Y-%m-%d %H:%M:%S"), callsign, sat_name, data)
        ):
            item = QTableWidgetItem(text)
            if dim:
                item.setForeground(QBrush(QColor("#888888")))
            if column == 0 and log_id is not None:
                item.setData(Qt.ItemDataRole.UserRole, log_id)
            self._table.setItem(row, column, item)
        self._table.scrollToBottom()
        if not dim:
            self._frame_count += 1
            self._lbl_count.setText(_("Frames: ") + str(self._frame_count) + _(" received"))

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

        id_defs = get_telemetry_id_defs(norad)
        decode_tab_index = self._log_tabs.indexOf(self._decode_page)
        self._log_tabs.setTabEnabled(decode_tab_index, bool(id_defs))
        if not id_defs:
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
            table.setAlternatingRowColors(True)
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
            self._decode_tables[id_str] = table
            self._decode_field_rows[id_str] = row_map
            self._decode_id_tabs.addTab(table, id_def.get("label", f"ID{id_str}"))

    def _update_decode_tab(self, tf: TelemetryFrame) -> None:
        """Push *tf*'s decoded field values into its "Decoded Fields" sub-tab.

        No-op unless tf carries a recognized telemetry ID for the satellite
        the sub-tabs are currently built for (_rebuild_decode_tabs() is
        called from set_satellite(), so this tracks the main satellite list
        selection, not necessarily the AFSK combo).
        """
        if tf.telemetry_id is None or not tf.has_fields or tf.norad != self._decode_tabs_norad:
            return
        id_str = str(tf.telemetry_id)
        table = self._decode_tables.get(id_str)
        row_map = self._decode_field_rows.get(id_str)
        if table is None or row_map is None:
            return
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

    def _persist_frame(
        self, tf: TelemetryFrame, ts: datetime.datetime, reliable: bool = True
    ) -> int | None:
        """Log *tf*; returns its ``telemetry_log`` id. *reliable* is False when *ts*
        comes from a placeholder recording start time (such frames are never sent)."""
        if not hasattr(self._conn, "execute"):
            return None
        parsed = (
            json.dumps({f.name: {"value": f.scaled_value, "unit": f.unit} for f in tf.fields})
            if tf.fields
            else None
        )
        cursor = self._conn.execute(
            """INSERT INTO telemetry_log
               (received_at, norad_cat_id, callsign, raw_hex, parsed_json, time_reliable)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts.isoformat(), tf.norad, tf.callsign, tf.raw_hex, parsed, int(reliable)),
        )
        self._conn.commit()
        row_id = cursor.lastrowid
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
        if self._afsk_source == "direwolf" and self._engine.is_running:
            modem = self._engine.current_modem
            suffix = f"  [{modem} baud]" if modem else ""
            self._lbl_status.setText(_("Rig + Direwolf (receiving)") + suffix)
            self._lbl_status.setStyleSheet("color: #27ae60;")
        elif self._afsk_source == "sdr_direwolf" and self._engine.is_running:
            modem = self._engine.current_modem
            suffix = f"  [{modem} baud]" if modem else ""
            self._lbl_status.setText(_("SDR — Direwolf (AX.25) (receive only)") + suffix)
            self._lbl_status.setStyleSheet("color: #4a9eff;")
        else:
            # gr-satellites is SDR-only (no Rig + Sound Card path, unlike
            # Direwolf mode above), so its idle hint must not mention Rig.
            if self._current_mode() == _MODE_GR:
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
