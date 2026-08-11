"""
Main window

MainWindow     — Qt6 application main window (QMainWindow)
SatDetailPanel — selected satellite detail info panel
PassListPanel  — pass prediction list panel
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import httpx
from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from comms import mode_detection
from comms.audio_device_manager import get_audio_device_manager
from core.autotrack import AutotrackManager
from core.celestial_engine import MOON_ID, CelestialEngine
from core.clock_offset import set_clock_offset
from core.doppler_worker import DopplerWorker
from core.engine import DopplerCalculator, Observation, PassPredictor, SatelliteEngine
from core.location import LocationManager
from core.notifier import PassNotifier
from core.ntp_check import check_system_clock
from data.amsat_status import AMSATStatusFetcher
from data.ctcss_db import get_ctcss
from data.tle_manager import TLEManager
from data.transmitter_manager import TransmitterManager
from i18n import _
from rig.controller import (
    _FT991_DIRECT_MODEL_IDS,
    _FTX1_MODEL_IDS,
    _IC705_DEFAULT_CIV_ADDR,
    _IC705_MODEL_IDS,
    CTCSS_PRESET_TEMPLATES,
    HamlibDirectController,
    HamlibNetController,
    HamlibRotatorController,
    RigControlError,
    RigController,
    RotatorController,
    normalize_civ_addr,
)
from ui.dashboard_view import DashboardView
from ui.pass_chart import GroupPassChartView, PassChartView
from ui.pass_panel import PassPanel
from ui.radar_view import SAT_COLORS, RadarView, SatTrackData
from ui.radio_control_widget import RadioControlWidget
from ui.world_map import WorldMapView

logger = logging.getLogger(__name__)


class _SatData(TypedDict):
    """Satellite list display data (used for filtering)."""

    norad: int
    name: str
    alt_names: str  # JSON array of alias strings from SatNOGS
    is_favorite: bool
    is_hidden: int  # 0=visible, 1=user-hidden, 2=system-hidden
    status: str
    tle_group: str
    amsat_status: str | None
    tle_no_result_since: str | None  # set when no TLE found; shown yellow in list
    favorite_group: int  # 0 = not in any group, 1..N = custom group id


# Regular expression to extract AMSAT designators like AO-91, FO-29, CAS-4A
# 2-4 character prefix + optional separator + 1-3 digit number + optional trailing character
_DESIG_RE = re.compile(r"\b([A-Za-z]{2,4})[-\s]?(\d{1,3}[A-Za-z]?)\b")

# Mode inversion table for inverting transponders (invert=True).
# Downlink and uplink use opposite sidebands so that when the operator
# tunes up on one VFO the other VFO moves in the opposite direction.
_MODE_INVERT: dict[str, str] = {
    "USB": "LSB",
    "LSB": "USB",
    "CW": "CW-R",
    "CW-R": "CW",
    "USB-D": "LSB-D",
    "LSB-D": "USB-D",
}

# Direct-mode models with their own dedicated raw-CAT CTCSS path
# (_apply_mode_and_ctcss_cat_ftx1 / _apply_mode_and_ctcss_cat_ft991 in
# controller.py) that never consults the ctcss_method setting. Any other
# Direct-mode HamlibDirectController that is not in satmode is a "generic"
# rig (e.g. IC-705): ctcss_method is a NET-mode-oriented setting that
# doesn't apply to it, so its CTCSS always goes through standard Hamlib
# regardless of what's stored in settings (avoids stale NET-mode values
# leaking in when a rig slot is reconfigured from NET to Direct).
_RAW_CAT_MODEL_IDS: frozenset[int] = _FTX1_MODEL_IDS | _FT991_DIRECT_MODEL_IDS

# Lock (L button) dial feedback thresholds -- see MainWindow._lock_watch_cycle().
# Ignore implausible single-cycle jumps above this. Originally 50_000.0, but a
# real linear-transponder pass (FO-29, ~100kHz-wide passband) showed a
# deliberate, large manual retune across the passband getting rejected as
# "implausible" -- raised to comfortably cover a full-passband sweep on any
# known amateur transponder (widest are ~130kHz) while still catching a
# genuinely wrong read (confirmed live, 2026-07-22).
_DIAL_FEEDBACK_SANITY_HZ = 200_000.0
# If a DL reading is this close to the same poll's UL reading, rigctld's
# "current VFO" pointer is almost certainly stuck on Sub (confirmed live,
# 2026-07-15, FTX-1F) -- discard the reading rather than act on it. DL and
# UL are always tens of MHz apart for real transponders, so this threshold
# is far too generous to ever reject a genuine reading.
_DIAL_FEEDBACK_CROSSCHECK_HZ = 1000.0


def _is_generic_direct_rig(rig: RigController) -> bool:
    """True for a Direct-mode rig with no dedicated CTCSS path of its own."""
    return (
        isinstance(rig, HamlibDirectController)
        and not rig._satmode
        and rig._model_id not in _RAW_CAT_MODEL_IDS
    )


# Oscar designator prefixes (e.g. AO-7, FO-29, IO-86, QO-100, RS-44, RS95S)
# Hyphen is optional to handle SatNOGS alt_names stored without it (e.g. "RS95S").
# Two capturing groups: (prefix, number+suffix) so the display can normalise to "RS-95S".
_OSCAR_RE = re.compile(
    r"\b((?:AO|BO|CO|DO|EO|FO|GO|HO|IO|JO|KO|LO|MO|NO|PO|QO|RS|SO|TO|UO|VO|XO|ZO))"
    r"-?(\d+[A-Z]?)\b",
    re.IGNORECASE,
)


def _extract_designators(name: str) -> set[str]:
    """Extract and normalize AMSAT designators from a satellite name (e.g. 'AO-91' -> {'ao91'})."""
    return {(m.group(1) + m.group(2)).lower() for m in _DESIG_RE.finditer(name)}


def _amsat_key_in_sat_name(amsat_key: str, sat_name_lower: str) -> bool:
    """Check whether an AMSAT key appears as a complete token within a satellite name.

    Only matches where the key is not adjacent to alphanumeric characters are accepted.
    Example: "iss" -> "iss (zarya)" matches; "ao-7" -> "ao-73" does not.
    """
    pattern = r"(?<![a-z0-9])" + re.escape(amsat_key) + r"(?![a-z0-9])"
    return bool(re.search(pattern, sat_name_lower))


# ---------------------------------------------------------------------------
# SatDetailPanel
# ---------------------------------------------------------------------------


class SatDetailPanel(QWidget):
    """
    Panel that displays selected satellite details (elevation, azimuth, range,
    range rate, visibility) using a QFormLayout.

    Also hosts the "Comms Quick Panel": a mini radar, an optional satellite
    quick-select combo, an optional Doppler-corrected DL/UL frequency mirror,
    and Rig 1 / Rig 2 / Rotator connect proxy buttons. This section is hidden
    unless a Communications tab (FT4, APRS, ...) is active — see
    MainWindow._on_tab_changed() and comms.mode_detection.COMMS_TAB_CONFIG.
    """

    # Emitted when the user picks a satellite from the Quick Panel's Input
    # Source combo. Args: tab_key (e.g. "ft4"), norad_cat_id.
    comms_satellite_requested: Signal = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radio_control: Any = None
        self._active_comms_tab: str | None = None
        self._active_tab_widget: QWidget | None = None
        self._freq_source: str | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        group = QGroupBox(_("Satellite Detail"))
        form = QFormLayout(group)

        self._name_label = QLabel("—")
        self._norad_label = QLabel("—")
        self._el_label = QLabel("—")
        self._az_label = QLabel("—")
        self._range_label = QLabel("—")
        self._rate_label = QLabel("—")
        self._vis_label = QLabel("—")

        form.addRow(_("Name:"), self._name_label)
        form.addRow(_("NORAD:"), self._norad_label)
        form.addRow(_("Elevation:"), self._el_label)
        form.addRow(_("Azimuth:"), self._az_label)
        form.addRow(_("Range:"), self._range_label)
        form.addRow(_("Range rate:"), self._rate_label)
        form.addRow(_("Visible:"), self._vis_label)

        layout.addWidget(group)

        # ── Comms Quick Panel (hidden until a Communications tab is active) ──
        self._comms_group = QGroupBox(_("Quick Comms Control"))
        comms_lay = QVBoxLayout(self._comms_group)
        comms_lay.setContentsMargins(4, 4, 4, 4)
        comms_lay.setSpacing(4)

        self._mini_radar = RadarView(compact=True)
        self._mini_radar.setMaximumHeight(200)
        comms_lay.addWidget(self._mini_radar)

        self._input_source_row = QWidget()
        src_lay = QHBoxLayout(self._input_source_row)
        src_lay.setContentsMargins(0, 0, 0, 0)
        src_lay.addWidget(QLabel(_("Input:")))
        self._input_source_combo = QComboBox()
        # activated() (not currentIndexChanged()) — the latter does not fire
        # when the user re-picks the item that is already current, which is
        # exactly the first (alphabetically/NORAD-sorted first) entry shown
        # right after the combo is populated, silently no-oping the most
        # common click.
        self._input_source_combo.activated.connect(self._on_input_source_changed)
        src_lay.addWidget(self._input_source_combo, stretch=1)
        comms_lay.addWidget(self._input_source_row)

        self._quick_dl_label = QLabel("D: —")
        self._quick_ul_label = QLabel("U: —")
        comms_lay.addWidget(self._quick_dl_label)
        comms_lay.addWidget(self._quick_ul_label)

        rig_btn_row = QHBoxLayout()
        self._quick_rig1_btn = QPushButton(_("Rig 1"))
        self._quick_rig1_btn.clicked.connect(self._on_quick_connect_rig1)
        self._quick_rig2_btn = QPushButton(_("Rig 2"))
        self._quick_rig2_btn.clicked.connect(self._on_quick_connect_rig2)
        rig_btn_row.addWidget(self._quick_rig1_btn)
        rig_btn_row.addWidget(self._quick_rig2_btn)
        comms_lay.addLayout(rig_btn_row)

        self._quick_rot_btn = QPushButton(_("Rotator"))
        self._quick_rot_btn.clicked.connect(self._on_quick_connect_rotator)
        comms_lay.addWidget(self._quick_rot_btn)

        layout.addWidget(self._comms_group)
        self._comms_group.setVisible(False)

        layout.addStretch()

    def set_satellite(self, norad: int, name: str) -> None:
        """Set basic info for the selected satellite."""
        self._name_label.setText(name)
        self._norad_label.setText("—" if norad == MOON_ID else str(norad))

    def update_observation(self, obs: Observation | None) -> None:
        """Update observation values. Sets '—' for all fields when obs is None."""
        if obs is None:
            self._clear_obs_fields()
            return
        self._el_label.setText(f"{obs.elevation_deg:.2f}°")
        self._az_label.setText(f"{obs.azimuth_deg:.2f}°")
        self._range_label.setText(f"{obs.range_km:.1f} km")
        self._rate_label.setText(f"{obs.range_rate_km_s:.3f} km/s")
        self._vis_label.setText(_("Visible") if obs.is_above_horizon else _("Below horizon"))

    def clear(self) -> None:
        """Reset all fields."""
        self._name_label.setText("—")
        self._norad_label.setText("—")
        self._clear_obs_fields()

    def _clear_obs_fields(self) -> None:
        for label in (
            self._el_label,
            self._az_label,
            self._range_label,
            self._rate_label,
            self._vis_label,
        ):
            label.setText("—")

    # ------------------------------------------------------------------ #
    # Comms Quick Panel
    # ------------------------------------------------------------------ #

    def bind_radio_control(self, radio_control: Any) -> None:
        """Store a RadioControlWidget reference for the freq mirror and
        Rig 1 / Rig 2 / Rotator connect-button proxies."""
        self._radio_control = radio_control

    def set_active_comms_tab(
        self,
        tab_key: str,
        satellite_options: list[tuple[int, str]],
        tab_widget: QWidget | None = None,
    ) -> None:
        """Show the Comms Quick Panel configured for *tab_key*.

        satellite_options: (norad, name) pairs offered in the Input Source
        combo (ignored when the tab's config has show_input_source=False).
        tab_widget: the open Communications tab instance itself, used for
        freq_source="satdump" (reads tab_widget.current_rx_frequency_mhz()).
        """
        config = mode_detection.COMMS_TAB_CONFIG.get(tab_key)
        self._active_comms_tab = tab_key
        self._active_tab_widget = tab_widget
        self._freq_source = config.freq_source if config else None
        self._comms_group.setVisible(True)

        show_input = bool(config and config.show_input_source)
        self._input_source_row.setVisible(show_input)
        if show_input:
            self._input_source_combo.blockSignals(True)
            self._input_source_combo.clear()
            for norad, name in satellite_options:
                self._input_source_combo.addItem(name, norad)
            self._input_source_combo.blockSignals(False)

        show_freq = self._freq_source is not None
        self._quick_dl_label.setVisible(show_freq)
        self._quick_ul_label.setVisible(show_freq)
        self.refresh_freq_mirror()

    def deactivate_comms_panel(self) -> None:
        """Hide the Comms Quick Panel (called when a resident tab becomes active)."""
        self._active_comms_tab = None
        self._active_tab_widget = None
        self._comms_group.setVisible(False)

    def update_radar_track(self, track: SatTrackData) -> None:
        """Forward the current satellite track to the mini radar (skipped while hidden)."""
        if self._active_comms_tab is not None:
            self._mini_radar.set_tracks([track])

    def refresh_freq_mirror(self) -> None:
        """Refresh the D:/U: readout from the configured freq_source.

        Called once per second from MainWindow._on_tick() while the Quick
        Panel is active; a no-op otherwise.
        """
        if self._active_comms_tab is None:
            return
        if self._freq_source == "radio_control" and self._radio_control is not None:
            self._quick_dl_label.setText(f"D: {self._radio_control._downlink_label.text()}")
            self._quick_ul_label.setText(f"U: {self._radio_control._uplink_label.text()}")
        elif self._freq_source == "satdump":
            freq_mhz = None
            getter = getattr(self._active_tab_widget, "current_rx_frequency_mhz", None)
            if callable(getter):
                freq_mhz = getter()
            self._quick_dl_label.setText(
                f"D: {freq_mhz:.6f} MHz" if freq_mhz is not None else "D: —"
            )
            self._quick_ul_label.setText("U: — (RX only)")

    def _on_input_source_changed(self, index: int) -> None:
        if self._active_comms_tab is None or index < 0:
            return
        norad = self._input_source_combo.itemData(index)
        if norad is not None:
            self.comms_satellite_requested.emit(self._active_comms_tab, int(norad))

    def _on_quick_connect_rig1(self) -> None:
        if self._radio_control is not None:
            self._radio_control._connect_rig1_btn.click()

    def _on_quick_connect_rig2(self) -> None:
        if self._radio_control is not None:
            self._radio_control._connect_rig2_btn.click()

    def _on_quick_connect_rotator(self) -> None:
        if self._radio_control is not None:
            self._radio_control._connect_rot_btn.click()


@dataclass(frozen=True)
class DopplerDisplayUpdate:
    """Result of one DopplerWorker cycle, carried to the UI thread via the
    _doppler_computed signal — see MainWindow._doppler_cycle()."""

    dl_nom: float | None
    dl_corr: float | None
    dl_shift: float | None
    ul_nom: float | None
    ul_corr: float | None
    ul_shift: float | None
    mode: str | None
    ctcss_display: float | None


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """
    FBSAT59 main window.

    Layout:
        Left   — satellite list (with TLE quality indicator)
        Centre — tabs (World Map / Radar / Pass Chart)
        Right  — selected satellite detail panel
        Bottom — pass prediction list
    """

    # Signal used to safely call _load_satellites from a background thread.
    # QTimer.singleShot does not fire in threads without an event loop, so a Signal is used.
    _satellite_list_refresh: Signal = Signal()
    # Signal used to pass rig control errors from a background thread to the UI thread.
    _rig_error: Signal = Signal(str)
    # Signal used to pass SATNOGS sync results from a background thread to the status bar.
    _satnogs_status: Signal = Signal(str)
    # Signals used by the SatNOGS UUID background fetch to update the UI thread.
    _satnogs_open_url: Signal = Signal(str)
    _satnogs_not_found: Signal = Signal()
    # Distinct from _satnogs_not_found: fired when the SatNOGS API itself could
    # not be reached at all (network/timeout/HTTP error), so the user isn't
    # told "not found" for a satellite that may well exist on SatNOGS.
    _satnogs_network_error: Signal = Signal(str)
    # Signal used to pass rotator position from a background thread to the UI thread.
    _rot_pos_updated: Signal = Signal(float, float)
    # Signal fired from the download thread when the default NASA map has been saved.
    _map_downloaded: Signal = Signal()
    # Signal to update sync progress label from a background thread (empty string = hide).
    _sync_progress: Signal = Signal(str)
    # Signal fired from the startup NTP check thread when no NTP server could
    # be reached at all (so the offset below could not be measured/applied),
    # carrying the dialog message.
    _ntp_check_failed: Signal = Signal(str)
    # Signal fired from the startup NTP check thread once an offset has been
    # measured and applied via core.clock_offset, carrying a short status-bar
    # message. Only emitted when the drift exceeds the warning threshold —
    # informational only, since FT4/Q65 timing has already been compensated.
    _ntp_offset_applied: Signal = Signal(str)
    # Signal used to pass a freshly computed Doppler result (DopplerDisplayUpdate)
    # from DopplerWorker's own thread to the UI thread — see _doppler_cycle().
    _doppler_computed: Signal = Signal(object)
    # Passes a freshly recomputed SDR Lock offset (Hz) from _rig_send()'s /
    # _rig2_send()'s background thread to the UI thread, so
    # SdrControlWidget's offset label can be updated safely — see
    # _doppler_cycle()'s SDR Lock branch.
    _sdr_lock_offset_computed: Signal = Signal(float)

    def __init__(
        self,
        conn: sqlite3.Connection,
        tle_manager: TLEManager,
        engine: SatelliteEngine | None = None,
        pass_predictor: PassPredictor | None = None,
        location_manager: LocationManager | None = None,
        fastapi_app: Any | None = None,
        web_port: int = 8080,
        rig_state: Any | None = None,
    ) -> None:
        """
        Args:
            conn:             SQLite connection
            tle_manager:      TLE manager
            engine:           satellite engine (no position updates if None)
            pass_predictor:   pass predictor (no pass prediction if None)
            location_manager: location manager (QTH shown as unset if None)
            fastapi_app:      FastAPI app (web server not started if None)
            web_port:         web server port number
            rig_state:        shared RigWebState (written every tick for mobile UI)
        """
        super().__init__()
        self._conn = conn
        self._tle_manager = tle_manager
        self._engine = engine
        self._pass_predictor = pass_predictor
        self._location_manager = location_manager
        self._selected_norad: int | None = None
        self._all_norads: list[int] = []  # ALL non-hidden norads (for pass predictor)
        self._visible_norads: list[int] = []  # norads currently shown in the list widget
        self._all_sat_data: list[_SatData] = []
        self._current_passes: list[Any] = []
        # Satellite name cache — rebuilt in _load_satellites, used every tick
        self._sat_name_cache: dict[int, str] = {}
        # World-map update throttle: only redraw every N ticks (default 5 s at 1 Hz).
        # With the "All Satellites" filter (typically 1000+ satellites) the
        # map is too cluttered to read individual dots anyway, so it's
        # throttled far more aggressively (60s) — this is on top of, not a
        # replacement for, _is_map_tab_active()'s "skip entirely unless
        # Dashboard/World Map is visible" gate (2026-07-10).
        self._map_tick_counter: int = 0
        self._MAP_UPDATE_INTERVAL: int = 5
        self._MAP_UPDATE_INTERVAL_ALL_SATELLITES: int = 60
        # Latest elevations computed in _update_world_map, reused by _check_autotrack
        self._last_elevations: dict[int, float] = {}
        self._current_transmitter: dict[str, Any] | None = None
        # Latest Doppler result from DopplerWorker's own thread (see
        # _doppler_cycle/_on_doppler_computed) — reset on transponder change
        # so a stale value from the previous selection is never shown.
        self._latest_doppler: DopplerDisplayUpdate | None = None
        # Maps a non-resident Communications tab widget -> its mode_detection
        # tab key (e.g. "ft4"), used by _on_tab_changed to drive the Comms
        # Quick Panel and to auto-resize the pass-prediction splitter.
        self._comms_tab_keys: dict[QWidget, str] = {}
        # Pass-prediction splitter sizes saved just before shrinking it for a
        # Communications tab; restored when returning to a resident tab.
        self._pass_panel_saved_sizes: list[int] | None = None
        # View > Wide Tab: manual override that shrinks the satellite list
        # and pass-prediction panels regardless of which tab is active, for
        # more room while operating a Communications tab (FT4, SDR Control,
        # etc.). Not persisted across restarts — always starts OFF.
        self._wide_tab_enabled: bool = False
        # h_splitter sizes saved just before Wide Tab mode shrinks the
        # satellite list panel; restored when Wide Tab is turned back off.
        self._wide_tab_saved_h_sizes: list[int] | None = None
        self._web_server: Any | None = None
        self._web_server_url: str = ""
        # Celestial body tracking (Moon, etc.)
        self._celestial_engine: CelestialEngine = CelestialEngine()
        # Cached 24-hour arc track for the Moon (updated every MAP_UPDATE_INTERVAL ticks)
        self._moon_arc_cache: list[tuple[float, float]] = []
        self._scheduler: Any | None = None
        # Guards _fetch_all_tle_sources(): pressing Update TLE while a fetch
        # is already running (nothing else visibly happens for the several
        # seconds to minutes the initial 6-group bulk loop can take with no
        # progress feedback of its own) used to start a second, fully
        # independent _fetch_all() thread racing the first one over the same
        # 6 sources -- confirmed via a real log, 2026-08-10.
        self._tle_fetch_in_progress = False
        # Set to True in closeEvent so background threads stop gracefully
        self._shutdown_flag = threading.Event()
        self._amsat_fetcher = AMSATStatusFetcher(conn)
        self._transmitter_manager = TransmitterManager(conn)
        self._rig_controller: RigController | None = None
        self._rig2_controller: RigController | None = None
        self._rotator_controller: RotatorController | None = None
        self._ctcss_method: str = "hamlib"
        self._ctcss_cat_on: str = ""
        self._ctcss_cat_off: str = ""
        # Generation counter to cancel superseded _do_nonsatmode() threads.
        self._nonsatmode_gen: int = 0
        # Lock indicating whether the rig control thread is currently running.
        # If acquire(blocking=False) fails, the previous cycle is still executing.
        self._rig_busy_lock = threading.Lock()
        # Non-blocking lock for Rig 2 (same pattern as Rig 1).
        self._rig2_busy_lock = threading.Lock()
        # Same pattern for rotator set_position calls.
        self._rot_busy_lock = threading.Lock()
        # When True, AZ sent to the rotator is offset by 180° (south-initialized rotator).
        self._rotator_south_init: bool = False
        # Cache for forced frequency transmission when the Tune button resets to centre frequency.
        # None -> use the Doppler-corrected value as-is.
        # A value -> transmit it once then reset to None.
        self._tune_dl_override: float | None = None
        self._tune_ul_override: float | None = None
        # Passband tune offset applied to SDR (Rig 2) DL, and mirrored to
        # Rig 1 UL when Lock is active (sign reversed for inverted transponders).
        self._sdr_tune_offset: float = 0.0
        # SDR Control's own "L" button (SdrControlWidget.sdr_lock_changed) —
        # independent of _trsp_lock below (CAT dial feedback / Rig1<->Rig2 TX
        # mirroring). While True, _doppler_cycle() stops writing an absolute
        # frequency to whichever rig slot is the SDR and instead recomputes
        # _sdr_tune_offset fresh every cycle from the SDR's actual live
        # frequency, so Passband Tune / Freq+Tune are free to move it and
        # Doppler correction resumes from there once turned back off.
        self._sdr_lock: bool = False
        # L button: when True, uplink is slaved to downlink.
        self._trsp_lock: bool = False
        # Lock dial feedback: manual-DL-retune offset (Hz, DL space),
        # folded into dl_corr/ul_corr in _doppler_cycle(). While Lock is
        # on, recomputed fresh each cycle from a live DL reading (DL is
        # read-only while Lock is active -- see _rig_send()); with Lock
        # off it persists unchanged, so Doppler correction resumes from
        # the manually-adjusted frequency instead of snapping back. Reset
        # to 0.0 only by selecting a different transponder or pressing the
        # T (Tune) button -- never by toggling Lock itself.
        self._dial_feedback_offset_hz: float = 0.0
        # Override for CTCSS label: set when a button is pressed, reset on transponder change.
        # None -> show the transmitter's ctcss_tone; float -> persist the last-sent tone.
        self._current_ctcss_tone: float | None = None
        # Resolved CTCSS tone for the current transmitter (SatNOGS or CTCSS_DB fallback).
        self._ctcss_tone_hz: float | None = None
        # Activation tone for the current satellite (from CTCSS_DB; None if not applicable).
        self._ctcss_activation_hz: float | None = None

        # Shared rig state for mobile web UI
        self._rig_state = rig_state

        # AOS/LOS desktop notifier
        self._notifier = PassNotifier(conn)
        # Group pass results cache for notifier
        self._group_pass_results: list[object] = []
        # Sequential autotrack engine
        self._autotrack = AutotrackManager(conn)
        self._autotrack_enabled: bool = False
        self._autotrack_audio_record: bool = False
        self._autotrack_iq_record: bool = False
        self._autotrack_meteor_record: bool = False
        self._autotrack_tracking_norad: int | None = None  # NORAD of currently auto-tracked sat

        from PySide6.QtWidgets import QApplication

        _ver = QApplication.applicationVersion() or "0.1.0"
        self.setWindowTitle(f"FBSAT59  v{_ver}")
        self.resize(1280, 800)
        self._set_app_icon()
        self._sync_progress.connect(self._on_sync_progress)
        self._load_saved_language()

        self._build_ui()
        self._build_menu()
        self._build_statusbar()

        # Autotrack/Record dialog (non-modal, created once)
        from ui.autotrack_record_dialog import AutotrackRecordDialog

        self._at_dialog = AutotrackRecordDialog(self._conn, parent=self)
        self._at_dialog.autotrack_toggled.connect(self._on_autotrack_toggled)
        self._at_dialog.autotrack_list_changed.connect(self._on_autotrack_list_changed)
        self._at_dialog.audio_record_changed.connect(self._on_autotrack_audio_record_changed)
        self._at_dialog.iq_record_changed.connect(self._on_autotrack_iq_record_changed)
        self._at_dialog.meteor_record_changed.connect(self._on_autotrack_meteor_record_changed)
        self._at_dialog.lists_modified.connect(self._on_autotrack_lists_modified)

        # Connect PassPanel signals
        self._pass_list.target_search_requested.connect(self._on_target_search_requested)
        self._pass_list.highlight_satellite.connect(self._on_highlight_satellite)
        self._pass_list.group_results_ready.connect(self._on_group_results_ready)
        self._pass_list.set_pass_predictor(self._pass_predictor)
        self._pass_list.set_celestial_engine(self._celestial_engine)
        # Connect signal that receives satellite list refresh requests from background threads
        self._satellite_list_refresh.connect(self._load_satellites)
        self._rig_error.connect(self._on_rig_error)
        self._doppler_computed.connect(self._on_doppler_computed)
        self._sdr_lock_offset_computed.connect(self._on_sdr_lock_offset_computed)
        self._satnogs_status.connect(self._on_satnogs_status)
        self._map_downloaded.connect(self._apply_world_map)
        self._satnogs_open_url.connect(self._open_url_app_mode)
        self._satnogs_not_found.connect(
            lambda: QMessageBox.information(self, "SatNOGS", _("SatNOGS page not found"))
        )
        self._satnogs_network_error.connect(self._on_satnogs_network_error)
        self._ntp_check_failed.connect(self._on_ntp_check_failed)
        self._ntp_offset_applied.connect(self._on_ntp_offset_applied)
        self._radio_control.transmitter_changed.connect(self._on_transmitter_changed)
        self._radio_control.cycle_changed.connect(self._on_cycle_changed)
        self._radio_control.tune_requested.connect(self._on_tune_requested)
        self._radio_control.lock_changed.connect(self._on_lock_changed)
        self._radio_control.rx_offset_changed.connect(self._on_rx_offset_changed)
        self._rot_pos_updated.connect(self._on_rotator_pos_updated)
        self._radio_control.ctcss_send_requested.connect(self._on_ctcss_send)
        self._radio_control.ctcss_activate_requested.connect(self._on_ctcss_activate)
        self._radio_control.rotator_connected.connect(self._on_rotator_connected)
        self._radio_control.south_init_changed.connect(self._on_south_init_changed)
        self._radio_control.rig_connected.connect(lambda: self._on_rig_slot_connected(1))
        self._radio_control.rig2_connected.connect(lambda: self._on_rig_slot_connected(2))
        self._radio_control.rig_disconnected.connect(lambda: self._on_rig_slot_disconnected(1))
        self._radio_control.rig2_disconnected.connect(lambda: self._on_rig_slot_disconnected(2))
        self._radio_control.sstv_transponder_selected.connect(self._on_open_sstv)
        self._radio_control.aprs_transponder_selected.connect(self._on_open_aprs)
        self._radio_control.ft4_transponder_selected.connect(self._on_open_ft4)
        self._radio_control.meteor_transponder_selected.connect(
            self._on_meteor_transponder_selected
        )
        self._radio_control.cw_transponder_selected.connect(self._on_cw_transponder_selected)
        self._radio_control.cw_mode_requested.connect(self._apply_mode_toggle_to_rig)
        self._radio_control.dmode_requested.connect(self._apply_mode_toggle_to_rig)
        self._restore_satellite_filter()
        # Load bundled community transmitters immediately (no network required).
        # This runs on the main thread so satellites are visible before any
        # background sync completes — important on first launch.
        try:
            self._transmitter_manager.load_community_transmitters()
        except Exception as exc:
            logger.warning("Community transmitter load failed at startup: %s", exc)
        self._load_satellites()
        self._load_rig_settings()
        self._start_satmode_warmup()
        self._load_rotator_settings()
        self._reload_autotrack_lists()
        self._apply_world_map()
        self._apply_time_zone()

        if fastapi_app is not None:
            self._start_web_server(fastapi_app, web_port)

        self._start_scheduler()

        # Fixed at 1s — only drives display refresh now (satellite detail
        # panel, radar, status bar, world map throttle counter). No longer
        # user-configurable, since it isn't the radio-critical path anymore.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(1000)

        # Doppler correction + rig CAT sends run on their own precise
        # thread, independent of the Qt main thread's own business — see
        # core/doppler_worker.py. The user-facing "Cycle" setting now
        # controls this worker's interval instead of self._timer's.
        self._doppler_worker = DopplerWorker(self._doppler_cycle)
        self._doppler_worker.start()
        self._load_cycle_setting()

        # Lock (L button) dial feedback: reuses DopplerWorker's generic
        # precise-interval thread (it owns no Doppler-specific state) to
        # poll _lock_watch_cycle() independently of the Doppler cycle's own
        # timing. Only started/stopped when Lock is toggled — see
        # _on_lock_changed(). A slower interval than the Doppler cycle is
        # fine (and avoids hammering the rig): a human turning a dial takes
        # at least a second or two.
        self._lock_watch_worker = DopplerWorker(self._lock_watch_cycle, interval_s=2.0)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        """Build widgets and layout."""
        v_splitter = QSplitter(Qt.Orientation.Vertical)
        self._v_splitter = v_splitter
        self.setCentralWidget(v_splitter)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._h_splitter = h_splitter

        # Left: satellite list
        left = QWidget()
        self._sat_list_panel = left
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(2)
        left_layout.addWidget(QLabel(_("Satellites")))

        self._filter_combo = QComboBox()
        self._rebuild_filter_combo()
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        left_layout.addWidget(self._filter_combo)

        # Link to the AMSAT Live Oscar Status page — visible only for the AMSAT filter
        self._amsat_link = QLabel(
            '<a href="https://www.amsat.org/status/"'
            ' style="color:#2980b9; font-size:10px;">'
            "↗ AMSAT Status Page</a>"
        )
        self._amsat_link.setOpenExternalLinks(False)
        self._amsat_link.linkActivated.connect(self._open_url_app_mode)
        self._amsat_link.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._amsat_link.setVisible(False)
        left_layout.addWidget(self._amsat_link)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText(_("Search satellites..."))
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search_changed)
        left_layout.addWidget(self._search_box)

        self._sat_list = QListWidget()
        self._sat_list.currentRowChanged.connect(self._on_sat_selected)
        self._sat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sat_list.customContextMenuRequested.connect(self._on_sat_context_menu)
        left_layout.addWidget(self._sat_list)
        left.setMinimumWidth(140)
        left.setMaximumWidth(240)
        h_splitter.addWidget(left)

        # Centre: tabs (Dashboard / World Map / Radar / Pass Chart / Group Pass Chart / Radio)
        self._tab_widget = QTabWidget()
        # With this many tabs (2 resident + up to 7 Communications tabs at
        # once), the default elide-to-fit behaviour shrinks every tab until
        # long labels like "APRS" or "SDR Control" show only "AP…"/"SDR…"
        # (reported on macOS, where the native tab bar favours keeping every
        # tab visible over showing full labels). ElideNone keeps each tab at
        # its natural full-text width and lets the bar overflow into scroll
        # arrows instead of truncating — usesScrollButtons is normally the
        # default, but is set explicitly here since it's what ElideNone
        # relies on once the bar no longer fits.
        self._tab_widget.setUsesScrollButtons(True)
        self._tab_widget.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self._dashboard_view = DashboardView()
        self._world_map = WorldMapView()
        self._world_map.sat_clicked.connect(self._select_satellite_by_norad)
        self._radar_view = RadarView()
        self._pass_chart = PassChartView()
        self._group_pass_chart = GroupPassChartView()
        self._radio_control = RadioControlWidget()
        self._radio_control.set_db_connection(self._conn)
        # Ctrl+L toggles Lock app-wide regardless of which tab is active --
        # requested in GitHub Issue #14 (ei4gnb): the operator's hands are
        # on the rig's VFO knob, not the mouse, while manually retuning.
        # Default QShortcut context (WindowShortcut) fires whenever any
        # widget inside this window has focus; a plain letter key was
        # avoided so it doesn't get swallowed as text by a focused QLineEdit
        # (e.g. an APRS/FT4 callsign field).
        self._lock_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self._lock_shortcut.activated.connect(self._radio_control.toggle_lock)
        self._pass_chart.range_changed.connect(self._on_chart_range_changed)
        self._dashboard_tab_idx = self._tab_widget.addTab(self._dashboard_view, _("Dashboard"))
        self._tab_widget.addTab(self._world_map, _("World Map"))
        self._tab_widget.addTab(self._radar_view, _("Radar"))
        self._tab_widget.addTab(self._pass_chart, _("Pass Chart"))
        # Group Pass Chart tab — hidden until first group search completes
        self._group_chart_tab_idx = self._tab_widget.addTab(
            self._group_pass_chart, _("Group Pass Chart")
        )
        self._tab_widget.setTabVisible(self._group_chart_tab_idx, False)
        # Resident, but dismissable without exiting the app (re-shown by the
        # next group search) — unlike other resident tabs it does get a ×.
        self._add_tab_close_button(self._group_pass_chart)
        self._tab_widget.addTab(self._radio_control, _("Radio Control"))

        # SDR Control tab — always visible; content greys out until SDR connects
        from ui.sdr_control_widget import SdrControlWidget

        self._sdr_control = SdrControlWidget()
        self._sdr_control_tab_idx = self._tab_widget.addTab(self._sdr_control, _("SDR Control"))
        # CW/FT4/Q65/AX100 Digi look this up as
        # self._radio_control._sdr_control — without this call that
        # attribute never existed at all, so their SDR audio input never
        # worked (GitHub Issue #12 follow-up; see
        # RadioControlWidget.set_sdr_control()'s docstring for the full
        # story).
        self._radio_control.set_sdr_control(self._sdr_control)
        self._sdr_control.tune_offset_changed.connect(self._on_sdr_tune_offset)
        self._sdr_control.sdr_lock_changed.connect(self._on_sdr_lock_changed)
        self._sdr_control.manual_freq_requested.connect(self._on_sdr_manual_freq_requested)

        self._tab_widget.currentChanged.connect(self._on_tab_changed)

        # Allow tabs to be closed (for non-resident Communications tabs).
        # Hide the close button on all resident tabs so only Communications
        # tabs (APRS, Telemetry) show the × button.
        self._resident_tab_widgets: set[QWidget] = {
            self._dashboard_view,
            self._world_map,
            self._radar_view,
            self._pass_chart,
            self._group_pass_chart,
            self._radio_control,
            self._sdr_control,
        }
        # Note: QTabWidget.setTabsClosable(True) is intentionally NOT used
        # here. On macOS, enabling it bakes a native close glyph into every
        # tab's own paint routine (Cocoa style), and per-tab
        # setTabButton(index, side, None) cannot fully suppress it on
        # resident tabs — it leaves stray artifact pixels above the label
        # of whichever tab is currently selected (reported 2026-07-30).
        # Each Communications tab instead gets its own explicit × QToolButton
        # via _add_tab_close_button(), which calls _on_tab_close_requested()
        # directly — there is no native tabCloseRequested signal to connect.

        h_splitter.addWidget(self._tab_widget)

        # Right: satellite detail panel (hidden when Dashboard tab is active)
        self._detail_panel = SatDetailPanel()
        self._detail_panel.setMinimumWidth(160)
        self._detail_panel.setMaximumWidth(260)
        self._detail_panel.bind_radio_control(self._radio_control)
        self._detail_panel.comms_satellite_requested.connect(self._on_comms_satellite_requested)
        h_splitter.addWidget(self._detail_panel)

        h_splitter.setStretchFactor(0, 0)
        h_splitter.setStretchFactor(1, 1)
        h_splitter.setStretchFactor(2, 0)
        # Explicit (matches the QSplitter default) so the satellite list can
        # be collapsed to zero width by View > Wide Tab while still being
        # draggable back out via its splitter handle.
        h_splitter.setCollapsible(0, True)
        # The satellite-detail / Quick Comms Panel (right pane) must never be
        # squeezed below its own minimum width: unlike the collapsible=True
        # default, this floor is enforced even when QSplitter recalculates
        # the layout on its own (e.g. after collapsing pane 0 to zero), not
        # just against direct user dragging. Wide Tab only ever collapses
        # the satellite list — the right panel stays put.
        h_splitter.setCollapsible(2, False)

        # Apply initial visibility: Dashboard is the first tab so currentChanged
        # won't fire on startup — hide the detail panel explicitly here.
        self._detail_panel.setVisible(False)

        # Bottom: pass prediction list (PassPanel)
        self._pass_list = PassPanel()
        self._pass_list.setMinimumHeight(200)

        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(self._pass_list)
        v_splitter.setStretchFactor(0, 3)
        v_splitter.setStretchFactor(1, 2)
        v_splitter.setSizes([600, 400])
        # Explicit (matches the QSplitter default) so the pass-prediction
        # panel can be collapsed to zero height by _on_tab_changed / View >
        # Wide Tab while still being draggable back out via its handle.
        v_splitter.setCollapsible(1, True)

    def _build_menu(self) -> None:
        """Build the menu bar."""
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu(_("File"))
        if file_menu:
            file_menu.addAction(_("Set QTH..."), self._on_set_qth)
            file_menu.addAction(_("General Settings"), self._on_settings)
            file_menu.addSeparator()
            file_menu.addAction(_("Exit"), self.close)

        # Satellite
        sat_menu = mb.addMenu(_("Satellite"))
        if sat_menu:
            sat_menu.addAction(_("Add Transmitter..."), self._on_add_transmitter)
            sat_menu.addAction(_("Edit Transmitter..."), self._on_edit_transmitter)
            sat_menu.addAction(_("Delete Transmitter..."), self._on_delete_transmitter)
            sat_menu.addAction(_("Hide Satellite"), self._on_hide_satellite)
            sat_menu.addSeparator()
            sat_menu.addAction(_("Add Manual TLE..."), self._on_add_manual_tle)
            sat_menu.addAction(_("Update TLE"), self._on_update_tle)
            sat_menu.addAction(_("Sync SATNOGS"), self._on_sync_satnogs)

        # Radio
        radio_menu = mb.addMenu(_("Radio"))
        if radio_menu:
            radio_menu.addAction(_("Rig Settings..."), self._on_rig_settings)
            radio_menu.addAction(_("Rotator Settings..."), self._on_rotator_settings)

        # Communications
        comm_menu = mb.addMenu(_("Communications"))
        if comm_menu:
            comm_menu.addAction(_("APRS"), self._on_open_aprs)
            comm_menu.addAction(_("Telemetry"), self._on_open_telemetry)
            comm_menu.addAction(_("SSTV / SSDV"), self._on_open_sstv)
            comm_menu.addAction(_("FT4"), self._on_open_ft4)
            comm_menu.addAction(_("Q65"), self._on_open_q65)
            comm_menu.addAction(_("CW Decoder"), self._on_open_cw)
            comm_menu.addAction(_("AX100 Digi"), self._on_open_ax100_digi)
            comm_menu.addSeparator()
            comm_menu.addAction(_("METEOR / HRPT"), self._on_open_meteor)

        # Autotrack / Record
        # macOS Cocoa ignores QMenuBar.addAction() — wrap in a single-item menu.
        # Linux / Windows render direct menubar actions correctly.
        if sys.platform == "darwin":
            at_menu = mb.addMenu(_("Autotrack/Record"))
            if at_menu:
                at_menu.addAction(_("Open Autotrack/Record..."), self._on_open_autotrack_dialog)
        else:
            mb.addAction(_("Autotrack/Record"), self._on_open_autotrack_dialog)

        # View
        view_menu = mb.addMenu(_("View"))
        if view_menu:
            lang_menu = view_menu.addMenu(_("Language"))
            if lang_menu:
                from i18n import get_language

                lang_group = QActionGroup(self)
                lang_group.setExclusive(True)
                en_action = QAction("English", self, checkable=True)
                ja_action = QAction("日本語", self, checkable=True)
                lang_group.addAction(en_action)
                lang_group.addAction(ja_action)
                lang_menu.addAction(en_action)
                lang_menu.addAction(ja_action)
                en_action.setChecked(get_language() != "ja")
                ja_action.setChecked(get_language() == "ja")
                en_action.triggered.connect(lambda: self._on_set_language("en"))
                ja_action.triggered.connect(lambda: self._on_set_language("ja"))

            tz_menu = view_menu.addMenu(_("Time Zone"))
            if tz_menu:
                tz_group = QActionGroup(self)
                tz_group.setExclusive(True)
                self._tz_utc_action = QAction("UTC", self, checkable=True)
                self._tz_local_action = QAction(_("Local Time"), self, checkable=True)
                tz_group.addAction(self._tz_utc_action)
                tz_group.addAction(self._tz_local_action)
                tz_menu.addAction(self._tz_utc_action)
                tz_menu.addAction(self._tz_local_action)
                self._tz_utc_action.triggered.connect(lambda: self._on_time_zone_changed(True))
                self._tz_local_action.triggered.connect(lambda: self._on_time_zone_changed(False))

            view_menu.addSeparator()
            self._wide_tab_action = QAction(_("Wide Tab"), self, checkable=True)
            self._wide_tab_action.setToolTip(
                _("Shrink the satellite list and pass-prediction panels for more tab space")
            )
            view_menu.addAction(self._wide_tab_action)
            self._wide_tab_action.triggered.connect(self._on_wide_tab_toggled)

        # Help
        help_menu = mb.addMenu(_("Help"))
        if help_menu:
            help_menu.addAction(_("Satellite/Transmitter Colors"), self._on_satellite_color)
            help_menu.addAction(_("Auto Fetch Rules"), self._on_auto_fetch_rules)
            help_menu.addAction(_("Clear TLE Sync History…"), self._on_clear_tle_sync_history)
            help_menu.addSeparator()
            help_menu.addAction(_("Check for Updates…"), self._on_check_updates)
            help_menu.addAction(_("SDR Device Installation…"), self._on_sdr_install)
            help_menu.addAction(_("Hamlib Update…"), self._on_hamlib_update)
            help_menu.addAction(_("ft8lib Installation…"), self._on_ft8lib_help)
            help_menu.addAction(_("FT4 Enhanced Decoder Installation…"), self._on_ft4wsjt_help)
            help_menu.addAction(_("Q65 Library Installation…"), self._on_q65lib_help)
            help_menu.addAction(_("Direwolf Installation…"), self._on_direwolf_help)
            help_menu.addAction(_("SatDump…"), self._on_satdump_help)
            help_menu.addAction(_("gr-satellites Installation…"), self._on_gr_satellites_help)
            help_menu.addAction(_("CW Model Installation…"), self._on_cw_model_help)
            help_menu.addSeparator()
            help_menu.addAction(_("About"), self._on_about)
            help_menu.addAction(_("GitHub"), self._on_github)

    def _set_app_icon(self) -> None:
        """Set the application window icon from the bundled assets."""
        from pathlib import Path

        # Locate icon: PyInstaller bundle uses _MEIPASS, dev uses assets/ in repo root
        if getattr(sys, "frozen", False):
            icon_path = Path(getattr(sys, "_MEIPASS", "")) / "assets" / "icon_256.png"
        else:
            icon_path = Path(__file__).parent.parent.parent / "assets" / "icon_256.png"

        if icon_path.exists():
            icon = QIcon(str(icon_path))
            self.setWindowIcon(icon)
            from PySide6.QtWidgets import QApplication

            QApplication.setWindowIcon(icon)

    def _on_sync_progress(self, text: str) -> None:
        """Update the sync progress label in the status bar (called on UI thread)."""
        if text:
            self._sync_label.setText(text)
            self._sync_label.setVisible(True)
        else:
            self._sync_label.setVisible(False)

    def _build_statusbar(self) -> None:
        """Build the status bar."""
        sb = self.statusBar()

        self._qth_label = QLabel("QTH: Not set")
        self._tle_label = QLabel("")
        self._filter_label = QLabel("Showing: All")
        self._sync_label = QLabel("")
        self._sync_label.setStyleSheet("color: #f0a500; font-style: italic;")
        self._sync_label.setVisible(False)
        self._url_label = QLabel("")
        self._qr_button = QPushButton("QR")
        self._qr_button.setFlat(True)
        self._qr_button.setMaximumWidth(32)
        self._qr_button.setToolTip(_("Show QR code for web access"))
        self._qr_button.clicked.connect(self._on_show_qr)
        self._rig_label = QLabel(_("RIG: Off"))
        self._rot_label = QLabel(_("ROT: Off"))
        self._tx_owner_label = QLabel("")
        self._tx_owner_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
        self._tx_owner_label.setVisible(False)
        get_audio_device_manager().tx_owner_changed.connect(self._on_tx_owner_changed)

        if sb:
            sb.addWidget(self._qth_label)
            sb.addWidget(self._tle_label)
            sb.addWidget(self._filter_label)
            sb.addWidget(self._sync_label)
            sb.addPermanentWidget(self._url_label)
            sb.addPermanentWidget(self._qr_button)
            sb.addPermanentWidget(self._tx_owner_label)
            sb.addPermanentWidget(self._rig_label)
            sb.addPermanentWidget(self._rot_label)

    # ------------------------------------------------------------------ #
    # Data loading
    # ------------------------------------------------------------------ #

    def _restore_satellite_filter(self) -> None:
        """Restore the satellite filter combo to the last saved selection."""
        try:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'satellite_filter'"
            ).fetchone()
            saved = row[0] if row else "Operational (AMSAT)"
            idx = self._filter_combo.findText(saved)
            if idx >= 0:
                self._filter_combo.blockSignals(True)
                self._filter_combo.setCurrentIndex(idx)
                self._filter_combo.blockSignals(False)
                self._amsat_link.setVisible(saved == "Operational (AMSAT)")
        except Exception:
            pass

    def _load_satellites(self) -> None:
        """Load satellite data from the DB, build the internal list, and apply filters."""
        amsat_map: dict[str, str] = self._amsat_fetcher.load_cached() or {}

        designator_status: dict[str, str] = {}
        for amsat_name, status in amsat_map.items():
            for desig in _extract_designators(amsat_name):
                designator_status[desig] = status

        amsat_keys_by_len = sorted(amsat_map.keys(), key=len, reverse=True)

        rows = self._conn.execute(
            """
            SELECT s.norad_cat_id, s.name, s.alt_names, s.is_favorite, s.is_hidden, s.status,
                   COALESCE(t.tle_group, 'amateur') AS tle_group,
                   s.tle_no_result_since,
                   COALESCE(s.favorite_group, 0) AS favorite_group
            FROM satellites s
            LEFT JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id
            ORDER BY s.name
            """
        ).fetchall()

        self._all_sat_data = []
        self._all_norads = []
        self._sat_name_cache = {}

        for row in rows:
            norad: int = int(row["norad_cat_id"])
            name: str = str(row["name"])
            self._sat_name_cache[norad] = name

            # Parse alt_names once; used for both AMSAT matching and display
            try:
                alt_list: list[str] = json.loads(str(row["alt_names"] or "[]"))
            except (json.JSONDecodeError, ValueError):
                alt_list = []

            # Match AMSAT status against primary name then each alt_name in order.
            # This covers renamed satellites (e.g. DOSAAF-85 a.k.a. RS-44).
            amsat_status: str | None = None
            for candidate in [name, *alt_list]:
                cand_lower = candidate.lower()
                amsat_status = amsat_map.get(cand_lower)
                if amsat_status is not None:
                    break
                for desig in _extract_designators(candidate):
                    if desig in designator_status:
                        amsat_status = designator_status[desig]
                        break
                if amsat_status is not None:
                    break
                for amsat_key in amsat_keys_by_len:
                    if _amsat_key_in_sat_name(amsat_key, cand_lower):
                        amsat_status = amsat_map[amsat_key]
                        break
                if amsat_status is not None:
                    break

            self._all_sat_data.append(
                _SatData(
                    norad=norad,
                    name=name,
                    alt_names=str(row["alt_names"] or "[]"),
                    is_favorite=bool(row["is_favorite"]),
                    is_hidden=int(row["is_hidden"] or 0),
                    status=str(row["status"] or "unknown"),
                    tle_group=str(row["tle_group"]),
                    amsat_status=amsat_status,
                    tle_no_result_since=(
                        str(row["tle_no_result_since"]) if row["tle_no_result_since"] else None
                    ),
                    favorite_group=int(row["favorite_group"] or 0),
                )
            )
            self._all_norads.append(norad)

        self._apply_filter()

    def _apply_filter(self) -> None:
        """Redraw the satellite list according to the filter combo and search box."""
        filter_text = self._filter_combo.currentText()
        search_query = self._search_box.text().strip().lower()

        # Pause signals to prevent currentRowChanged(-1) from firing during list rebuild
        # (which would clear the selection and display), then restore the selection afterward
        prev_item = self._sat_list.currentItem()
        current_norad: int | None = (
            prev_item.data(Qt.ItemDataRole.UserRole) if prev_item is not None else None
        )

        self._sat_list.blockSignals(True)
        self._sat_list.clear()
        count = 0
        restore_row = -1
        filtered_sats: list[tuple[int, str]] = []

        # Inject Moon entry for "All Satellites" and "Celestial Bodies" filters
        show_moon = filter_text in ("All Satellites", "Celestial Bodies") and not search_query
        if show_moon:
            moon_item = QListWidgetItem("☽ Moon")
            moon_item.setData(Qt.ItemDataRole.UserRole, MOON_ID)
            moon_item.setForeground(QColor("#4488ff"))
            self._sat_list.addItem(moon_item)
            if current_norad == MOON_ID:
                restore_row = count
            count += 1
            # Moon is not included in filtered_sats (no pass prediction in Phase 1)

        for d in self._all_sat_data:
            # Skip regular satellites when Celestial Bodies filter is active
            if filter_text == "Celestial Bodies":
                continue
            # Visibility filter:
            #   is_hidden=1 (user-hidden)   -> shown only under the "Hidden" filter
            #   is_hidden=2 (system-hidden) -> hidden from all filters
            # Exception: an active search query surfaces hidden satellites too
            # (marked "(Hidden)" in gray) so a satellite stuck hidden by a stale
            # system judgment (see NORAD 98293 "Marina", 2026-07-09) can still be
            # found and dealt with. Without a search, normal visibility applies.
            found_via_hidden_search = bool(search_query) and d["is_hidden"] != 0
            if not found_via_hidden_search:
                if filter_text == "Hidden":
                    if d["is_hidden"] != 1:
                        continue
                elif d["is_hidden"] != 0:
                    continue
            # Category filter — custom groups (e.g. "★ Favorite 1")
            if filter_text.startswith("★ "):
                group_name = filter_text[2:]  # strip leading "★ "
                grp_row = self._conn.execute(
                    "SELECT id FROM custom_groups WHERE name = ?", (group_name,)
                ).fetchone()
                if grp_row is None or d["favorite_group"] != grp_row["id"]:
                    continue
            if filter_text == "Amateur" and d["tle_group"] != "amateur":
                continue
            if filter_text == "CubeSat" and d["tle_group"] != "cubesat":
                continue
            if filter_text == "Weather" and d["tle_group"] != "weather":
                continue
            if filter_text == "Earth Observation" and d["tle_group"] != "earth-obs":
                continue
            if filter_text == "Science" and d["tle_group"] != "science":
                continue
            if filter_text == "Space Stations" and d["tle_group"] != "stations":
                continue
            if filter_text == "Operational (AMSAT)" and d["amsat_status"] != "operational":
                continue
            # Search filter (case-insensitive substring match on name and alt_names)
            if search_query and search_query not in d["name"].lower():
                try:
                    alts_lower = " ".join(json.loads(d["alt_names"])).lower()
                except (json.JSONDecodeError, ValueError):
                    alts_lower = ""
                if search_query not in alts_lower:
                    continue

            prefix = "★ " if d["favorite_group"] > 0 else ""
            # Append Oscar designator (e.g. "(IO-86)") when not already in the name
            oscar_suffix = ""
            try:
                alt_list: list[str] = json.loads(d["alt_names"])
            except (json.JSONDecodeError, ValueError):
                alt_list = []
            name_upper = d["name"].upper()
            for alt in alt_list:
                m = _OSCAR_RE.search(alt)
                if m:
                    oscar_str = f"{m.group(1)}-{m.group(2)}".upper()
                    if oscar_str not in name_upper:
                        oscar_suffix = f" ({oscar_str})"
                        break
            hidden_suffix = " " + _("(Hidden)") if found_via_hidden_search else ""
            item = QListWidgetItem(prefix + d["name"] + oscar_suffix + hidden_suffix)
            item.setData(Qt.ItemDataRole.UserRole, d["norad"])

            amsat_status = d["amsat_status"]

            if found_via_hidden_search:
                item.setForeground(QColor("#7f8c8d"))
            elif amsat_status == "operational":
                item.setForeground(QColor("#2ecc71"))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            elif amsat_status == "partial":
                item.setForeground(QColor("#f1c40f"))
            elif amsat_status == "non_operational":
                item.setForeground(QColor("#e74c3c"))
            elif d["tle_no_result_since"] is not None:
                # Alive satellite in 30-day TLE grace period → purple caution
                item.setForeground(QColor("#9b59b6"))
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            elif d["status"] == "alive":
                item.setForeground(QColor("#e67e22"))
            else:
                item.setForeground(QColor("#7f8c8d"))

            self._sat_list.addItem(item)
            if current_norad is not None and d["norad"] == current_norad:
                restore_row = count
            filtered_sats.append((d["norad"], d["name"]))
            count += 1

        if restore_row >= 0:
            self._sat_list.setCurrentRow(restore_row)
        self._sat_list.blockSignals(False)

        if search_query:
            self._filter_label.setText(f"Search: '{search_query}' — {count} matches")
        elif filter_text == "All Satellites":
            self._filter_label.setText(f"Showing: All ({count})")
        else:
            self._filter_label.setText(f"Showing: {filter_text} ({count})")

        # Update the visible norads list (used for world-map computation)
        # Moon (MOON_ID) is intentionally excluded — it is drawn separately.
        if filter_text == "All Satellites" and not search_query:
            self._pass_list.set_satellites(filtered_sats)
            self._visible_norads = list(self._all_norads)
            self._world_map.set_visible_norads(None)
        elif filter_text == "Celestial Bodies" and not search_query:
            # Pass Moon as the only Group-search target; no TLE satellites shown.
            self._pass_list.set_satellites([(MOON_ID, _("Moon"))])
            self._visible_norads = []
            self._world_map.set_satellites({})
            self._world_map.set_visible_norads(set())
        else:
            self._pass_list.set_satellites(filtered_sats)
            self._visible_norads = [n for n, _ in filtered_sats]
            self._world_map.set_visible_norads(set(self._visible_norads))

    # ------------------------------------------------------------------ #
    # Background processing
    # ------------------------------------------------------------------ #

    def _start_web_server(self, fastapi_app: Any, port: int) -> None:
        """Start the FastAPI app with uvicorn in the background."""
        try:
            from web.server import WebServer

            self._web_server = WebServer(fastapi_app, port=port)
            url = self._web_server.start()
            self._web_server_url = url
            self._url_label.setText(url)
        except Exception as exc:
            logger.warning("Web server start failed: %s", exc)

    def _start_scheduler(self) -> None:
        """Register and start TLE and AMSAT auto-update jobs with APScheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            self._scheduler = BackgroundScheduler()
            self._scheduler.add_job(
                self._refresh_tle_sync,
                "interval",
                hours=2,
                id="tle_refresh",
                misfire_grace_time=300,
            )
            self._scheduler.add_job(
                self._refresh_amsat_sync,
                "interval",
                hours=24,
                id="amsat_refresh",
                misfire_grace_time=600,
            )
            self._scheduler.add_job(
                self._refresh_provisional_tle_sync,
                "interval",
                hours=12,
                id="provisional_tle_refresh",
                misfire_grace_time=600,
            )
            self._scheduler.add_job(
                self._refresh_active_tle_sync,
                "interval",
                hours=24,
                id="active_tle_refresh",
                misfire_grace_time=1800,
            )
            self._scheduler.add_job(
                self._refresh_satnogs_sync,
                "interval",
                hours=168,
                id="satnogs_transmitter_refresh",
                misfire_grace_time=3600,
            )
            self._scheduler.add_job(
                self._refresh_meteor_tle_sync,
                "interval",
                hours=12,
                id="meteor_tle_refresh",
                misfire_grace_time=600,
            )
            self._scheduler.add_job(
                self._refresh_satellite_names_periodic,
                "interval",
                hours=6,
                id="satellite_names_refresh",
                misfire_grace_time=600,
            )
            self._scheduler.start()
            logger.debug("APScheduler started")
        except Exception as exc:
            logger.warning("APScheduler start failed: %s", exc)
            self._scheduler = None

        # On startup, refresh AMSAT status in the background if stale
        if self._amsat_fetcher.is_stale():
            threading.Thread(target=self._refresh_amsat_sync, daemon=True).start()

        # On startup, auto-sync SATNOGS transmitters if none have been fetched from SATNOGS yet,
        # or if the last full sync is older than the documented 7-day cadence -- the
        # satnogs_transmitter_refresh interval job below only ever fires after 7
        # continuous days of uptime, so without this a user who never leaves the app
        # open that long would never see it refresh again after the initial sync
        # (see TransmitterManager.is_satnogs_transmitters_stale()'s docstring).
        # Community transmitters (source='community') are always present on first launch,
        # so check specifically for SATNOGS-sourced transmitters instead of total count.
        satnogs_count = self._conn.execute(
            "SELECT COUNT(*) FROM transmitters WHERE source = 'satnogs'"
        ).fetchone()[0]
        need_satnogs_sync = (
            satnogs_count == 0 or self._transmitter_manager.is_satnogs_transmitters_stale()
        )

        # Both of the below hit db.satnogs.org (the transmitter sync directly;
        # sync_satellite_names()/Phase 2b/fetch_provisional_tles() inside
        # _refresh_satellite_names_sync() indirectly). Run them one after the
        # other in a single background thread rather than as two independent
        # threads racing in parallel -- from SATNOGS's point of view, two
        # separate connections opening at once and each firing off requests
        # is harder to distinguish from abusive traffic than one steady
        # stream, and every phase here already runs on its own staleness gate
        # or circuit breaker, so the only real cost of serializing them is a
        # few extra seconds before the second one starts (2026-08-11).
        def _startup_data_sync() -> None:
            if need_satnogs_sync:
                self._refresh_satnogs_sync()
            # Always sync satellite names (inserts new satellites too) and
            # then fetch TLEs. Active-TLE fetch runs inside
            # _refresh_satellite_names_sync so it is guaranteed to start only
            # after satellite rows are present in the DB.
            self._refresh_satellite_names_sync()

        threading.Thread(target=_startup_data_sync, daemon=True).start()

        # Load DE421 ephemeris for celestial body tracking (downloads ~17 MB on first run)
        threading.Thread(target=self._load_celestial_engine, daemon=True).start()

        # Verify system clock accuracy at startup — FT4/Q65 decoding requires the
        # clock to be synced to within about a second, and OS-level NTP daemons
        # can silently fail to sync on flaky networks (see notes for 2026-07-09).
        threading.Thread(target=self._check_ntp_sync_background, daemon=True).start()

    @staticmethod
    def _sort_sources_by_priority(sources: list[str]) -> list[str]:
        """Sort TLE source names by their priority in TLE_SOURCES (ascending).

        Running sources in ascending priority order ensures that more-specific
        groups (e.g. cubesat priority=2) always execute *after* more-general ones
        (amateur priority=1), so their tle_group value wins for overlapping satellites.
        Sources not found in TLE_SOURCES are appended at the end.
        """
        from data.tle_manager import TLE_SOURCES

        priority_map = {s["name"]: int(s.get("priority", 99)) for s in TLE_SOURCES}
        return sorted(sources, key=lambda n: priority_map.get(n, 99))

    def _refresh_tle_sync(self) -> None:
        """Update all enabled TLE sources from a background thread (APScheduler job)."""
        from ui.settings_dialog import SettingsDialog

        enabled = self._sort_sources_by_priority(SettingsDialog.get_enabled_sources(self._conn))
        for source_name in enabled:
            try:
                asyncio.run(self._tle_manager.fetch_and_update(source_name))
                logger.info("TLE refresh completed: %s", source_name)
            except Exception as exc:
                logger.warning("TLE refresh failed: %s — %s", source_name, exc)
        # Refresh the satellite list so tle_group changes are visible in the UI
        # without requiring an app restart.
        self._satellite_list_refresh.emit()

    def _refresh_amsat_sync(self) -> None:
        """Update AMSAT operational status from a background thread."""
        try:
            asyncio.run(self._amsat_fetcher.fetch_and_update())
            logger.info("AMSAT status refresh completed")
            self._satellite_list_refresh.emit()
        except Exception as exc:
            logger.warning("AMSAT status refresh failed: %s", exc)

    def _refresh_satellite_names_periodic(self) -> None:
        """APScheduler job: re-run sync_satellite_names() so a transient failure at
        startup (e.g. SATNOGS temporarily unreachable) self-heals instead of leaving
        satellites permanently missing from the DB until the app is restarted.

        Only sync_satellite_names() is repeated here; fetch_provisional_tles() /
        fetch_legacy_tles() / fetch_meteor_tles() already have their own dedicated
        recurring jobs (provisional_tle_refresh, active_tle_refresh, meteor_tle_refresh)
        so re-running the full _refresh_satellite_names_sync() chain would duplicate
        that work.
        """
        try:
            result = asyncio.run(self._transmitter_manager.sync_satellite_names())
            logger.info("Periodic SATNOGS satellite names sync completed: %s", result)
            self._satellite_list_refresh.emit()
        except Exception as exc:
            logger.warning(
                "Periodic SATNOGS satellite names sync failed: %s: %s", type(exc).__name__, exc
            )

    def _load_celestial_engine(self) -> None:
        """Load the DE421 ephemeris in a background thread."""
        ok = self._celestial_engine.load()
        if ok:
            logger.info("CelestialEngine ready")
        else:
            logger.warning("CelestialEngine failed to load — Moon tracking unavailable")

    def _refresh_satellite_names_sync(self) -> None:
        """Sync satellite names from SATNOGS, then refresh TLEs.

        Execution order:
          1. sync_satellite_names() — updates names/status, runs migration pipelines
          2. fetch_active_tles() — TLEs for regular NORAD 10000-89999 satellites
          3. fetch_provisional_tles() — TLEs for visible NORAD >= 90000 satellites
          4. fetch_legacy_tles() — one-time check for NORAD < 10000 satellites;
             hides those no longer tracked by CelesTrak (fast no-op after first run)
          5. fetch_meteor_tles() — individual CATNR check for METEOR/HRPT satellites
             CelesTrak has dropped from its curated group listings (e.g. NOAA 18/19)

        Step 2 used to run last, on the reasoning that its SATNOGS fallback could
        take 20-30 minutes so later steps shouldn't wait on it. Moved to run right
        after step 1 (2026-08-10): step 2 is what actually refreshes an ordinary
        already-known satellite's TLE (e.g. ORIGAMISAT-2, NORAD 68795), and with no
        progress indicator between steps, a user watching a status label that
        stops updating after step 1 has no way to tell steps 3-5 are still working
        rather than frozen — closing the app partway through meant step 2 was
        never reached at all across several restarts (2026-08-10 report). Now the
        most valuable step runs first and reports its own progress (see
        _active_progress below), so even an impatient restart has usually already
        benefited from it.
        """
        from ui.settings_dialog import SettingsDialog  # local import to avoid circular dep

        if self._shutdown_flag.is_set():
            return

        # 2026-08-11 全面刷新: 従来は各ステップが個別にタイムアウトで
        # 「繋がらない」と気づく方式だったため、エラー表示が出るまでに
        # 数分かかり、しかもその間に別のステップの正常なメッセージで
        # 上書きされて見えなくなることがあった（ユーザー報告）。
        # 最初に一度だけ両ホストへ接続確認を行い、どちらか一方でも
        # 繋がらなければ直ちにエラー表示してネットワーク処理全体を中止する。
        celestrak_ok = asyncio.run(self._tle_manager.is_celestrak_bulk_group_reachable())
        satnogs_ok = asyncio.run(self._tle_manager.is_satnogs_reachable())
        if not celestrak_ok or not satnogs_ok:
            down = [
                name
                for name, ok in (("CelesTrak", celestrak_ok), ("SATNOGS", satnogs_ok))
                if not ok
            ]
            msg = _("Cannot connect to {hosts}").format(hosts="/".join(down))
            logger.warning("Startup TLE sync aborted — unreachable: %s", down)
            self._sync_progress.emit(f"❌ {msg}")
            # ネットワーク不要のローカル処理のみ続行する。
            try:
                comm = self._transmitter_manager.load_community_transmitters()
                if comm["inserted"] + comm["updated"] > 0:
                    logger.info("Community transmitters loaded: %s", comm)
            except Exception as exc:
                logger.warning("Community transmitter load failed: %s", exc)
            return

        self._sync_progress.emit("🛰 Syncing satellites from SATNOGS...")

        def _sat_progress(n: int) -> None:
            self._sync_progress.emit(f"🛰 Syncing satellites... ({n:,})")

        try:
            result = asyncio.run(
                self._transmitter_manager.sync_satellite_names(progress_callback=_sat_progress)
            )
            logger.info("SATNOGS satellite names sync completed: %s", result)
        except Exception as exc:
            logger.warning("SATNOGS satellite names sync failed: %s: %s", type(exc).__name__, exc)

        if self._shutdown_flag.is_set():
            return

        # Refresh TLEs for regular (NORAD 10000-89999) satellites first — see the
        # docstring above for why this moved ahead of provisional/legacy/meteor.
        def _active_progress(msg: str) -> None:
            self._sync_progress.emit(f"🛰 {msg}")

        # is_active_tle_retry_due() catches the case where a previous run
        # got blocked partway through and logged a 'success' sync_log entry
        # anyway (see _schedule_active_tle_retry_if_blocked()'s docstring)
        # — without it, is_active_tle_stale() alone would treat that run as
        # "fresh for 24h" and this startup fetch would be skipped even
        # though satellites were left unresolved.
        if self._tle_manager.is_active_tle_stale() or self._tle_manager.is_active_tle_retry_due():
            try:
                active = asyncio.run(
                    self._tle_manager.fetch_active_tles(progress_callback=_active_progress)
                )
                logger.info("Active TLE fetch completed: %s", active)
                self._satellite_list_refresh.emit()
                self._schedule_active_tle_retry_if_blocked(active)
            except Exception as exc:
                logger.warning("Active TLE fetch failed: %s", exc)
        else:
            logger.info("Active TLE cache is fresh — skipping fetch.")

        if self._shutdown_flag.is_set():
            return

        # Fetch TLEs for remaining visible provisional satellites (NORAD >= 90000).
        def _prov_progress(done: int, total: int) -> None:
            self._sync_progress.emit(f"🛰 Fetching provisional TLEs... ({done}/{total})")

        # is_provisional_tle_stale() gate added 2026-08-11: this call used to run
        # unconditionally on every startup regardless of CLAUDE.md's documented
        # 12h cadence, which only actually held while the app stayed running long
        # enough for the APScheduler interval job to fire.
        if self._tle_manager.is_provisional_tle_stale():
            try:
                prov = asyncio.run(
                    self._tle_manager.fetch_provisional_tles(progress_callback=_prov_progress)
                )
                logger.info("Provisional TLE fetch completed: %s", prov)
            except Exception as exc:
                logger.warning("Provisional TLE fetch failed: %s: %s", type(exc).__name__, exc)
        else:
            logger.info("Provisional TLE cache is fresh — skipping fetch.")

        if self._shutdown_flag.is_set():
            return

        # One-time cleanup for very old satellites (NORAD < 10000).
        # Hides those no longer in CelesTrak; fast no-op once all are resolved.
        try:
            legacy = asyncio.run(self._tle_manager.fetch_legacy_tles())
            if legacy.get("found", 0) + legacy.get("hidden", 0) > 0:
                logger.info("Legacy satellite TLE check completed: %s", legacy)
        except Exception as exc:
            logger.warning("Legacy satellite TLE check failed: %s", exc)

        if self._shutdown_flag.is_set():
            return

        # Ensure METEOR/HRPT satellites (e.g. NOAA 18/19, retired from CelesTrak's
        # curated GROUP=WEATHER listing) have a satellites row + TLE so they appear
        # in the main list for AOS/LOS pass prediction.
        try:
            meteor = asyncio.run(self._tle_manager.fetch_meteor_tles())
            if meteor.get("found", 0) > 0:
                logger.info("METEOR satellite TLE check completed: %s", meteor)
        except Exception as exc:
            logger.warning("METEOR satellite TLE check failed: %s", exc)

        if self._shutdown_flag.is_set():
            return

        # Load bundled community transmitters (FT4/FT8 calling freqs, etc.)
        try:
            comm = self._transmitter_manager.load_community_transmitters()
            if comm["inserted"] + comm["updated"] > 0:
                logger.info("Community transmitters loaded: %s", comm)
        except Exception as exc:
            logger.warning("Community transmitter load failed: %s", exc)

        # Covers three cases, not just "first launch" (the name of this block is
        # historical -- see is_source_stale()'s own docstring for the 2026-08-11
        # fix that made it check real elapsed time, not just "ever fetched"):
        #   1. Fresh install: the APScheduler group-specific jobs (celestrak-cubesat,
        #      celestrak-weather, etc.) haven't fired yet because they are scheduled
        #      with interval hours=1/2/4/6/12. Without this initial fetch, every
        #      satellite ends up with tle_group='amateur' and CubeSat / Weather /
        #      Science / Earth-Obs / Space-Stations groups appear empty.
        #   2. Normal staleness: is_source_stale() now also catches a source whose
        #      last successful fetch is older than its own update_interval_hours --
        #      relevant every restart, not just the first one, since interval jobs
        #      don't get a chance to fire on their own unless the app stays open for
        #      a full interval.
        #   3. Upgrade case (e.g. Windows): a previous beta may have left sync_log
        #      entries so is_source_stale() returns False, but without the CASE WHEN
        #      protection that beta introduced, all tle_group values were overwritten
        #      back to 'amateur'. is_group_empty() detects this by checking whether
        #      the expected tle_group has too few satellites in tle_data.
        #
        # Per-source reason logged (not just the source list) so a report like
        # "this group keeps refetching even right after a full sync" can be
        # diagnosed from the log alone: group_empty=True on every run for a
        # source points at is_group_empty()'s fixed minimum-count threshold
        # being too strict for that source's real (small) population, rather
        # than at is_source_stale()'s elapsed-time check (2026-08-11 report).
        enabled = self._sort_sources_by_priority(SettingsDialog.get_enabled_sources(self._conn))
        stale_sources: list[str] = []
        for s in enabled:
            is_stale = self._tle_manager.is_source_stale(s)
            is_empty = self._tle_manager.is_group_empty(s)
            if is_stale or is_empty:
                stale_sources.append(s)
                logger.info(
                    "Group TLE fetch queued: %s (stale=%s, group_empty=%s)", s, is_stale, is_empty
                )
        # celestrak_ok is already confirmed True by the upfront check above
        # (this point is only reached when it is) -- no per-loop reachability
        # probe needed here anymore (2026-08-11 simplification).
        if stale_sources:
            total = len(stale_sources)
            fetching_group_tles = _("Fetching group TLEs")
            for idx, source_name in enumerate(stale_sources, start=1):
                if self._shutdown_flag.is_set():
                    break
                self._sync_progress.emit(
                    f"🛰 {fetching_group_tles}: {source_name} ({idx}/{total})..."
                )
                try:
                    result = asyncio.run(self._tle_manager.fetch_and_update(source_name))
                    logger.info("Group TLE fetch done: %s -> %s", source_name, result)
                except Exception as exc:
                    logger.warning("Group TLE fetch failed: %s - %s", source_name, exc)
            self._sync_progress.emit("")

        # Refresh the satellite list now that names and group TLEs are synced.
        self._satellite_list_refresh.emit()
        self._sync_progress.emit("")  # Hide sync label once satellite list is ready

    # ------------------------------------------------------------------ #
    # Timer callback (every 1 second)
    # ------------------------------------------------------------------ #

    def _on_tick(self) -> None:
        """Timer callback that updates satellite positions and the status bar."""
        try:
            # World map position update is throttled to every MAP_UPDATE_INTERVAL ticks
            # (default 5 seconds) to reduce Skyfield SGP4 computation load — or
            # every 60s with the "All Satellites" filter, since 1000+ dots are
            # too cluttered to usefully read at 5s resolution anyway.
            # _update_world_map() itself further skips the expensive part
            # unless Dashboard/World Map is the active tab, and reports back
            # here whether it actually ran that part.
            map_interval = (
                self._MAP_UPDATE_INTERVAL_ALL_SATELLITES
                if self._filter_combo.currentText() == "All Satellites"
                else self._MAP_UPDATE_INTERVAL
            )
            self._map_tick_counter += 1
            if self._map_tick_counter >= map_interval:
                self._map_tick_counter = 0
                self._update_world_map()

            self._update_selected_satellite()
            self._update_statusbar()
            self._check_notifications()
            self._check_autotrack()
            self._update_rig_web_state()
            self._detail_panel.refresh_freq_mirror()
        except Exception:
            logger.exception("_on_tick error")

    def _update_rig_web_state(self) -> None:
        """Push current rig/rotator state to the shared RigWebState for the mobile web UI."""
        if self._rig_state is None:
            return
        rs = self._rig_state

        # Rig 1
        rig = self._rig_controller
        rs.rig_connected = rig is not None and rig.is_connected
        rs.rig_engaged = rs.rig_connected and self._current_transmitter is not None

        # Frequencies from current transmitter + Doppler
        if self._current_transmitter is not None and self._selected_norad is not None:
            loc = self._location_manager.current if self._location_manager else None
            obs = (
                self._celestial_engine.observe_moon(
                    loc.latitude_deg, loc.longitude_deg, loc.elevation_m
                )
                if self._selected_norad == MOON_ID and loc is not None
                else (self._engine.observe(self._selected_norad) if self._engine else None)
            )
            if obs is not None:
                dl_nom = self._current_transmitter.get("downlink_low")
                ul_nom = self._current_transmitter.get("uplink_low")
                # EME round-trip: Doppler is twice the one-way shift
                rr = obs.range_rate_km_s * (2.0 if self._selected_norad == MOON_ID else 1.0)
                if dl_nom:
                    dl_hz = float(dl_nom)
                    doppler_dl = -dl_hz * rr / 299792.458
                    rs.dl_hz = dl_hz + doppler_dl
                    rs.dl_doppler_hz = doppler_dl
                else:
                    rs.dl_hz = rs.dl_doppler_hz = None
                if ul_nom:
                    ul_hz = float(ul_nom)
                    invert = bool(self._current_transmitter.get("invert", False))
                    doppler_ul = ul_hz * rr / 299792.458 if invert else -ul_hz * rr / 299792.458
                    rs.ul_hz = ul_hz + doppler_ul
                    rs.ul_doppler_hz = doppler_ul
                else:
                    rs.ul_hz = rs.ul_doppler_hz = None
            rs.mode = str(self._current_transmitter.get("mode") or "")
        else:
            rs.dl_hz = rs.ul_hz = rs.dl_doppler_hz = rs.ul_doppler_hz = None
            rs.mode = ""

        # Rotator
        rot = self._rotator_controller
        rs.rot_connected = rot is not None and rot.is_connected
        rs.rot_engaged = rs.rot_connected

        # Handle toggle requests from mobile UI
        if rs.rig_toggle_requested:
            rs.rig_toggle_requested = False
            # Toggle by changing _current_transmitter to None or restoring
            if rs.rig_engaged:
                self._current_transmitter = None
            # (re-engage handled by user selecting transponder again)
        if rs.rot_toggle_requested:
            rs.rot_toggle_requested = False
            if self._rotator_controller is not None:
                if self._rotator_controller.is_connected:
                    self._rotator_controller.disconnect()
                else:
                    self._rotator_controller.connect()

        # Connect request from mobile UI (satellite + transponder already chosen)
        if rs.rig_connect_requested:
            rs.rig_connect_requested = False
            norad = rs.requested_norad
            xpdr_uuid = rs.requested_xpdr_uuid
            if norad is not None and xpdr_uuid is not None:
                self._mobile_rig_connect(norad, xpdr_uuid)

        # Disconnect request from mobile UI
        if rs.rig_disconnect_requested:
            rs.rig_disconnect_requested = False
            self._disconnect_rig()
            self._radio_control.refresh_status()

    def _mobile_rig_connect(self, norad: int, xpdr_uuid: str) -> None:
        """Select satellite+transponder and connect rig, triggered from mobile UI."""
        # 1. Select satellite in list widget (fires _on_sat_selected)
        self._select_satellite_by_norad(norad)

        # 2. Select transponder by UUID (include_dead: the mobile UI's own
        # transponder card list shows every transmitter regardless of status,
        # so the uuid it sends back may not be alive=1)
        transmitters = self._transmitter_manager.get_transmitters(norad, include_dead=True)
        try:
            idx = next(i for i, t in enumerate(transmitters) if t["uuid"] == xpdr_uuid)
        except StopIteration:
            idx = 0
        if transmitters:
            self._radio_control.set_transmitters(transmitters, default_index=idx)
            # _on_transmitter_changed is triggered by set_transmitters

        # 3. Connect rig if not already connected
        if self._rig_controller is not None and not self._rig_controller.is_connected:
            self._rig_controller.connect()
            self._radio_control.refresh_status()

    def _check_notifications(self) -> None:
        """Fire AOS/LOS desktop notifications for Target and Group passes."""
        # Target satellite passes
        if self._current_passes and self._selected_norad is not None:
            sat_name = self._sat_name_cache.get(self._selected_norad, str(self._selected_norad))
            self._notifier.check(self._current_passes, sat_name)

        # Group search passes
        if self._group_pass_results:
            self._notifier.check_group(self._group_pass_results)

    def _check_autotrack(self) -> None:
        """Run one autotrack evaluation cycle (called every second from _on_tick)."""
        # Timer-based auto start/stop
        if self._autotrack_enabled:
            now_utc = datetime.now(UTC)
            stop_utc = self._at_dialog.get_timer_stop_utc()
            if now_utc >= stop_utc:
                # Timer expired — disable autotrack
                self._autotrack_on_los()
                self._autotrack_enabled = False
                self._autotrack.reset()
                self._at_dialog.set_autotrack_enabled(False)
                self._radio_control.set_autotrack_indicator(False)
                self._at_dialog.set_autotrack_status(
                    _("Timer expired — Autotrack stopped"), ok=False
                )
                return
        elif self._autotrack.is_ready:
            # Auto start when start time is reached and autotrack is not yet running
            now_utc = datetime.now(UTC)
            start_utc = self._at_dialog.get_timer_start_utc()
            if now_utc >= start_utc:
                self._autotrack_enabled = True
                self._at_dialog.set_autotrack_enabled(True)
                self._radio_control.set_autotrack_indicator(True)

        if not self._autotrack_enabled or self._engine is None:
            return
        if not self._autotrack.is_ready:
            return

        if self._pass_predictor is None:
            return
        result = self._autotrack.check(
            self._engine,
            self._pass_predictor,
            cached_elevations=self._last_elevations,
        )
        if result is None:
            # No satellite to switch to — check if currently tracked one is still visible
            # to manage auto connect/disconnect and recording
            if self._autotrack_tracking_norad is not None:
                el = self._last_elevations.get(self._autotrack_tracking_norad, -90.0)
                if el < 0:
                    self._autotrack_on_los()

            # Update status with next satellite info
            info = self._autotrack.next_satellite_info(self._engine, self._pass_predictor)
            if info is not None:
                next_name, next_aos = info
                if next_aos is not None:
                    now = datetime.now(UTC)
                    mins = int((next_aos - now).total_seconds() / 60)
                    self._at_dialog.set_autotrack_status(
                        f"Next: {next_name} in {mins} min", ok=True
                    )
            return

        next_norad, xpdr_uuid = result
        is_new_sat = next_norad != self._autotrack_tracking_norad

        # Switch satellite in the UI
        self._select_satellite_by_norad(next_norad)

        # Switch transponder to the registered one
        xpdr_row = self._conn.execute(
            "SELECT * FROM transmitters WHERE uuid = ?", (xpdr_uuid,)
        ).fetchone()
        if xpdr_row:
            transmitters = self._transmitter_manager.get_transmitters(next_norad, include_dead=True)
            try:
                idx = next(i for i, t in enumerate(transmitters) if t["uuid"] == xpdr_uuid)
            except StopIteration:
                idx = 0
            self._radio_control.set_transmitters(transmitters, default_index=idx)

        sat_name = self._sat_name_cache.get(next_norad, str(next_norad))
        self._at_dialog.set_autotrack_status(f"Tracking: {sat_name}", ok=True)

        if is_new_sat:
            # Previous satellite's LOS — disconnect if it was being tracked
            if self._autotrack_tracking_norad is not None:
                self._autotrack_on_los()
            # New satellite's AOS — connect rig + rotator, start recording
            self._autotrack_tracking_norad = next_norad
            self._autotrack_on_aos(sat_name)

    def _select_satellite_by_norad(self, norad: int) -> None:
        """Select a satellite in the list widget by NORAD id (autotrack helper)."""
        for i in range(self._sat_list.count()):
            item = self._sat_list.item(i)
            if item is not None and int(item.data(Qt.ItemDataRole.UserRole)) == norad:
                self._sat_list.setCurrentRow(i)
                return

    def _reload_autotrack_lists(self) -> None:
        """Reload Autotrack Lists from DB and refresh the dialog combo."""
        lists = AutotrackManager.get_all_lists(self._conn)
        self._at_dialog.populate_list_combo(lists)
        self._radio_control.populate_autotrack_lists(lists)

    def _on_autotrack_toggled(self, enabled: bool) -> None:
        """Called when the user toggles the Autotrack checkbox."""
        self._autotrack_enabled = enabled
        self._radio_control.set_autotrack_indicator(enabled)
        if not enabled:
            self._autotrack.reset()
            self._at_dialog.set_autotrack_status("—")
            self._autotrack_tracking_norad = None
        else:
            if not self._autotrack.is_ready:
                self._at_dialog.set_autotrack_status(_("Run a pass search first"), ok=False)

    def _on_autotrack_list_changed(self, list_id: object) -> None:
        """Called when the user selects a different Autotrack List."""
        lid = int(list_id) if isinstance(list_id, int) else None
        self._autotrack.set_list(lid)
        self._autotrack_enabled = False
        self._at_dialog.set_autotrack_enabled(False)
        self._at_dialog.set_autotrack_status("—")
        self._radio_control.set_autotrack_indicator(False)

    def _on_autotrack_audio_record_changed(self, enabled: bool) -> None:
        self._autotrack_audio_record = enabled

    def _on_autotrack_iq_record_changed(self, enabled: bool) -> None:
        self._autotrack_iq_record = enabled

    def _on_autotrack_meteor_record_changed(self, enabled: bool) -> None:
        self._autotrack_meteor_record = enabled

    def _on_autotrack_lists_modified(self) -> None:
        """Called when lists are added/removed in the dialog — refresh radio control combo."""
        lists = AutotrackManager.get_all_lists(self._conn)
        self._radio_control.populate_autotrack_lists(lists)

    def _on_open_autotrack_dialog(self) -> None:
        """Open the Autotrack/Record settings dialog."""
        self._at_dialog.show()
        self._at_dialog.raise_()
        self._at_dialog.activateWindow()

    # ------------------------------------------------------------------ #
    # Communications menu slots
    # ------------------------------------------------------------------ #

    def _add_tab_close_button(self, tab: QWidget) -> None:
        """Attach an explicit × close button to a single tab.

        Deliberately does NOT use QTabWidget.setTabsClosable(True). That
        global switch makes macOS's native (Cocoa) style bake a close glyph
        into the paint routine of every tab, and per-tab
        setTabButton(index, side, None) cannot fully suppress it on tabs
        that never had a real button widget — it left stray artifact pixels
        above the label of whichever tab was selected (reported 2026-07-30).
        Attaching a real QToolButton per tab avoids that native chrome
        entirely, so resident tabs (which never call this) are unaffected.
        """
        from PySide6.QtWidgets import QTabBar, QToolButton

        btn = QToolButton()
        btn.setText("×")
        btn.setAutoRaise(True)
        btn.setFixedSize(18, 18)
        btn.setToolTip(_("Close"))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _on_click() -> None:
            idx = self._tab_widget.indexOf(tab)
            if idx >= 0:
                self._on_tab_close_requested(idx)

        btn.clicked.connect(_on_click)
        idx = self._tab_widget.indexOf(tab)
        self._tab_widget.tabBar().setTabButton(idx, QTabBar.ButtonPosition.RightSide, btn)

    def _on_tab_close_requested(self, index: int) -> None:
        """Close a Communications tab and clean up its resources.

        Guards against removing a resident tab even if this were somehow
        called with its index (defence in depth — resident tabs never get
        a close button from _add_tab_close_button() in the first place).

        Calls widget.close() before deleteLater() so the tab's closeEvent()
        actually fires — that's what stops background subprocesses
        (Direwolf, SatDump, gr-satellites) and releases shared audio device
        locks. deleteLater() alone only schedules the widget for deletion
        and never invokes closeEvent().

        The Group Pass Chart tab is a special case: it is a persistent
        resident widget (updated in place by each group search via
        set_results()), not a disposable Communications tab, so closing it
        only hides it instead of removing/destroying it. The next group
        search makes it visible again (_on_group_results_ready).
        """
        widget = self._tab_widget.widget(index)
        if widget is self._group_pass_chart:
            self._tab_widget.setTabVisible(self._group_chart_tab_idx, False)
            self._tab_widget.setCurrentIndex(self._dashboard_tab_idx)
            return
        if widget is None or widget in self._resident_tab_widgets:
            return
        self._tab_widget.removeTab(index)
        self._comms_tab_keys.pop(widget, None)
        widget.close()
        widget.deleteLater()

    def _notify_comms_tab_of_rig_state(self, tab: object) -> None:
        """Call _on_rig_connected / _on_rig2_connected on a newly created comms
        tab if the corresponding rig is already connected when the tab opens.
        Without this, the tab misses the signal that was emitted before it existed."""
        rc = self._radio_control
        rig1 = getattr(rc, "_rig1", None)
        rig2 = getattr(rc, "_rig2", None)
        if (
            rig1 is not None
            and getattr(rig1, "is_connected", False)
            and hasattr(tab, "_on_rig_connected")
        ):
            tab._on_rig_connected()
        if (
            rig2 is not None
            and getattr(rig2, "is_connected", False)
            and hasattr(tab, "_on_rig2_connected")
        ):
            tab._on_rig2_connected()

    def _notify_telemetry_tab_satellite(self, norad: int, name: str) -> None:
        """Forward satellite selection to the Telemetry tab if it is open."""
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == _("Telemetry"):
                w = self._tab_widget.widget(i)
                if w is not None and hasattr(w, "set_satellite"):
                    w.set_satellite(norad, name)
                break

    def _on_open_aprs(self) -> None:
        """Open the APRS tab (Communications > APRS).

        Does nothing when neither a rig nor an SDR is connected — the tab
        requires an audio source to be useful.  A placeholder tab with an
        explanatory message is shown instead so the user is not left without
        feedback.
        """
        # Check if APRS tab is already open
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == _("APRS"):
                self._tab_widget.setCurrentIndex(i)
                return

        from ui.aprs_tab import AprsTab

        tab = AprsTab(self._conn, self._radio_control, parent=self)
        self._comms_tab_keys[tab] = "aprs"
        tab.aprs_stations_updated.connect(self._world_map.set_aprs_stations)
        tab.aprs_stations_cleared.connect(self._world_map.clear_aprs_stations)
        idx = self._tab_widget.addTab(tab, _("APRS"))
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)
        self._notify_comms_tab_of_rig_state(tab)

    def _on_open_telemetry(self) -> None:
        """Open the Telemetry tab (Communications > Telemetry)."""
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == _("Telemetry"):
                self._tab_widget.setCurrentIndex(i)
                return

        from ui.telemetry_tab import TelemetryTab

        tab = TelemetryTab(self._conn, self._radio_control, parent=self)
        tab.satellite_selected.connect(self._on_telemetry_satellite_requested)
        self._comms_tab_keys[tab] = "telemetry"
        idx = self._tab_widget.addTab(tab, _("Telemetry"))
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)
        self._notify_comms_tab_of_rig_state(tab)
        if self._selected_norad is not None:
            sat_name = self._sat_name_cache.get(self._selected_norad, str(self._selected_norad))
            tab.set_satellite(self._selected_norad, sat_name)

    def _on_telemetry_satellite_requested(self, norad: int, mode: str = "afsk") -> None:
        """Telemetry tab combo changed — switch filter to All, select satellite, pick transponder.

        For AFSK mode: prefer Beacon / AFSK type transmitters.
        For gr-satellites mode: pick the transmitter whose downlink_low is closest to
        the first frequency listed in the gr-satellites YAML definition.
        """
        all_text = "All Satellites"
        if self._filter_combo.currentText() != all_text:
            idx = self._filter_combo.findText(all_text)
            if idx >= 0:
                self._filter_combo.setCurrentIndex(idx)
        self._select_satellite_by_norad(norad)
        self._refresh_radio_control(norad)
        transmitters = self._radio_control._transmitters
        if not transmitters:
            return

        best_idx = 0
        if mode == "afsk":
            # Priority: description contains TLM/Telemetry > type=Beacon > mode=AFSK > mode=CW
            best_score = 999
            for i, t in enumerate(transmitters):
                desc = (t.get("description") or "").upper()
                if "TLM" in desc or "TELEMETRY" in desc:
                    score = 0
                elif t.get("type") == "Beacon":
                    score = 1
                elif (t.get("mode") or "").upper() == "AFSK":
                    score = 2
                elif (t.get("mode") or "").upper() == "CW":
                    score = 3
                else:
                    score = 999
                if score < best_score:
                    best_score = score
                    best_idx = i
        else:
            # gr-satellites: TLM/Telemetry description first, then YAML frequency proximity
            tlm_idx: int | None = None
            for i, t in enumerate(transmitters):
                desc = (t.get("description") or "").upper()
                if "TLM" in desc or "TELEMETRY" in desc:
                    tlm_idx = i
                    break
            if tlm_idx is not None:
                best_idx = tlm_idx
            else:
                from comms.telemetry.gr_satellites_backend import get_satellite_info

                info = get_satellite_info(norad)
                freqs: list[int] = (
                    [int(f) for f in info["frequencies"]]  # type: ignore[attr-defined]
                    if info and info.get("frequencies")
                    else []
                )
                if freqs:
                    best_diff = float("inf")
                    for i, t in enumerate(transmitters):
                        dl = t.get("downlink_low") or 0
                        diff = min(abs(dl - f) for f in freqs)
                        if diff < best_diff:
                            best_diff = diff
                            best_idx = i

        self._radio_control.set_transmitters(transmitters, default_index=best_idx)

    def _on_open_sstv(self) -> None:
        """Open the SSTV / SSDV tab (Communications > SSTV / SSDV)."""
        tab_label = _("SSTV / SSDV")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                self._tab_widget.setCurrentIndex(i)
                return

        from ui.sstv_tab import SstvTab

        # Pass the APRS engine so SSDV can tap the AX.25 pipeline
        aprs_engine = None
        for i in range(self._tab_widget.count()):
            w = self._tab_widget.widget(i)
            if w is not None and hasattr(w, "engine"):
                aprs_engine = w.engine
                break

        tab = SstvTab(self._conn, self._radio_control, aprs_engine=aprs_engine, parent=self)
        self._comms_tab_keys[tab] = "sstv"
        idx = self._tab_widget.addTab(tab, tab_label)
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)
        self._notify_comms_tab_of_rig_state(tab)

    def _on_open_ft4(self) -> None:
        """Open the FT4 tab (Communications > FT4)."""
        tab_label = _("FT4")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                self._tab_widget.setCurrentIndex(i)
                return

        from ui.ft4_tab import Ft4Tab

        tab = Ft4Tab(self._conn, self._radio_control, parent=self)
        self._comms_tab_keys[tab] = "ft4"
        idx = self._tab_widget.addTab(tab, tab_label)
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)
        self._notify_comms_tab_of_rig_state(tab)

    def _on_comms_satellite_requested(self, tab_key: str, norad: int) -> None:
        """Comms Quick Panel's Input Source combo changed — switch filter to
        All Satellites, select the satellite, and auto-pick the first
        transponder matching this tab's mode_detection matcher (if any).

        Generic counterpart of _on_telemetry_satellite_requested(), reused by
        every Communications tab that has show_input_source=True in
        COMMS_TAB_CONFIG (FT4 now; APRS/SSTV in a later phase).
        """
        all_text = "All Satellites"
        if self._filter_combo.currentText() != all_text:
            idx = self._filter_combo.findText(all_text)
            if idx >= 0:
                self._filter_combo.setCurrentIndex(idx)
        self._select_satellite_by_norad(norad)
        self._refresh_radio_control(norad)

        config = mode_detection.COMMS_TAB_CONFIG.get(tab_key)
        transmitters = self._radio_control._transmitters
        if not transmitters or config is None or config.matcher is None:
            return
        best_idx = mode_detection.pick_preferred_transponder_index(transmitters, config.matcher)
        if best_idx is None:
            return
        self._radio_control.set_transmitters(transmitters, default_index=best_idx)

    def _on_open_q65(self) -> None:
        """Open the Q65 tab (Communications > Q65)."""
        tab_label = _("Q65")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                self._tab_widget.setCurrentIndex(i)
                return

        # Unlike the other _on_open_* handlers, Q65Tab pulls in a ctypes
        # dylib/dll load (libq65) at *import* time (comms.q65.codec's
        # module-level _load_libq65() call) — if that ever raises something
        # other than the OSError it's guarded against (e.g. a corrupt or
        # wrong-arch library left over from a previous install attempt),
        # the exception used to propagate out of this Qt slot silently: no
        # dialog, no tab, no visible error unless you were watching stderr.
        # The import itself must be inside the try — a version of this fix
        # that left `from ui.q65_tab import Q65Tab` outside the try block
        # only caught failures from the constructor, not from the import,
        # and so still failed silently for an import-time crash.
        try:
            from ui.q65_tab import Q65Tab

            tab = Q65Tab(self._conn, self._radio_control, parent=self)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to open Q65 tab")
            QMessageBox.warning(
                self,
                tab_label,
                _("Could not open the Q65 tab:\n{error}").format(error=exc),
            )
            return
        self._comms_tab_keys[tab] = "q65"
        idx = self._tab_widget.addTab(tab, tab_label)
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)
        self._notify_comms_tab_of_rig_state(tab)

    def _on_open_meteor(self, norad: int = 0, downlink_hz: int = 0) -> None:
        """Open the METEOR / HRPT tab (Communications > METEOR / HRPT).

        If *norad* and *downlink_hz* are provided the tab's pipeline combo is
        synced to the matching entry (called from Radio Control auto-open).
        """
        from ui.meteor_tab import MeteorTab

        tab_label = _("METEOR / HRPT")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                self._tab_widget.setCurrentIndex(i)
                # Sync pipeline combo if called with a specific transponder
                w = self._tab_widget.widget(i)
                if norad and isinstance(w, MeteorTab):
                    w.select_pipeline_by_norad_and_freq(norad, downlink_hz)
                return

        # Pass the SDR Control tab widget so MeteorTab can grey it out
        sdr_control_tab = self._tab_widget.widget(self._sdr_control_tab_idx)
        tab = MeteorTab(
            sdr_control_tab=sdr_control_tab,
            sdr_widget=self._sdr_control,
            parent=self,
        )
        # Connect MeteorTab → Radio Control / satellite list sync
        tab.satellite_selection_requested.connect(self._on_meteor_satellite_requested)
        self._comms_tab_keys[tab] = "meteor"
        idx = self._tab_widget.addTab(tab, tab_label)
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)
        if norad:
            tab.select_pipeline_by_norad_and_freq(norad, downlink_hz)

    def _on_meteor_transponder_selected(self, norad: int, downlink_hz: int) -> None:
        """Radio Control selected a METEOR LRPT/HRPT transponder — open/sync METEOR tab."""
        self._on_open_meteor(norad=norad, downlink_hz=downlink_hz)

    def _on_meteor_satellite_requested(self, norad: int, downlink_hz: int) -> None:
        """METEOR tab pipeline combo changed — sync satellite list and Radio Control.

        Selects the satellite in the list (which triggers _refresh_radio_control),
        then explicitly selects the closest-frequency LRPT transponder so Radio
        Control reflects the pipeline even when the satellite was already selected.
        """
        # Select satellite — fires _on_sat_selected → _refresh_radio_control if row changes
        self._select_satellite_by_norad(norad)

        # Always explicitly reload transmitters and pick the LRPT transponder closest
        # to downlink_hz so Radio Control mirrors the METEOR tab selection even when
        # the satellite was already selected (currentRowChanged does not fire then).
        self._refresh_radio_control(norad)
        transmitters = self._radio_control._transmitters
        if not transmitters:
            return
        best_idx = 0
        best_diff = float("inf")
        for i, t in enumerate(transmitters):
            mode = (t.get("mode") or "").upper()
            if mode not in ("LRPT", "HRPT"):
                continue
            dl = t.get("downlink_low") or 0
            diff = abs(dl - downlink_hz)
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        self._radio_control.set_transmitters(transmitters, default_index=best_idx)

    def _on_open_cw(self) -> None:
        """Open the CW Decoder tab (Communications > CW Decoder)."""
        tab_label = _("CW Decoder")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                self._tab_widget.setCurrentIndex(i)
                return

        from ui.cw_tab import CwTab

        tab = CwTab(self._conn, self._radio_control, parent=self)
        self._comms_tab_keys[tab] = "cw"
        idx = self._tab_widget.addTab(tab, tab_label)
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)

    def _on_cw_transponder_selected(self) -> None:
        """Radio Control selected a CW transponder — auto-open CW Decoder tab."""
        self._on_open_cw()

    def _on_open_ax100_digi(self) -> None:
        """Open the AX100 Digipeater tab (Communications > AX100 Digipeater).

        GreenCube (IO-117) / MARMOTSat-compatible GMSK "ASM+Golay" receiver.
        No transponder-selection auto-open wiring yet (MARMOTSat's VHF
        digipeater is listed in SATNOGS with mode=AFSK and a description
        containing "APRS", which would collide with is_aprs_transmitter()'s
        matching — this tab must be opened from the menu until that's
        resolved)."""
        tab_label = _("AX100 Digi")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                self._tab_widget.setCurrentIndex(i)
                return

        from ui.ax100_digi_tab import Ax100DigiTab

        tab = Ax100DigiTab(self._conn, self._radio_control, parent=self)
        self._comms_tab_keys[tab] = "ax100digi"
        idx = self._tab_widget.addTab(tab, tab_label)
        self._add_tab_close_button(tab)
        self._tab_widget.setCurrentIndex(idx)

    def _on_cw_model_help(self) -> None:
        """Open the Help > CW Model Installation… dialog."""
        from ui.cw_model_dialog import CwModelDialog

        dlg = CwModelDialog(self)
        dlg.exec()

    def _on_satdump_help(self) -> None:
        """Open the Help > SatDump… dialog."""
        from ui.satdump_dialog import SatDumpDialog

        dlg = SatDumpDialog(self)
        dlg.exec()

    def _is_map_tab_active(self) -> bool:
        """True if the currently active tab actually displays the world map
        (the flat World Map tab, or Dashboard's embedded zoom map) — the
        only two consumers of the expensive per-satellite loop below."""
        current = self._tab_widget.currentWidget()
        return current is self._dashboard_view or current is self._world_map

    def _update_world_map(self) -> bool:
        """Fetch satellite subpoints for visible satellites and update the world map.

        Only computes positions for the satellites currently shown in the list widget
        (_visible_norads) instead of all non-hidden satellites, reducing SGP4 load
        significantly when a filter is active.  Uses _sat_name_cache to avoid a DB
        round-trip every 5 seconds.

        The per-satellite loop below is skipped entirely unless Dashboard or
        World Map is the active tab — profiling showed it costing ~0.2-0.8s
        with a broad ("All Satellites") filter, which was running every 5s
        even while the user was looking at a completely unrelated tab (e.g.
        FT4), needlessly competing with MainWindow._on_tick()'s other
        duties — see ft4_decode.log's "main_window tick" entries and
        comms/ft4/decode_log.py's investigation notes (2026-07-10). Returns
        True iff that per-satellite pass actually ran, for _on_tick()'s own
        diagnostic logging.
        """
        # Update the observer location star marker (regardless of whether the engine is set)
        if self._location_manager is not None and self._location_manager.current is not None:
            loc = self._location_manager.current
            self._world_map.set_observer_location(loc.latitude_deg, loc.longitude_deg)
            self._dashboard_view.set_observer(loc.latitude_deg, loc.longitude_deg)
            self._pass_list.set_observer_location(
                loc.latitude_deg, loc.longitude_deg, loc.elevation_m
            )

        if self._engine is None or not self._visible_norads:
            return False
        if not self._is_map_tab_active():
            return False

        # Use cached name map — rebuilt in _load_satellites, no DB hit here
        name_map = self._sat_name_cache

        # One Skyfield propagation per satellite instead of two (subpoint()
        # and observe() previously each called sat.at(t) independently) —
        # roughly a 4x speedup for this loop (2026-07-10).
        observed = self._engine.observe_multi_with_subpoints(self._visible_norads)
        sat_data: dict[int, tuple[str, float, float, QColor]] = {}
        new_elevations: dict[int, float] = {}

        for i, norad in enumerate(self._visible_norads):
            result = observed.get(norad)
            if result is None:
                continue
            color = SAT_COLORS[i % len(SAT_COLORS)]
            sat_data[norad] = (
                name_map.get(norad, str(norad)),
                result.subpoint_lat_deg,
                result.subpoint_lon_deg,
                color,
            )
            new_elevations[norad] = result.observation.elevation_deg

        self._last_elevations = new_elevations
        self._world_map.set_satellites(sat_data)

        # Update the selected satellite's footprint (moves dynamically every second)
        if self._selected_norad is not None:
            swa = self._engine.subpoint_with_alt(self._selected_norad)
            if swa is not None:
                fp_lat, fp_lon, alt_km = swa
                self._world_map.draw_footprint(self._selected_norad, fp_lat, fp_lon, alt_km)
            else:
                self._world_map.clear_footprint()
        else:
            self._world_map.clear_footprint()
        return True

    def _update_moon(self) -> None:
        """Update EL/AZ/Range, radar 24h arc, world map sub-point, and rotator for the Moon."""
        if not self._celestial_engine.is_loaded:
            return
        loc = self._location_manager.current if self._location_manager else None
        if loc is None:
            return

        obs = self._celestial_engine.observe_moon(
            loc.latitude_deg, loc.longitude_deg, loc.elevation_m
        )
        self._detail_panel.update_observation(obs)

        if obs is None:
            return

        # Update Moon list item colour: blue below horizon, bold blue above
        for i in range(self._sat_list.count()):
            it = self._sat_list.item(i)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == MOON_ID:
                it.setForeground(QColor("#4488ff"))
                f = it.font()
                f.setBold(obs.is_above_horizon)
                it.setFont(f)
                break

        # 24-hour arc: recompute only on map-update ticks (every 5 s) to reduce CPU load
        if self._map_tick_counter == 0 or not self._moon_arc_cache:
            self._moon_arc_cache = self._celestial_engine.moon_track(
                loc.latitude_deg, loc.longitude_deg, loc.elevation_m
            )
        arc = self._moon_arc_cache

        track = SatTrackData(
            name="Moon",
            norad_cat_id=MOON_ID,
            azimuth_deg=obs.azimuth_deg,
            elevation_deg=obs.elevation_deg,
            is_visible=obs.is_above_horizon,
            track=arc,
            aos_time=None,
            los_time=None,
            next_max_el=None,
            next_duration_s=None,
        )
        self._radar_view.set_tracks([track])
        self._detail_panel.update_radar_track(track)

        # Sub-lunar point: used for both the world map icon and the Dashboard zoom
        sub = self._celestial_engine.moon_subpoint()
        if sub is not None:
            self._world_map.set_moon_position(sub[0], sub[1])
            # Pass sub-lunar point to Dashboard so it can zoom/center on the Moon.
            # alt_km slot carries range_km; draw_footprint is skipped for MOON_ID.
            self._dashboard_view.update_observation(
                obs,
                subpoint=(sub[0], sub[1], obs.range_km),
                track_data=track,
            )
        else:
            self._dashboard_view.update_observation(obs, subpoint=None, track_data=track)

        # Radio Control: EME Doppler correction (round-trip × 2)
        if self._current_transmitter is not None:
            rr = obs.range_rate_km_s * 2.0
            dl_nom = self._current_transmitter.get("downlink_low")
            ul_nom = self._current_transmitter.get("uplink_low")
            mode = self._current_transmitter.get("mode")
            dl_corr, dl_shift = (
                DopplerCalculator.correct_downlink(float(dl_nom), rr)
                if dl_nom is not None
                else (None, None)
            )
            ul_corr, ul_shift = (
                DopplerCalculator.correct_uplink(float(ul_nom), rr)
                if ul_nom is not None
                else (None, None)
            )
            if self._tune_dl_override is not None:
                dl_corr = self._tune_dl_override
                dl_shift = None
                self._tune_dl_override = None
            if self._tune_ul_override is not None:
                ul_corr = self._tune_ul_override
                ul_shift = None
                self._tune_ul_override = None
            self._radio_control.update_doppler(
                dl_nom,
                dl_corr,
                dl_shift,
                ul_nom,
                ul_corr,
                ul_shift,
                mode,
                None,
            )
            if sub is not None:
                self._dashboard_view.update_observation(
                    obs,
                    subpoint=(sub[0], sub[1], obs.range_km),
                    dl_hz=dl_corr,
                    ul_hz=ul_corr,
                    dl_doppler=dl_shift,
                    ul_doppler=ul_shift,
                    track_data=track,
                )

            # Send Doppler-corrected frequencies to the connected rig (same
            # non-blocking pattern as regular satellites).
            if self._rig_controller is not None and self._rig_controller.is_connected:
                if self._rig_busy_lock.acquire(blocking=False):
                    rig = self._rig_controller
                    dl = dl_corr
                    ul = ul_corr

                    def _eme_rig_send() -> None:
                        try:
                            rig.set_vfo_frequencies(dl, ul)
                        except RigControlError as exc:
                            self._rig_error.emit(str(exc))
                        except Exception as exc:
                            logger.error("EME rig: unexpected error: %s", exc)
                            self._rig_error.emit(str(exc))
                        finally:
                            self._rig_busy_lock.release()

                    threading.Thread(target=_eme_rig_send, daemon=True).start()
                else:
                    logger.debug("EME rig: previous cycle still running, skipping tick")

            if (
                self._rig2_controller is not None
                and self._rig2_controller.is_connected
                and self._rig2_busy_lock.acquire(blocking=False)
            ):
                rig2 = self._rig2_controller
                dl2 = dl_corr
                ul2 = ul_corr

                def _eme_rig2_send() -> None:
                    try:
                        rig2.set_vfo_frequencies(dl2, ul2)
                    except RigControlError as exc:
                        self._rig_error.emit(str(exc))
                    except Exception as exc:
                        logger.error("EME rig2: unexpected error: %s", exc)
                        self._rig_error.emit(str(exc))
                    finally:
                        self._rig2_busy_lock.release()

                threading.Thread(target=_eme_rig2_send, daemon=True).start()

        # Rotator tracking — same non-blocking path as regular satellites
        self._send_to_rotator(obs)

    def _update_selected_satellite(self) -> None:
        """Update the observation values and radar view for the currently selected satellite.

        Runs on the Qt main thread from _on_tick() (1s, fixed). Doppler
        correction itself — computing corrected DL/UL and sending them to
        the rig(s) — no longer happens here; it runs independently on
        DopplerWorker's own precise schedule (see _doppler_cycle()). This
        method only reads the worker's last result (self._latest_doppler,
        updated via the _doppler_computed signal) for display, so the
        display never blocks on or waits for a rig CAT round-trip.
        """
        if self._selected_norad is None:
            return

        if self._selected_norad == MOON_ID:
            self._update_moon()
            return

        if self._engine is None:
            return

        obs = self._engine.observe(self._selected_norad)
        self._detail_panel.update_observation(obs)

        if obs is not None:
            item = self._sat_list.currentItem()
            name = item.text() if item else str(self._selected_norad)

            # Choose next pass info depending on whether a pass is currently active
            now = datetime.now(UTC)
            next_pass = next(
                (p for p in self._current_passes if p.los > now),
                None,
            )
            aos_t = next_pass.aos if next_pass is not None else None
            los_t = next_pass.los if next_pass is not None else None
            next_max_el = next_pass.max_elevation_deg if next_pass is not None else None
            next_dur = next_pass.duration_s if next_pass is not None else None

            # Compute track points from AOS to LOS at ~30-second intervals
            # for the next (or current) pass.
            pass_track: list[tuple[float, float]] = []
            if next_pass is not None:
                n_steps = max(20, min(40, int(next_pass.duration_s / 15)))
                step_s = next_pass.duration_s / n_steps
                for i in range(n_steps + 1):
                    t = next_pass.aos + timedelta(seconds=i * step_s)
                    pt = self._engine.observe(self._selected_norad, at=t)
                    if pt is not None:
                        pass_track.append((pt.azimuth_deg, pt.elevation_deg))

            track = SatTrackData(
                name=name,
                norad_cat_id=self._selected_norad,
                azimuth_deg=obs.azimuth_deg,
                elevation_deg=obs.elevation_deg,
                is_visible=obs.is_above_horizon,
                track=pass_track,
                aos_time=aos_t,
                los_time=los_t,
                next_max_el=next_max_el,
                next_duration_s=next_dur,
            )
            self._radar_view.set_tracks([track])
            self._detail_panel.update_radar_track(track)

            # Dashboard: position/track always; Doppler frequencies layered
            # in from the worker's last result once a transmitter is
            # selected and at least one cycle has completed.
            swa = self._engine.subpoint_with_alt(self._selected_norad)
            d = self._latest_doppler
            if self._current_transmitter is not None and d is not None:
                self._dashboard_view.update_observation(
                    obs,
                    subpoint=swa,
                    sat_color=SAT_COLORS[0],
                    dl_hz=d.dl_corr,
                    ul_hz=d.ul_corr,
                    dl_doppler=d.dl_shift,
                    ul_doppler=d.ul_shift,
                    track_data=track,
                )
            else:
                self._dashboard_view.update_observation(obs, subpoint=swa, track_data=track)

        # Send AZ/EL to the rotator every tick (same non-blocking pattern as rig).
        self._send_to_rotator(obs)

        self._radio_control.refresh_status()
        self._update_rig_label()
        self._update_rot_label()

    def _is_dial_feedback_rig(self, rig: RigController | None) -> bool:
        """True for rig configurations dial feedback works for (see
        _rig_send()'s comment in _doppler_cycle()):
        - HamlibNetController using a Yaesu-CAT NET-mode ctcss_method
          ("ftx1"/"ft991"), or any other non-satmode HamlibNetController
          with ctcss_method == "hamlib" (the "Hamlib standard" default --
          covers IC-705 NET mode and any other generic NET rig, which
          Rig Settings cannot distinguish from each other: ctcss_method
          only has "hamlib"/"ftx1"/"ft991"/"custom_cat" as values, with no
          per-model option, unlike Direct mode's model_id). UNVERIFIED for
          this "hamlib" bucket beyond what's already true for any NET-mode
          rig: get_frequency()/get_split_frequency() send bare "f"/"i"
          (RIG_VFO_CURR, no VFO argument) which never needs an internal
          VFO switch for ANY rig (asking "what is the current VFO's
          frequency" is trivially safe -- there is nothing to switch to).
          What is NOT verified for rigs other than FTX-1F is whether
          rigctld's own current_vfo bookkeeping reliably stays pointed at
          DL after _init_vfo()'s split-init sequence -- this can only be
          confirmed live (see the "first cycle sometimes reads UL instead
          of DL, self-heals" quirk documented for FTX-1F elsewhere in this
          file). _lock_watch_cycle()'s pre-Connect path stays scoped to
          "ftx1"/"ft991" only (read_dl_ul_independent() itself still gates
          on that) -- the "hamlib" bucket only works once connected.
        - HamlibNetController satmode rigs (IC-9100/9700 etc., "Icom SAT
          mode rig" checkbox checked), its own explicit branch independent
          of ctcss_method. Reads DL only, via the same bare "f"
          (RIG_VFO_CURR) already used above -- never calls
          get_split_frequency() ("i") or writes UL at all while Lock is on
          (see _rig_send()'s satmode-NET branch for the full rationale).
          Originally (2026-07-20) this read DL via "f" and UL via "i", on
          the theory that "hardware Main/Sub auto-tracking would make the
          UL write redundant, but the UL read (for the DL/UL cross-check)
          was still safe since rigctld's own current-VFO bookkeeping was
          untested but presumed OK." That was wrong: confirmed live
          (2026-07-22, IC-9100 NET mode, manual rigctld testing) that
          rigctld's "i" command internally calls Hamlib's
          rig_get_split_freq() (src/rig.c), which for a rig with
          targetable_vfo == 0 (IC-9100; IC-9700 is spared -- its
          targetable_vfo includes RIG_TARGETABLE_FREQ and takes a
          swap-free fast path instead) falls through to "Assisted mode":
          it calls set_vfo(tx_vfo) to read Sub, then tries to restore
          set_vfo(RIG_VFO_MAIN) afterward -- but that restore's return
          code is effectively ignored ("try and revert even if we had an
          error above"). When the restore silently fails on real hardware,
          rigctld's own "current VFO" stays stuck on Sub, so every later
          bare "f" keeps returning Sub's (UL/VHF-band) frequency instead
          of Main's -- observed live as "Lock works perfectly on the first
          Connect, then reads become unstable/implausible after
          reconnecting" (repeated "implausible DL reading 145840000.0 Hz"
          log lines -- a UL-band value where a UHF DL was expected). Same
          fix as the Direct-mode satmode branch: never read or write UL at
          all, relying on the same hardware Main->Sub auto-tracking
          confirmed live on real IC-9100 hardware. Same caveat as the
          "hamlib" bucket above still applies to the "f" read itself
          (whether rigctld's current_vfo bookkeeping reliably stays on
          Main is only checked empirically, not provable from Hamlib
          source alone), and satmode's cross-band case additionally makes
          Hamlib toggle actual hardware SATMODE
          (set_func(RIG_FUNC_SATMODE, 1)) -- a stateful hardware mode
          switch qualitatively different from every other rig's purely
          software/virtual split -- but that risk is unchanged from
          before this fix and orthogonal to the "i" command problem fixed
          here.
        - HamlibDirectController for FTX-1F (model 1051) or FT-991/FT-991A
          (models 1035/1036). Confirmed safe via Hamlib source
          (rigs/yaesu/ftx1/ftx1.c: .targetable_vfo = RIG_TARGETABLE_ALL;
          rigs/yaesu/ft991.c: .targetable_vfo = RIG_TARGETABLE_FREQ) --
          both backends' get_freq(VFOA)/get_freq(VFOB) query the CAT
          commands FA;/FB; directly with no internal VFO switch, unlike
          NET mode's forbidden "V" command. Neither rig's Direct-mode UL
          write goes through set_split_freq() (FTX-1F: plain
          set_freq(VFOB); FT-991: raw CAT "FB;" bypassing Hamlib entirely),
          so the VFOA/VFOB cache-slot-sharing quirk documented for
          set_split_freq() (see the "Directモードへの展開" section in
          CLAUDE.md) does not apply to either -- read order between DL and
          UL does not matter for either rig.
        - HamlibDirectController for IC-705 (model 3085), its own explicit
          branch (deliberately kept separate from FTX-1F/FT-991 above and
          from the generic fallback below, so a future IC-705-specific fix
          cannot silently affect an unrelated rig the way IC-705's own
          set_vfo(VFOA) display-restore once leaked into FTX-1F's shared
          "generic" write branch -- see "Connect直後にTXがSubからMainへ
          勝手に戻るバグと修正" in CLAUDE.md). Confirmed via Hamlib source
          (rigs/icom/ic7300.c: .targetable_vfo includes RIG_TARGETABLE_FREQ;
          rigs/icom/icom.c: icom_get_freq() uses CI-V command 0x25 to read
          the requested VFO directly, no VFO swap, since IC-705 has no
          Main/Sub+A/B combination that would force one) -- same safety
          property as FTX-1F/FT-991.
        - HamlibDirectController satmode rigs (IC-9100/9700 etc.), its own
          explicit branch (2026-07-20, cross-band use only -- Lock is a
          linear-transponder feature; same-band FM/repeater use is out of
          scope by the user's explicit call, so its correctness there is
          untested and not a concern). Reads DL only, via "Main"
          (RIG_VFO_MAIN) -- never reads or writes UL at all while Lock is
          on (see _rig_send()'s satmode branch for the full rationale).
          Originally this also read UL via "TX" (RIG_VFO_TX), on the
          theory that RIG_VFO_TX had its own dedicated swap-free read path
          (icom_get_tx_freq(), CI-V S_RD_TX_FREQ). That theory was wrong:
          confirmed via Hamlib source (src/misc.c: vfo_fixup(), called
          from the generic rig_get_freq() *before* icom_get_freq() ever
          runs) that RIG_VFO_TX gets unconditionally remapped to
          RIG_VFO_SUB for any VFO_HAS_MAIN_SUB_A_B_ONLY rig in satmode --
          so icom_get_freq()'s dedicated TX-read branch was never actually
          reached, and every "TX" read was silently forcing the
          VFO_HAS_MAIN_SUB_A_B_ONLY force_vfo_swap path instead (an actual
          VFO switch via set_vfo_curr()). Confirmed live (2026-07-22,
          IC-9100) that this corrupted Hamlib's internal VFO tracking
          badly enough to freeze DL reads and hang the serial link
          entirely on the next write after Lock was turned back off (Qt
          main thread blocked -> "Python is not responding"). Separately,
          the user confirmed live that satmode rigs already move Sub (TX)
          in hardware whenever Main (RX) is retuned, with no CAT command
          involved -- so there was nothing useful for software to add by
          writing UL here anyway. Reading Main is unaffected by any of
          this (vfo_fixup() only remaps RIG_VFO_TX/_SUB/_SUB_A/_SUB_B,
          never RIG_VFO_MAIN), so DL-only reading remains safe.
        - Any other HamlibDirectController that isn't satmode: a generic,
          best-effort fallback for whichever Hamlib backend rigctld ends up
          routing to. UNVERIFIED -- get_freq(VFOA)/get_freq(VFOB) safety
          depends on that specific rig's own Hamlib backend declaring
          RIG_TARGETABLE_FREQ the same way FTX-1F/FT-991/IC-705 do above;
          a rig that doesn't could hit the same kind of live-only-
          discoverable quirk FTX-1F's set_vfo(VFOA) restore turned out to
          have. Treat any new rig landing in this bucket the same way as
          FTX-1F/FT-991/IC-705 were treated: verify against Hamlib source
          and real hardware before trusting it, and give it its own
          explicit branch here once confirmed.

        Connected-only for now for every Direct-mode case above and for
        the generic/satmode NET buckets; "ftx1"/"ft991" NET mode remains
        the only pre-Connect-capable configuration
        (_lock_watch_cycle()'s own "ftx1"/"ft991" gate on
        read_dl_ul_independent() is unchanged -- satmode NET rigs reaching
        _lock_watch_cycle() before Connect are caught by the same
        ctcss_method check and no-op there too).
        """
        if isinstance(rig, HamlibNetController):
            if rig._ctcss_method in ("ftx1", "ft991"):
                return True
            if rig.is_satmode:
                return True
            return rig._ctcss_method == "hamlib"
        # Every HamlibDirectController is now covered: FTX-1F/FT-991/IC-705
        # explicitly verified above, satmode its own explicit branch (see
        # docstring), and anything else the generic best-effort fallback.
        # _rig_send() still branches on rig.is_satmode to pick the right
        # VFO strings ("Main"/"TX" vs "VFOA"/"VFOB") -- that distinction
        # belongs there, not in this eligibility gate.
        return isinstance(rig, HamlibDirectController)

    def _lock_watch_cycle(self) -> None:
        """Pre-Connect only: read the operator's current manual DL position
        via an independent short-lived connection and update
        self._dial_feedback_offset_hz accordingly (requirement 3: works
        even before Connect is pressed).

        Called from _lock_watch_worker's own background thread at a fixed
        interval, only while Lock (L button) is enabled (the worker is
        started/stopped in _on_lock_changed()).

        Confirmed explicitly with the user (2026-07-16): while Lock is on
        and not yet connected, nothing is written to the rig at all --
        this method only ever reads and updates
        self._dial_feedback_offset_hz for later use. This is simpler than
        the connected case (see _rig_send()): once Connect is pressed, UL
        does resume being written every cycle (there is no operator hand
        physically fighting for control of UL, only DL), but there is no
        equivalent "keep writing UL" mechanism before Connect -- doing so
        would require reintroducing an independent-connection write path
        (removed once it had no caller) for a state (not yet connected,
        not yet actively tracking) where it is unlikely to matter in
        practice. If this turns out to matter, add a
        write_ul_independent()-equivalent back and call it here the same
        way _rig_send() calls set_vfo_frequencies(None, ul).

        Once connected, this method is a no-op (returns immediately) --
        the read step moves into _rig_send() instead, on the same cadence
        as the Doppler cycle. This method mirrors that same direct-
        computation approach for the pre-Connect case, computing its own
        Doppler baseline from self._engine since _doppler_cycle() isn't
        driving anything yet.

        Scope: Rig 1 only, and only for HamlibNetController instances with
        ctcss_method in ("ftx1", "ft991") -- the Yaesu-CAT NET-mode rigs.
        This is the only configuration verified live (FTX-1F) to support
        frequency readback without the "V" (VFO switch) command, which was
        separately confirmed live to corrupt the rig's actual Main/Sub
        role assignment from even a single use -- not merely a cosmetic
        TX-LED flicker as previously documented here -- while software-
        side readbacks (f/i/get_vfo) continued to look entirely self-
        consistent throughout. "V" must never be used here.

        HamlibDirectController (FTX-1F and FT-991/FT-991A since
        2026-07-20; IC-705 and any other non-satmode Direct-mode rig also
        since 2026-07-20, see _is_dial_feedback_rig()) is also a
        dial-feedback rig, but only for the *connected* case -- this
        pre-Connect poller stays NET-mode-only for now, since a Direct-mode
        pre-Connect read would need its own short-lived Hamlib session
        (same risk class as _apply_mode_and_ctcss_hamlib()'s/
        _apply_mode_and_ctcss_cat_ft991()'s existing pre-Connect CTCSS
        paths, both of which deliberately avoid Hamlib via raw CAT instead
        -- see "FTX-1F 固有の制約"/"FT-991 / FT-991A" in CLAUDE.md). Only
        satmode Icom rigs keep the existing Doppler-driven-only Lock
        behaviour unchanged entirely; this method is a no-op for them
        (self._dial_feedback_offset_hz simply stays whatever it last was).

        Read mechanism: bare "f"/"i" (get_freq/get_split_freq), mirroring
        the existing "F"/"I" write-only Doppler-cycle protocol exactly --
        no VFO argument, no "V" command anywhere. A read-only poll (no F/I
        ever sent in this connection) reads correctly from the very first
        cycle (confirmed live, FTX-1F) -- unlike the connected case's
        persistent connection, which can briefly return a stale reading
        right after its first post-connect write (see _rig_send()). Every
        DL reading is still cross-checked against the UL reading from the
        same poll as defence-in-depth -- if they are suspiciously close
        (_DIAL_FEEDBACK_CROSSCHECK_HZ), the DL value almost certainly came
        from "current VFO" being stuck on Sub, and the whole cycle is
        discarded.
        """
        if not self._trsp_lock:
            return
        rig = self._rig_controller
        if not self._is_dial_feedback_rig(rig):
            return
        if not isinstance(rig, HamlibNetController):
            # Direct mode (FTX-1F, FT-991, IC-705, generic) is also a
            # dial-feedback rig per _is_dial_feedback_rig(), but connected-
            # case only -- this pre-Connect poller stays NET-mode-only (see
            # docstring above). Without this check, a Direct-mode rig
            # reaching this point (Lock left on from a previous test, rig
            # switched to Direct mode in Rig Settings) would hit an assert
            # below.
            return
        if rig._ctcss_method not in ("ftx1", "ft991"):
            # The generic NET "hamlib" bucket (2026-07-20, see
            # _is_dial_feedback_rig()) is connected-case only too --
            # read_dl_ul_independent() itself still only supports "ftx1"/
            # "ft991" and would silently return None here, which would
            # otherwise log a misleading "read failed" every cycle for a
            # rig that was never eligible for this pre-Connect path.
            return
        if rig.is_connected:
            return
        if self._selected_norad is None or self._selected_norad == MOON_ID:
            return
        if self._engine is None or self._current_transmitter is None:
            return
        obs = self._engine.observe(self._selected_norad)
        if obs is None:
            return
        rr = obs.range_rate_km_s
        dl_nom = self._current_transmitter.get("downlink_low")
        if dl_nom is None:
            return
        dl_baseline, _ = DopplerCalculator.correct_downlink(float(dl_nom), rr)

        result = rig.read_dl_ul_independent()
        if result is None:
            logger.info(
                "LockWatch (pre-Connect): read_dl_ul_independent() failed, "
                "offset unchanged at %.1fHz",
                self._dial_feedback_offset_hz,
            )
            return
        live_dl, live_ul = result
        if live_dl < 0 or live_ul < 0:
            logger.info(
                "LockWatch (pre-Connect): read failed (live_dl=%.1f live_ul=%.1f), "
                "offset unchanged at %.1fHz",
                live_dl,
                live_ul,
                self._dial_feedback_offset_hz,
            )
            return
        if abs(live_dl - live_ul) < _DIAL_FEEDBACK_CROSSCHECK_HZ:
            logger.info(
                "LockWatch (pre-Connect): DL reading %.1f too close to UL %.1f, "
                "discarding (current VFO likely stuck on Sub), offset unchanged at %.1fHz",
                live_dl,
                live_ul,
                self._dial_feedback_offset_hz,
            )
            return

        new_offset = live_dl - dl_baseline
        if abs(new_offset) > _DIAL_FEEDBACK_SANITY_HZ:
            logger.warning(
                "LockWatch (pre-Connect): implausible DL reading %.1f Hz "
                "(baseline %.1f), ignoring, offset unchanged at %.1fHz",
                live_dl,
                dl_baseline,
                self._dial_feedback_offset_hz,
            )
            return
        if new_offset != self._dial_feedback_offset_hz:
            logger.info(
                "LockWatch (pre-Connect): offset updated %.1fHz -> %.1fHz "
                "(live_dl=%.1f dl_baseline=%.1f)",
                self._dial_feedback_offset_hz,
                new_offset,
                live_dl,
                dl_baseline,
            )
        self._dial_feedback_offset_hz = new_offset

    def _doppler_cycle(self) -> None:
        """Compute Doppler correction and send it to the rig(s).

        Called from DopplerWorker's own background thread at a precise
        interval (the "Cycle" setting), independent of _on_tick()/the Qt
        main thread — see core/doppler_worker.py's module docstring for
        why. Reads plain MainWindow attributes (safe cross-thread — none
        of them are Qt widgets) and must not touch any Qt widget directly;
        the one thing display-relevant here (the DopplerDisplayUpdate) is
        delivered to the main thread via the _doppler_computed signal, and
        the actual rig sends reuse the exact same busy-lock + background-
        thread pattern this logic always used (so this thread's own loop
        never blocks on a CAT round-trip either).

        Excludes the Moon/EME case (MOON_ID) — _update_moon() still runs
        its own equivalent logic from _on_tick(), unchanged; Moon passes
        are far less time-sensitive than satellite passes and merging the
        two paths was judged not worth the added risk here (2026-07-10).
        """
        if self._selected_norad is None or self._selected_norad == MOON_ID:
            return
        if self._engine is None or self._current_transmitter is None:
            return

        obs = self._engine.observe(self._selected_norad)
        if obs is None:
            return

        rr = obs.range_rate_km_s
        dl_nom = self._current_transmitter.get("downlink_low")
        ul_nom = self._current_transmitter.get("uplink_low")
        invert = bool(self._current_transmitter.get("invert", False))
        mode = self._current_transmitter.get("mode")
        # Persistent per-transponder RX offset (GitHub Issue #18): a fixed,
        # known correction (e.g. satellite TCXO aging) that the operator no
        # longer has to rediscover every pass. Folded into the nominal DL
        # *before* Doppler correction (rather than added to dl_corr after,
        # like Lock's dial-feedback offset below) so it also carries through
        # to _on_tune_requested()'s band-centre recompute automatically.
        rx_offset_hz = float(self._current_transmitter.get("rx_offset_hz") or 0.0)
        if dl_nom is not None and rx_offset_hz != 0.0:
            dl_nom = float(dl_nom) + rx_offset_hz
        dl_corr, dl_shift = (
            DopplerCalculator.correct_downlink(float(dl_nom), rr)
            if dl_nom is not None
            else (None, None)
        )
        # UL always uses its OWN independently-scaled Doppler correction,
        # Lock or not -- carrier-frequency-proportional (e.g. a ~3:1 ratio
        # for a 435MHz/145MHz V/U transponder like RS-44), matching
        # DopplerCalculator's physics-based formula (shift = -nominal_hz *
        # rr / c applied to ul_nom, not dl_nom). Lock must NOT replace this
        # with a raw copy of DL's shift-from-nominal -- that discards the
        # correct per-band scaling entirely. Confirmed live (2026-07-15):
        # the previous Lock branch computed ul_corr as
        # "ul_low + (dl_corr - dl_low_nom)", i.e. mirrored DL's ENTIRE
        # absolute shift onto UL 1:1 regardless of band -- pressing Lock on
        # a V/U transponder made UL move by the exact same absolute Hz as
        # DL (should be ~1/3) and permanently blanked the UL Doppler-shift
        # display (ul_shift was unconditionally set to None). This was a
        # pre-existing flaw in the Lock feature itself, not something
        # introduced by dial feedback -- dial feedback's offset was simply
        # being fed into the same already-broken formula.
        ul_corr, ul_shift = (
            DopplerCalculator.correct_uplink(float(ul_nom), rr)
            if ul_nom is not None
            else (None, None)
        )
        # dl_corr_base (pure Doppler, no manual offset) is needed by
        # _rig_send() below to recompute the offset directly from a fresh
        # DL reading each cycle while Lock is on.
        dl_corr_base = dl_corr
        if self._dial_feedback_offset_hz != 0.0:
            # The manual dial-feedback offset (accumulated by Lock's
            # read-only DL tracking -- see _rig_send()) applies
            # UNCONDITIONALLY, Lock on or off, not just while
            # self._trsp_lock is True. This is deliberate (confirmed
            # explicitly with the user, 2026-07-16): turning Lock off
            # must resume Doppler correction from the manually-adjusted
            # frequency, not snap back to the pure nominal-based
            # trajectory. The offset's *value* only changes while Lock is
            # on (_rig_send() only updates it when do_dial_feedback is
            # true); with Lock off it simply rides along unchanged,
            # continuing to shift both DL and UL by the same fixed amount
            # every cycle. It resets to 0 only on an explicit "start
            # over": selecting a different transponder, or pressing the T
            # (Tune) button. Raw, unscaled Hz delta -- matching GPredict's
            # own dial-feedback algorithm -- sign-flipped for inverting
            # transponders. dl_shift/ul_shift (pure Doppler-only shift)
            # are no longer accurate figures once a manual offset is
            # mixed in, so they are cleared for display.
            if dl_corr is not None:
                dl_corr = dl_corr + self._dial_feedback_offset_hz
                dl_shift = None
            if ul_corr is not None:
                ul_corr = ul_corr + (
                    -self._dial_feedback_offset_hz if invert else self._dial_feedback_offset_hz
                )
                ul_shift = None
        # If the Tune button has set an override, use the centre frequency,
        # then reset to None afterward (subsequent cycles return to
        # Doppler-corrected values). This worker is the only place that
        # consumes _tune_dl_override/_tune_ul_override for the satellite
        # path — _update_moon() has its own, independent pair of reads for
        # the Moon/EME path, so there is no double-consumption risk between
        # the two (only one is ever active, since MOON_ID is excluded above).
        if self._tune_dl_override is not None:
            dl_corr = self._tune_dl_override
            dl_shift = None
            self._tune_dl_override = None
        if self._tune_ul_override is not None:
            ul_corr = self._tune_ul_override
            ul_shift = None
            self._tune_ul_override = None

        ctcss_display = (
            self._current_ctcss_tone
            if self._current_ctcss_tone is not None
            else self._ctcss_tone_hz
        )
        self._doppler_computed.emit(
            DopplerDisplayUpdate(
                dl_nom=dl_nom,
                dl_corr=dl_corr,
                dl_shift=dl_shift,
                ul_nom=ul_nom,
                ul_corr=ul_corr,
                ul_shift=ul_shift,
                mode=mode,
                ctcss_display=ctcss_display,
            )
        )

        # Transmit Doppler-corrected frequencies to the connected rig (regardless of elevation).
        # set_vfo_frequencies() involves TCP communication with recv(), so calling it directly
        # here would block this worker's own precise wake-up loop.
        # Use _rig_busy_lock: if the previous cycle has finished, transmit on a background
        # thread; if the previous cycle is still running, skip this cycle.
        # Passband tune offset: apply to the SDR rig's DL, and when Lock is
        # ON mirror it to the other rig's TX (sign inverted for inverted
        # transponders).  Works regardless of whether SDR is Rig 1 or Rig 2.
        tune = self._sdr_tune_offset
        sdr_is_rig1 = (
            self._rig_controller is not None
            and getattr(self._rig_controller, "is_sdr", False)
            and self._rig_controller.is_connected
        )
        sdr_is_rig2 = (
            self._rig2_controller is not None
            and getattr(self._rig2_controller, "is_sdr", False)
            and self._rig2_controller.is_connected
        )
        # DL for Rig 1: add tune offset when SDR is Rig 1
        dl_rig1 = (dl_corr + tune) if (dl_corr is not None and sdr_is_rig1) else dl_corr
        # UL for Rig 1: mirror tune offset when SDR is Rig 2 and Lock is ON
        ul_rig1 = ul_corr
        if sdr_is_rig2 and self._trsp_lock and tune != 0.0 and ul_rig1 is not None:
            ul_rig1 = ul_rig1 + (-tune if invert else tune)

        if self._rig_controller is not None and self._rig_controller.is_connected:
            if self._rig_busy_lock.acquire(blocking=False):
                rig = self._rig_controller
                dl = dl_rig1
                ul = ul_rig1
                do_dial_feedback = self._trsp_lock and self._is_dial_feedback_rig(rig)
                # Baseline (pure Doppler, no manual offset) for
                # _rig_send()'s direct offset recompute below.
                dl_baseline = dl_corr_base
                # SDR Lock (SdrControlWidget's own "L" button, independent
                # of _trsp_lock above): baseline is dl_corr (post dial-
                # feedback fold, pre _sdr_tune_offset) so that turning Lock
                # back off and resuming the ordinary dl_rig1 = dl_corr +
                # _sdr_tune_offset write below reproduces the exact
                # frequency the SDR was left at -- see _rig_send()'s SDR
                # Lock branch.
                do_sdr_lock = sdr_is_rig1 and self._sdr_lock
                dl_corr_for_sdr_lock = dl_corr

                def _rig_send() -> None:
                    try:
                        if do_dial_feedback:
                            # Lock ON: DL stops being written to the rig
                            # entirely -- it is under the operator's sole
                            # manual control, and a periodic absolute-
                            # frequency write would fight their real-time
                            # VFO motion (confirmed live, 2026-07-16,
                            # FTX-1F: "the frequency reverts even while
                            # I'm still turning the dial, I can't even
                            # tell how far I turned it"). Each cycle only
                            # reads DL/UL to keep
                            # self._dial_feedback_offset_hz (and, via
                            # _doppler_cycle()'s unconditional fold, both
                            # the software's own display and "ul" below)
                            # up to date.
                            #
                            # UL, unlike DL, has no competing physical
                            # control to fight -- the operator only
                            # touches the DL VFO -- so it keeps being
                            # written every cycle even while Lock is on
                            # (confirmed explicitly with the user,
                            # 2026-07-16): the actual transmitted uplink
                            # stays properly Doppler-compensated (plus the
                            # operator's DL offset, sign-flipped for
                            # inverting transponders) for the entire
                            # duration Lock is engaged, instead of
                            # freezing at whatever it was before Lock was
                            # pressed. "ul" here is the same value
                            # _doppler_cycle() already computed for the
                            # display (one cycle behind this read, same as
                            # the display itself), so the rig and the
                            # on-screen UL figure never disagree.
                            #
                            # The offset is recomputed fresh from the live
                            # reading each cycle (self._dial_feedback_offset_hz
                            # = live_dl - dl_baseline) rather than
                            # accumulated incrementally against a "last
                            # written" tracker -- simpler and safer now
                            # that DL is never written, since there is no
                            # longer a "did our own write actually land"
                            # ambiguity to get out of sync over (that
                            # ambiguity was the root cause of an earlier
                            # bug where ordinary Doppler drift got
                            # misdetected as manual retuning).
                            # NET mode (non-satmode: ftx1/ft991/generic
                            # hamlib): bare "f"/"i" (get_freq/get_split_freq,
                            # no VFO argument) -- mirrors the write side's
                            # "F"/"I" exactly, since rigctld's "V" command was
                            # confirmed live to corrupt the rig's Main/Sub
                            # role assignment. NET mode satmode is excluded
                            # from this and handled by its own branch below
                            # (bare "f" only, "i" never called).
                            # Direct mode (FTX-1F, FT-991/FT-991A): get_freq
                            # (VFOA)/get_freq(VFOB) via Hamlib directly.
                            # Confirmed via Hamlib source (ftx1.c:
                            # .targetable_vfo = RIG_TARGETABLE_ALL; ft991.c:
                            # .targetable_vfo = RIG_TARGETABLE_FREQ; both
                            # backends' get_freq sends FA;/FB; for the
                            # requested VFO with no VFO switch) that this
                            # carries none of NET mode's "V" corruption risk
                            # for either rig. VFOA/VFOB also use separate
                            # Hamlib cache slots (freqMainA/freqMainB,
                            # confirmed in Hamlib's cache.c, generic across
                            # all rigs) so, unlike set_split_freq()'s
                            # documented Main-cache-corruption workaround
                            # (ftx1_vfo.c), read order between the two does
                            # not matter here -- neither rig's Direct-mode UL
                            # write goes through set_split_freq()/
                            # get_split_freq() at all (FTX-1F: plain
                            # set_freq(VFOB); FT-991: raw CAT "FB;" bypassing
                            # Hamlib entirely).
                            # IC-705 and the generic (unverified) fallback
                            # rig also land here -- get_frequency() itself
                            # is model-agnostic. See _is_dial_feedback_rig()
                            # for the per-model safety verification (or
                            # lack thereof, for the generic bucket).
                            #
                            # Satmode (IC-9100/9700 etc., cross-band/linear-
                            # transponder use only) is handled entirely
                            # separately below -- read DL only, never read
                            # or write UL at all while Lock is on. Two
                            # independent reasons, confirmed live
                            # (2026-07-22, IC-9100), converged on this:
                            # (1) the rig itself already moves Sub (TX)
                            # automatically whenever Main (RX) is retuned,
                            # in hardware, with no CAT command involved --
                            # the user confirmed this directly on real
                            # hardware -- so there is nothing for software
                            # to add by writing UL here.
                            # (2) reading UL via "TX" does NOT dodge a VFO
                            # switch the way it does for the write path or
                            # for other rigs: Hamlib's generic vfo_fixup()
                            # (src/misc.c), called from rig_get_freq()
                            # *before* icom_get_freq() ever runs, remaps
                            # RIG_VFO_TX -> RIG_VFO_SUB unconditionally for
                            # any VFO_HAS_MAIN_SUB_A_B_ONLY rig in satmode
                            # ("else if (VFO_HAS_MAIN_SUB_A_B_ONLY &&
                            # satmode) { vfo = RIG_VFO_SUB; }") -- so
                            # icom_get_freq()'s own dedicated swap-free TX
                            # read (icom_get_tx_freq(), CI-V S_RD_TX_FREQ)
                            # never gets a chance to run at all; every "TX"
                            # read was actually forcing the
                            # VFO_HAS_MAIN_SUB_A_B_ONLY force_vfo_swap path
                            # (an explicit switch-away-and-back via
                            # set_vfo_curr()) every single cycle. On real
                            # IC-9100 hardware this corrupted Hamlib's
                            # internal VFO tracking badly enough that DL
                            # reads froze, and the very next write attempt
                            # after Lock was turned back off hung the
                            # serial link entirely (Qt main thread blocked
                            # -> "Python is not responding" -> force quit).
                            # Reading Main is unaffected by any of this --
                            # vfo_fixup() only remaps RIG_VFO_TX/_SUB/
                            # _SUB_A/_SUB_B, never RIG_VFO_MAIN.
                            if isinstance(rig, HamlibDirectController) and rig.is_satmode:
                                # Skip this cycle's read entirely (no Hamlib
                                # call at all) when our own last write left
                                # the rig on Sub -- see
                                # last_written_vfo_is_main()'s docstring for
                                # the full rationale (2026-07-22, confirmed
                                # live: this is exactly the moment a "Python
                                # not responding" hang was reproduced). DL
                                # gets rewritten almost every cycle, so this
                                # self-heals within about one cycle.
                                if not rig.last_written_vfo_is_main():
                                    logger.info(
                                        "LockWatch: skipping this cycle (last write was UL/Sub), "
                                        "offset unchanged at %.1fHz",
                                        self._dial_feedback_offset_hz,
                                    )
                                    return
                                live_dl = rig.get_frequency("Main")
                                if live_dl < 0:
                                    logger.info(
                                        "LockWatch: read failed (live_dl=%.1f), satmode -- "
                                        "UL not read/written, offset unchanged at %.1fHz",
                                        live_dl,
                                        self._dial_feedback_offset_hz,
                                    )
                                elif dl_baseline is not None:
                                    new_offset = live_dl - dl_baseline
                                    if abs(new_offset) > _DIAL_FEEDBACK_SANITY_HZ:
                                        logger.warning(
                                            "LockWatch: implausible DL reading %.1f Hz "
                                            "(baseline %.1f), ignoring this cycle, "
                                            "offset unchanged at %.1fHz",
                                            live_dl,
                                            dl_baseline,
                                            self._dial_feedback_offset_hz,
                                        )
                                    elif new_offset != self._dial_feedback_offset_hz:
                                        logger.info(
                                            "LockWatch: offset updated %.1fHz -> %.1fHz "
                                            "(live_dl=%.1f dl_baseline=%.1f, satmode)",
                                            self._dial_feedback_offset_hz,
                                            new_offset,
                                            live_dl,
                                            dl_baseline,
                                        )
                                        self._dial_feedback_offset_hz = new_offset
                                return
                            if isinstance(rig, HamlibNetController) and rig.is_satmode:
                                # Same root cause as the Direct-mode satmode
                                # branch above, confirmed live via manual
                                # rigctld testing (2026-07-22, IC-9100 NET
                                # mode): the "i" (get_split_freq) command
                                # rigctld sends internally calls Hamlib's
                                # rig_get_split_freq() -- which, for a rig
                                # with targetable_vfo == 0 (IC-9100; IC-9700
                                # is spared since its targetable_vfo includes
                                # RIG_TARGETABLE_FREQ and takes a swap-free
                                # fast path instead), falls through to
                                # "Assisted mode": it calls set_vfo(tx_vfo)
                                # to read Sub, then tries to restore
                                # set_vfo(RIG_VFO_MAIN) afterward -- but that
                                # restore's return code is effectively
                                # ignored ("try and revert even if we had an
                                # error above", src/rig.c). When the restore
                                # silently fails on real hardware, rigctld's
                                # own "current VFO" stays stuck on Sub, so
                                # every later bare "f" (current-VFO get_freq,
                                # no VFO argument) keeps returning Sub's (UL/
                                # VHF-band) frequency instead of Main's --
                                # exactly the "implausible DL reading
                                # 145840000.0 Hz" log lines seen live (a UL-
                                # band value where a UHF DL was expected).
                                # This explains "works perfectly on the
                                # first Connect, unstable after reconnecting"
                                # (whatever made the first restore succeed
                                # doesn't reliably repeat). Same fix as
                                # Direct mode: never call
                                # get_split_frequency() ("i") at all here --
                                # read DL only via the same bare "f" already
                                # used for non-satmode NET rigs, and rely on
                                # the rig's own confirmed hardware Main->Sub
                                # auto-tracking instead of writing UL.
                                live_dl = rig.get_frequency()
                                if live_dl < 0:
                                    logger.info(
                                        "LockWatch: read failed (live_dl=%.1f), satmode NET -- "
                                        "UL not read/written, offset unchanged at %.1fHz",
                                        live_dl,
                                        self._dial_feedback_offset_hz,
                                    )
                                elif dl_baseline is not None:
                                    new_offset = live_dl - dl_baseline
                                    if abs(new_offset) > _DIAL_FEEDBACK_SANITY_HZ:
                                        logger.warning(
                                            "LockWatch: implausible DL reading %.1f Hz "
                                            "(baseline %.1f), ignoring this cycle, "
                                            "offset unchanged at %.1fHz",
                                            live_dl,
                                            dl_baseline,
                                            self._dial_feedback_offset_hz,
                                        )
                                    elif new_offset != self._dial_feedback_offset_hz:
                                        logger.info(
                                            "LockWatch: offset updated %.1fHz -> %.1fHz "
                                            "(live_dl=%.1f dl_baseline=%.1f, satmode NET)",
                                            self._dial_feedback_offset_hz,
                                            new_offset,
                                            live_dl,
                                            dl_baseline,
                                        )
                                        self._dial_feedback_offset_hz = new_offset
                                return
                            if isinstance(rig, HamlibNetController):
                                live_dl = rig.get_frequency()
                                live_ul = rig.get_split_frequency()
                            else:
                                assert isinstance(rig, HamlibDirectController)
                                live_dl = rig.get_frequency("VFOA")
                                live_ul = rig.get_frequency("VFOB")
                            if live_dl < 0 or live_ul < 0:
                                logger.info(
                                    "LockWatch: read failed (live_dl=%.1f live_ul=%.1f), "
                                    "offset unchanged at %.1fHz",
                                    live_dl,
                                    live_ul,
                                    self._dial_feedback_offset_hz,
                                )
                            elif abs(live_dl - live_ul) < _DIAL_FEEDBACK_CROSSCHECK_HZ:
                                logger.info(
                                    "LockWatch: DL reading %.1f too close to UL %.1f, "
                                    "discarding (current VFO likely stuck on Sub), "
                                    "offset unchanged at %.1fHz",
                                    live_dl,
                                    live_ul,
                                    self._dial_feedback_offset_hz,
                                )
                            elif dl_baseline is not None:
                                new_offset = live_dl - dl_baseline
                                if abs(new_offset) > _DIAL_FEEDBACK_SANITY_HZ:
                                    logger.warning(
                                        "LockWatch: implausible DL reading %.1f Hz "
                                        "(baseline %.1f), ignoring this cycle, "
                                        "offset unchanged at %.1fHz",
                                        live_dl,
                                        dl_baseline,
                                        self._dial_feedback_offset_hz,
                                    )
                                elif new_offset != self._dial_feedback_offset_hz:
                                    logger.info(
                                        "LockWatch: offset updated %.1fHz -> %.1fHz "
                                        "(live_dl=%.1f dl_baseline=%.1f)",
                                        self._dial_feedback_offset_hz,
                                        new_offset,
                                        live_dl,
                                        dl_baseline,
                                    )
                                    self._dial_feedback_offset_hz = new_offset
                            rig.set_vfo_frequencies(None, ul)
                            return
                        if do_sdr_lock:
                            # Stop writing an absolute frequency to the SDR
                            # entirely -- Passband Tune / Freq+Tune are free
                            # to move it -- and instead recompute
                            # _sdr_tune_offset fresh from the SDR's actual
                            # live frequency every cycle, so turning Lock
                            # back off resumes Doppler correction from
                            # wherever it was left (dl_rig1 = dl_corr +
                            # _sdr_tune_offset above already does this
                            # automatically once do_sdr_lock is false again
                            # -- no separate "on release" step needed).
                            live_freq = rig.get_frequency()
                            if live_freq > 0 and dl_corr_for_sdr_lock is not None:
                                new_offset = live_freq - dl_corr_for_sdr_lock
                                self._sdr_tune_offset = new_offset
                                self._sdr_lock_offset_computed.emit(new_offset)
                            return
                        rig.set_vfo_frequencies(dl, ul)
                    except RigControlError as exc:
                        self._rig_error.emit(str(exc))
                    except Exception as exc:
                        logger.error("RigNet: unexpected error in send thread: %s", exc)
                        self._rig_error.emit(str(exc))
                    finally:
                        self._rig_busy_lock.release()

                threading.Thread(target=_rig_send, daemon=True).start()
            else:
                logger.debug("RigNet: previous cycle still running, skipping tick")

        # Transmit Doppler-corrected frequencies to Rig 2 (same non-blocking pattern).
        if self._rig2_controller is not None and self._rig2_controller.is_connected:
            if self._rig2_busy_lock.acquire(blocking=False):
                rig2 = self._rig2_controller
                # DL for Rig 2: add tune offset when SDR is Rig 2
                dl2 = (dl_corr + tune) if (dl_corr is not None and sdr_is_rig2) else dl_corr
                # UL for Rig 2: mirror tune offset when SDR is Rig 1 and Lock is ON
                ul2 = ul_corr
                if sdr_is_rig1 and self._trsp_lock and tune != 0.0 and ul2 is not None:
                    ul2 = ul2 + (-tune if invert else tune)
                # SDR Lock for Rig 2 — same idea as the Rig 1 branch above.
                do_sdr_lock2 = sdr_is_rig2 and self._sdr_lock
                dl_corr_for_sdr_lock2 = dl_corr

                def _rig2_send() -> None:
                    try:
                        if do_sdr_lock2:
                            live_freq = rig2.get_frequency()
                            if live_freq > 0 and dl_corr_for_sdr_lock2 is not None:
                                new_offset = live_freq - dl_corr_for_sdr_lock2
                                self._sdr_tune_offset = new_offset
                                self._sdr_lock_offset_computed.emit(new_offset)
                            return
                        rig2.set_vfo_frequencies(dl2, ul2)
                    except RigControlError as exc:
                        self._rig_error.emit(str(exc))
                    except Exception as exc:
                        logger.error("Rig2: unexpected error in send thread: %s", exc)
                        self._rig_error.emit(str(exc))
                    finally:
                        self._rig2_busy_lock.release()

                threading.Thread(target=_rig2_send, daemon=True).start()
            else:
                logger.debug("Rig2: previous cycle still running, skipping tick")

    @Slot(object)
    def _on_doppler_computed(self, result: DopplerDisplayUpdate) -> None:
        """Main-thread slot for DopplerWorker's results — see _doppler_cycle()."""
        self._latest_doppler = result
        self._radio_control.update_doppler(
            result.dl_nom,
            result.dl_corr,
            result.dl_shift,
            result.ul_nom,
            result.ul_corr,
            result.ul_shift,
            result.mode,
            result.ctcss_display,
        )

    def _on_rig_error(self, msg: str) -> None:
        """Display an error from the background rig thread in the status bar (UI thread)."""
        logger.warning("RigControlError: %s", msg)
        sb = self.statusBar()
        if sb:
            # 8s rather than the previous 3s: this is often the only visible
            # sign of a rig-control failure for a user who cannot easily
            # pull fbsat59.log (e.g. testing on someone else's PC).
            sb.showMessage(f"RIG: {msg}", 8000)

    def _send_to_rotator(self, obs: Observation | None) -> None:
        """Send AZ/EL from obs to the rotator in a background thread (non-blocking)."""
        if (
            obs is not None
            and self._rotator_controller is not None
            and self._rotator_controller.is_connected
        ):
            if self._rot_busy_lock.acquire(blocking=False):
                rot = self._rotator_controller
                az = self._apply_south_offset(obs.azimuth_deg)
                el = obs.elevation_deg

                def _rot_send() -> None:
                    try:
                        rot.set_position(az, el)
                        logger.info("Rotator: set position az=%.1f el=%.1f", az, el)
                        pos = rot.get_position()
                        self._rot_pos_updated.emit(pos.azimuth_deg, pos.elevation_deg)
                    except Exception as exc:
                        logger.error("Rotator: set_position error: %s", exc)
                    finally:
                        self._rot_busy_lock.release()

                threading.Thread(target=_rot_send, daemon=True).start()
            else:
                logger.debug("Rotator: previous cycle still running, skipping tick")
        elif self._rotator_controller is None or not self._rotator_controller.is_connected:
            self._radar_view.set_rotator_position(None, None)
            self._dashboard_view._radar.set_rotator_position(None, None)

    def _on_rotator_pos_updated(self, rot_az: float, rot_el: float) -> None:
        """Update the radar rotator marker with the actual rotator position (UI thread)."""
        display_az = (rot_az - 180.0) % 360.0 if self._rotator_south_init else rot_az
        self._radar_view.set_rotator_position(display_az, rot_el)
        self._dashboard_view._radar.set_rotator_position(display_az, rot_el)

    def _update_statusbar(self) -> None:
        """Update the QTH text and TLE last-updated timestamp in the status bar."""
        if self._location_manager is not None:
            self._qth_label.setText(self._location_manager.status_text)

        row = self._conn.execute("SELECT MAX(fetched_at) AS last_fetch FROM tle_data").fetchone()
        if row and row["last_fetch"]:
            self._tle_label.setText(f"TLE: {str(row['last_fetch'])[:16]}")

    # ------------------------------------------------------------------ #
    # Satellite selection callbacks
    # ------------------------------------------------------------------ #

    def _on_tab_changed(self, index: int) -> None:
        """Hide the Satellite Detail panel when Dashboard tab is active (more space
        for map); drive the Comms Quick Panel and auto-resize the pass-prediction
        splitter when switching to/from a non-resident Communications tab, or
        while View > Wide Tab is enabled."""
        is_dashboard = index == self._dashboard_tab_idx
        self._detail_panel.setVisible(not is_dashboard)

        widget = self._tab_widget.widget(index)
        is_resident = widget is None or widget in self._resident_tab_widgets

        # _update_world_map()'s expensive per-satellite pass only runs while
        # Dashboard/World Map is active (see _is_map_tab_active) — refresh
        # immediately on switching to either so the map isn't showing a
        # stale snapshot from whenever it was last visible, rather than
        # waiting for the next 5s tick.
        if widget is self._dashboard_view or widget is self._world_map:
            self._update_world_map()

        self._update_pass_panel_size(hide=self._wide_tab_enabled or not is_resident)

        if is_resident:
            self._detail_panel.deactivate_comms_panel()
            return

        tab_key = self._comms_tab_keys.get(widget) if widget is not None else None
        if tab_key is not None:
            options = [
                (norad, self._sat_name_cache.get(norad, str(norad)))
                for norad in mode_detection.get_norads_for_tab(self._conn, tab_key)
            ]
            self._detail_panel.set_active_comms_tab(tab_key, options, tab_widget=widget)
        else:
            self._detail_panel.deactivate_comms_panel()

    def _update_pass_panel_size(self, hide: bool) -> None:
        """Collapse the pass-prediction (bottom) panel to zero height, or
        restore the sizes saved before it was last collapsed. The splitter
        handle stays visible and draggable (QSplitter.isCollapsible(1) is
        True) so the user can still pull the panel back out with the mouse.
        Shared by _on_tab_changed's per-tab auto-hide and View > Wide Tab's
        manual override, so the two never fight over _pass_panel_saved_sizes."""
        if hide:
            if self._pass_panel_saved_sizes is None:
                self._pass_panel_saved_sizes = self._v_splitter.sizes()
            total = sum(self._v_splitter.sizes())
            self._v_splitter.setSizes([total, 0])
        elif self._pass_panel_saved_sizes is not None:
            self._v_splitter.setSizes(self._pass_panel_saved_sizes)
            self._pass_panel_saved_sizes = None

    def _update_sat_list_panel_size(self, hide: bool) -> None:
        """Collapse the satellite list (left) panel to zero width for View >
        Wide Tab, or restore the h_splitter sizes saved beforehand. Like
        _update_pass_panel_size, the splitter handle stays draggable while
        collapsed (QSplitter.isCollapsible(0) is True)."""
        if hide:
            if self._wide_tab_saved_h_sizes is None:
                self._wide_tab_saved_h_sizes = self._h_splitter.sizes()
            sizes = self._h_splitter.sizes()
            total = sum(sizes)
            detail_width = sizes[2]
            self._h_splitter.setSizes([0, max(0, total - detail_width), detail_width])
        elif self._wide_tab_saved_h_sizes is not None:
            self._h_splitter.setSizes(self._wide_tab_saved_h_sizes)
            self._wide_tab_saved_h_sizes = None

    def _on_wide_tab_toggled(self, checked: bool) -> None:
        """View > Wide Tab: manually hide (or restore) the satellite list
        and pass-prediction panels so a Communications tab (FT4, SDR
        Control, etc.) gets more screen space. Both panels stay reachable
        by dragging their splitter handle back out (see
        _update_pass_panel_size / _update_sat_list_panel_size)."""
        self._wide_tab_enabled = checked
        widget = self._tab_widget.currentWidget()
        is_resident = widget is None or widget in self._resident_tab_widgets
        self._update_pass_panel_size(hide=checked or not is_resident)
        self._update_sat_list_panel_size(hide=checked)

    def _on_filter_changed(self, text: str) -> None:
        """Redraw the satellite list when the filter combo changes."""
        self._amsat_link.setVisible(text == "Operational (AMSAT)")
        self._apply_filter()

    def _on_search_changed(self, _text: str) -> None:
        """Re-filter the satellite list when the search box text changes."""
        self._apply_filter()

    def _on_sat_context_menu(self, pos: QPoint) -> None:
        """Show the right-click context menu for the satellite list."""
        item = self._sat_list.itemAt(pos)
        if item is None:
            return
        norad = int(item.data(Qt.ItemDataRole.UserRole))

        row_data = self._conn.execute(
            "SELECT name, is_favorite, is_hidden, favorite_group FROM satellites"
            " WHERE norad_cat_id = ?",
            (norad,),
        ).fetchone()
        if row_data is None:
            return

        name = str(row_data["name"])
        is_hidden = bool(row_data["is_hidden"])
        current_group: int = int(row_data["favorite_group"] or 0)

        # Load custom groups for submenu
        groups = self._conn.execute(
            "SELECT id, name FROM custom_groups ORDER BY sort_order, id"
        ).fetchall()

        menu = QMenu(self)
        fav_menu = menu.addMenu("★ " + _("Favorite Groups"))
        fav_actions: dict[int, QAction] = {}
        for grp in groups:
            grp_id = int(grp["id"])
            grp_name = str(grp["name"])
            act = fav_menu.addAction(f"★ {grp_name}")
            act.setCheckable(True)
            act.setChecked(current_group == grp_id)
            fav_actions[grp_id] = act
        if current_group > 0:
            fav_menu.addSeparator()
            remove_fav_action: QAction | None = fav_menu.addAction(_("Remove from Favorites"))
        else:
            remove_fav_action = None

        hide_label = _("Unhide Satellite") if is_hidden else _("Hide Satellite")
        hide_action = menu.addAction(hide_label)
        info_action = menu.addAction(_("Satellite Info..."))
        satnogs_action = menu.addAction(_("Open in SatNOGS"))

        # Deselect Satellite — stops tracking whatever is currently selected
        # (self._selected_norad), independent of which item was right-
        # clicked. There was previously no explicit UI action for this: the
        # satellite list keeps its current row selected even when a filter
        # change empties the list (deliberate — see the list-rebuild
        # blockSignals() comment elsewhere in this file), so a satellite
        # kept being tracked/Doppler-corrected with no obvious way to stop
        # it short of picking a different one.
        menu.addSeparator()
        deselect_action = menu.addAction(_("Deselect Satellite"))
        deselect_action.setEnabled(self._selected_norad is not None)

        action = menu.exec(self._sat_list.mapToGlobal(pos))
        if action is not None and action in fav_actions.values():
            chosen_id = next(k for k, v in fav_actions.items() if v == action)
            new_group = 0 if current_group == chosen_id else chosen_id
            self._set_favorite_group(norad, new_group)
        elif action is not None and action == remove_fav_action:
            self._set_favorite_group(norad, 0)
        elif action == hide_action:
            self._set_hidden(norad, not is_hidden)
        elif action == info_action:
            self._show_sat_info(norad, name)
        elif action == satnogs_action:
            self._open_in_satnogs(norad, name)
        elif action == deselect_action:
            self._sat_list.setCurrentRow(-1)

    def _open_in_satnogs(self, norad: int, name: str) -> None:
        """Open the SatNOGS satellite page. Uses DB cache; fetches UUID in background if needed."""
        row = self._conn.execute(
            "SELECT satnogs_uuid FROM satellites WHERE norad_cat_id = ?",
            (norad,),
        ).fetchone()
        cached = row["satnogs_uuid"] if row else None
        if cached:
            self._open_url_app_mode(f"https://db.satnogs.org/satellite/{cached}")
            return
        threading.Thread(
            target=self._fetch_satnogs_uuid_bg,
            args=(norad, name),
            daemon=True,
        ).start()

    def _fetch_satnogs_uuid_bg(self, norad: int, name: str) -> None:
        """Background thread: fetch SatNOGS UUID by NORAD, fall back to name/provisional search.

        A connection/timeout/HTTP-status failure is reported distinctly from a
        genuine "no match" — otherwise a network hiccup looks identical to
        "this satellite isn't on SatNOGS", which is misleading and makes the
        real problem (usually local connectivity) impossible to diagnose from
        the UI alone.
        """
        _SATNOGS_SAT_API = "https://db.satnogs.org/api/satellites/"
        sat_id: str | None = None
        network_error: str | None = None
        try:
            with httpx.Client(timeout=10.0) as client:
                # Primary: look up by NORAD ID
                r = client.get(_SATNOGS_SAT_API, params={"format": "json", "norad_cat_id": norad})
                r.raise_for_status()
                data = r.json()
                results = data.get("results", data) if isinstance(data, dict) else data
                if results:
                    sat_id = str(results[0]["sat_id"])

                # Fallback: search by satellite name, only accept an exact name match
                if not sat_id:
                    r2 = client.get(_SATNOGS_SAT_API, params={"format": "json", "search": name})
                    r2.raise_for_status()
                    data2 = r2.json()
                    results2 = data2.get("results", data2) if isinstance(data2, dict) else data2
                    name_lower = name.strip().lower()
                    for entry in results2:
                        entry_name = str(entry.get("name", "")).strip().lower()
                        alt_names = str(entry.get("names", "")).lower()
                        if entry_name == name_lower or name_lower in alt_names.split(","):
                            sat_id = str(entry["sat_id"])
                            break

                # Last resort: look up via provisional (satnogs_source_id) NORAD
                if not sat_id:
                    row = self._conn.execute(
                        "SELECT satnogs_source_id FROM satellites WHERE norad_cat_id = ?",
                        (norad,),
                    ).fetchone()
                    provisional = row[0] if row else None
                    if provisional:
                        r3 = client.get(
                            _SATNOGS_SAT_API,
                            params={"format": "json", "norad_cat_id": provisional},
                        )
                        r3.raise_for_status()
                        data3 = r3.json()
                        results3 = data3.get("results", data3) if isinstance(data3, dict) else data3
                        if results3:
                            sat_id = str(results3[0]["sat_id"])
        except httpx.HTTPError as exc:
            logger.warning(
                "SatNOGS UUID fetch: could not reach SatNOGS for NORAD %s / name %r: %s",
                norad,
                name,
                exc,
            )
            network_error = str(exc)
        except Exception:
            logger.exception("SatNOGS UUID fetch failed for NORAD %s / name %r", norad, name)

        if sat_id:
            with contextlib.suppress(Exception):
                self._conn.execute(
                    "UPDATE satellites SET satnogs_uuid = ? WHERE norad_cat_id = ?",
                    (sat_id, norad),
                )
                self._conn.commit()
            self._satnogs_open_url.emit(f"https://db.satnogs.org/satellite/{sat_id}")
        elif network_error:
            self._satnogs_network_error.emit(network_error)
        else:
            self._satnogs_not_found.emit()

    def _on_satnogs_network_error(self, error: str) -> None:
        QMessageBox.warning(
            self,
            "SatNOGS",
            _("Could not reach db.satnogs.org — check your network connection.\n\n") + error,
        )

    # Warn if the local clock drifts from true time by more than this many
    # seconds — FT4/Q65 rely on ~6-60s aligned periods and tolerate only a
    # fraction of a second of error before decoding starts failing.
    _NTP_DRIFT_WARN_THRESHOLD_S = 1.0

    def _check_ntp_sync_background(self) -> None:
        """Query an NTP server at startup and compensate for any clock drift.

        Runs in a background thread; never raises. Rather than requiring the
        OS clock itself to be corrected (which needs administrator privileges
        on Windows and would be a disruptive UAC prompt on every launch), the
        measured offset is applied via core.clock_offset so FT4/Q65 timing
        (see comms.ft4.scheduler / comms.ft4.rx_capture / comms.q65.scheduler)
        stays aligned regardless of how far off the OS clock is — no manual
        NTP sync step required. Emits _ntp_check_failed only when no server
        could be reached at all (offset unknown, so nothing to compensate),
        and _ntp_offset_applied (informational, non-modal) when a correction
        was actually applied.
        """
        if self._shutdown_flag.is_set():
            return
        result = check_system_clock()
        if self._shutdown_flag.is_set():
            return

        if not result.reachable:
            logger.warning("NTP clock check: no server reachable: %s", result.error)
            self._ntp_check_failed.emit(
                _(
                    "Could not verify system clock accuracy — none of the NTP "
                    "time servers could be reached. If FT4/Q65 decoding fails, "
                    "check your network connection and system clock settings."
                )
            )
            return

        offset = result.offset_s or 0.0
        set_clock_offset(offset)
        logger.info(
            "NTP clock check: offset=%.3fs via %s (applied to FT4/Q65 timing)",
            offset,
            result.server,
        )
        if abs(offset) > self._NTP_DRIFT_WARN_THRESHOLD_S:
            self._ntp_offset_applied.emit(
                _(
                    "System clock is off by {offset:.2f}s (checked against {server}) "
                    "— compensated automatically for FT4/Q65 timing."
                ).format(offset=offset, server=result.server)
            )

    def _on_ntp_check_failed(self, msg: str) -> None:
        """Show the startup NTP check warning dialog (called on UI thread)."""
        QMessageBox.warning(self, _("Clock Sync Check"), msg)

    def _on_ntp_offset_applied(self, msg: str) -> None:
        """Show a brief, non-modal status-bar note about the applied clock offset."""
        sb = self.statusBar()
        if sb:
            sb.showMessage(msg, 8000)

    def _toggle_favorite(self, norad: int, favorite: bool) -> None:
        """Save the favorite state to the DB and reload the satellite list (legacy)."""
        self._conn.execute(
            "UPDATE satellites SET is_favorite = ? WHERE norad_cat_id = ?",
            (1 if favorite else 0, norad),
        )
        self._conn.commit()
        self._load_satellites()

    def _set_favorite_group(self, norad: int, group_id: int) -> None:
        """Assign a satellite to a custom favorite group (0 = remove from all groups)."""
        self._conn.execute(
            "UPDATE satellites SET favorite_group = ?, is_favorite = ? WHERE norad_cat_id = ?",
            (group_id, 1 if group_id > 0 else 0, norad),
        )
        self._conn.commit()
        self._load_satellites()

    def _rebuild_filter_combo(self) -> None:
        """Rebuild the filter combo from DB custom_groups + fixed entries.

        Preserves the current selection when possible.
        """
        prev = self._filter_combo.currentText() if self._filter_combo.count() > 0 else ""
        self._filter_combo.blockSignals(True)
        self._filter_combo.clear()

        groups = self._conn.execute(
            "SELECT name FROM custom_groups ORDER BY sort_order, id"
        ).fetchall()

        items = ["All Satellites"]
        for grp in groups:
            items.append(f"★ {grp['name']}")
        items += [
            "Amateur",
            "CubeSat",
            "Weather",
            "Earth Observation",
            "Science",
            "Space Stations",
            "Operational (AMSAT)",
            "Celestial Bodies",
            "Hidden",
        ]
        self._filter_combo.addItems(items)

        idx = self._filter_combo.findText(prev)
        if idx >= 0:
            self._filter_combo.setCurrentIndex(idx)

        self._filter_combo.blockSignals(False)

    def _set_hidden(self, norad: int, hidden: bool) -> None:
        """Save the satellite hidden state to the DB and reload the satellite list."""
        self._conn.execute(
            "UPDATE satellites SET is_hidden = ? WHERE norad_cat_id = ?",
            (1 if hidden else 0, norad),
        )
        self._conn.commit()
        self._load_satellites()

    def _on_hide_satellite(self) -> None:
        """Satellite > Hide Satellite handler."""
        current = self._sat_list.currentItem()
        if current is None:
            QMessageBox.warning(self, _("Hide Satellite"), _("No satellite selected."))
            return
        norad = int(current.data(Qt.ItemDataRole.UserRole))
        name = current.text().lstrip("★ ").strip()
        answer = QMessageBox.question(
            self,
            _("Hide Satellite"),
            _("Hide {name} (NORAD {n}) from the satellite list?").format(name=name, n=norad),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._set_hidden(norad, True)

    def _show_sat_info(self, norad: int, name: str) -> None:
        """Display a satellite info dialog (NORAD number, TLE epoch, quality)."""
        tle_row = self._conn.execute(
            "SELECT epoch, quality_score, source, tle_group FROM tle_data WHERE norad_cat_id = ?",
            (norad,),
        ).fetchone()

        info_parts = [
            _("Name: {name}").format(name=name),
            _("NORAD: {norad}").format(norad=norad),
        ]
        if tle_row:
            epoch = str(tle_row["epoch"])[:16] if tle_row["epoch"] else _("N/A")
            info_parts += [
                _("TLE Epoch: {epoch} UTC").format(epoch=epoch),
                _("TLE Quality: {quality}").format(quality=tle_row["quality_score"]),
                _("Source: {source}").format(source=tle_row["source"]),
                _("Group: {group}").format(group=tle_row["tle_group"] or "amateur"),
            ]
        else:
            info_parts.append(_("TLE: Not available"))

        title = _("Satellite Info — {name}").format(name=name)
        QMessageBox.information(self, title, "\n".join(info_parts))

    def _on_sat_selected(self, row: int) -> None:
        """Callback invoked when the satellite list selection changes."""
        if row < 0:
            self._selected_norad = None
            self._current_transmitter = None
            self._detail_panel.clear()
            self._radio_control.clear_satellite()
            self._world_map.clear_footprint()
            self._world_map.clear_moon_position()
            # Mirror _on_transmitter_changed(None)'s SDR-side reset: without
            # this, SdrControlWidget still thinks a transponder is active
            # (its "T" button stays enabled) even though _current_transmitter
            # above was just cleared and _doppler_cycle() will no longer run
            # to back it up.
            self._sdr_control.set_transponder_active(False)
            self._sdr_tune_offset = 0.0
            self._sdr_control.reset_tune_offset()
            self._dial_feedback_offset_hz = 0.0
            return
        item = self._sat_list.item(row)
        if item is None:
            return
        norad = int(item.data(Qt.ItemDataRole.UserRole))
        self._selected_norad = norad
        name = item.text()
        self._detail_panel.set_satellite(norad, name)
        self._radio_control.set_satellite(norad, name)
        self._dashboard_view.set_satellite(norad, name)
        self._notify_telemetry_tab_satellite(norad, name)

        if norad == MOON_ID:
            self._world_map.clear_footprint()
            self._refresh_passes()
            self._refresh_radio_control(norad)
            return

        # Switching away from Moon: remove sub-lunar point marker
        self._world_map.clear_moon_position()
        self._refresh_passes()
        self._refresh_radio_control(norad)

    def _refresh_radio_control(self, norad: int) -> None:
        """Fetch the transmitter list for the selected satellite and update the Radio Control panel.

        Priority ORDER BY:
          1. Transponder with bidirectional links below 1 GHz
          2. Transceiver below 1 GHz
          3. Any entry below 1 GHz
          4. downlink_low ASC (lower frequency first)
        For MOON_ID, EME frequencies are loaded from eme_frequencies.json instead of the DB.
        """
        if norad == MOON_ID:
            transmitters = self._transmitter_manager.get_transmitters(MOON_ID)
            self._radio_control.set_transmitters(transmitters)
            return

        rows = self._conn.execute(
            """
            SELECT uuid, description, type,
                   downlink_low, uplink_low, mode, ctcss_tone, invert,
                   alive, satnogs_status, norad_cat_id, source
            FROM transmitters
            WHERE norad_cat_id = ?
            ORDER BY
                (CASE WHEN alive = 1 THEN 0
                      WHEN satnogs_status = 'invalid' THEN 2
                      ELSE 1 END) ASC,
                (CASE WHEN type='Transponder' AND uplink_low IS NOT NULL
                           AND downlink_low < 1000000000 THEN 1 ELSE 0 END) DESC,
                (CASE WHEN type='Transceiver'
                           AND downlink_low < 1000000000 THEN 1 ELSE 0 END) DESC,
                (CASE WHEN downlink_low < 1000000000 THEN 1 ELSE 0 END) DESC,
                downlink_low ASC
            """,
            (norad,),
        ).fetchall()
        transmitters = [dict(r) for r in rows]
        # set_transmitters emits transmitter_changed, which causes _on_transmitter_changed
        # to update _current_transmitter
        self._radio_control.set_transmitters(transmitters)

    def _on_transmitter_changed(self, xpdr: Any) -> None:
        """Update _current_transmitter and refresh the display on transponder selection change."""
        self._current_transmitter = xpdr if isinstance(xpdr, dict) else None
        self._current_ctcss_tone = None  # revert to transponder tone on selection change
        # Drop the previous transmitter's cached Doppler result so
        # _update_selected_satellite() doesn't briefly show stale
        # frequencies for the new selection until DopplerWorker's next cycle.
        self._latest_doppler = None
        self._dashboard_view.set_transmitter(self._current_transmitter)
        if self._current_transmitter:
            dl = self._current_transmitter.get("downlink_low")
            ul = self._current_transmitter.get("uplink_low")
            mode = self._current_transmitter.get("mode")
            satnogs_tone = self._current_transmitter.get("ctcss_tone")
            db_info = get_ctcss(self._selected_norad) if self._selected_norad else None
            # SatNOGS ctcss_tone takes priority; DB tone is the fallback.
            tone_hz: float | None = (
                float(satnogs_tone)
                if satnogs_tone
                else (db_info["tone_hz"] if db_info and db_info.get("tone_hz") else None)
            )
            activation_hz: float | None = (
                db_info["activation_hz"] if db_info and db_info.get("activation_hz") else None
            )
            self._ctcss_tone_hz = tone_hz
            self._ctcss_activation_hz = activation_hz
            self._radio_control.update_ctcss(tone_hz, activation_hz)
            self._radio_control.update_doppler(dl, dl, None, ul, ul, None, mode, tone_hz)
        else:
            self._ctcss_tone_hz = None
            self._ctcss_activation_hz = None
            self._radio_control.update_ctcss(None, None)
            self._radio_control.update_doppler(None, None, None, None, None, None)
        self._apply_transponder_state_to_rig()
        # Auto-select SDR demod mode from transponder; reset passband tune offset
        if self._current_transmitter:
            satnogs_mode = self._current_transmitter.get("mode") or ""
            self._sdr_control.set_transponder_mode(satnogs_mode)
        self._sdr_control.set_transponder_active(self._current_transmitter is not None)
        self._sdr_tune_offset = 0.0
        self._sdr_control.reset_tune_offset()
        # Lock dial feedback: reset for the new transponder -- a different
        # transponder means a different passband, so any accumulated
        # manual-retune offset from the previous one is no longer
        # meaningful.
        self._dial_feedback_offset_hz = 0.0

    def _on_rx_offset_changed(self, value: float) -> None:
        """Persist the Offset spinbox's new value for the current transponder
        (GitHub Issue #18). Updates the in-memory dict too so the next
        DopplerWorker cycle picks it up immediately, without waiting for a
        DB re-read."""
        if self._current_transmitter is None:
            return
        xpdr_uuid = self._current_transmitter.get("uuid")
        if not xpdr_uuid:
            return
        self._current_transmitter["rx_offset_hz"] = value
        self._transmitter_manager.update_transmitter(xpdr_uuid, rx_offset_hz=value)

    def _disconnect_rig(self) -> None:
        """Disconnect the rig and refresh the UI status."""
        if self._rig_controller is not None:
            is_sdr = getattr(self._rig_controller, "is_sdr", False)
            self._rig_controller.disconnect()
            if is_sdr:
                self._sdr_control.set_pipeline(None)
        self._radio_control.refresh_status()
        self._update_rig_label()

    def _apply_transponder_state_to_rig(self) -> None:
        """Apply mode + CTCSS to the rig when a transponder is selected.

        All rig types use ONE background thread so mode and CTCSS are always
        sent sequentially and never race each other.

        Satmode rigs (IC-9100 / IC-9700 etc.):
          Direct mode: CI-V handles everything in a single serial session.
            Disconnects first (UI thread) so the Hamlib handle is released
            before pyserial opens the same port.
          NET mode: send_mode_only (TCP) then _apply_ctcss_civ_direct
            (pyserial) in the same thread.

        Non-satmode rigs (FTX-1F, FT-991A etc.):
          FTX-1F: disconnects first (V commands cannot interleave with
            Doppler F/I), then send_mode_only → send_ctcss_cat sequentially.
          FT-991A: keeps the main connection alive (independent socket);
            send_mode_only → send_ctcss_cat sequentially.
        """
        from rig.controller import HamlibNetController

        rig = self._rig_controller
        if rig is None or self._current_transmitter is None:
            return

        mode = str(self._current_transmitter.get("mode") or "")
        if not mode:
            return
        invert = bool(self._current_transmitter.get("invert", False))
        dl_mode = mode
        ul_mode = _MODE_INVERT.get(mode, mode) if invert else mode
        ctcss_hz = float(self._ctcss_tone_hz or 0.0)

        # Update CW / DATA toggle button visibility/label in Radio Control tab.
        self._radio_control.update_cw_button(dl_mode, ul_mode)
        self._radio_control.update_dmode_button(dl_mode, ul_mode)

        # Notify NET rig of DL/UL frequencies (same-band detection) and
        # current mode (UL update throttle threshold).
        if isinstance(rig, HamlibNetController):
            dl_hz = float(self._current_transmitter.get("downlink_low") or 0)
            ul_hz = float(self._current_transmitter.get("uplink_low") or dl_hz)
            rig.set_transponder_freqs(dl_hz, ul_hz)
            rig.set_current_modes(dl_mode, ul_mode)

        if rig.is_satmode:
            # Satmode rigs: always disconnect so the user must reconnect
            # explicitly for the new satellite.
            if rig.is_connected:
                self._disconnect_rig()  # must run on UI thread

            # Pass transponder frequencies to Direct-mode satmode rigs so that
            # _apply_mode_and_ctcss_hamlib can write them first (Stage 1 freq anchor).
            if isinstance(rig, HamlibDirectController) and rig._satmode:
                dl_hz = float(self._current_transmitter.get("downlink_low") or 0)
                ul_hz = float(self._current_transmitter.get("uplink_low") or dl_hz)
                rig._transponder_dl_hz = dl_hz
                rig._transponder_ul_hz = ul_hz

            def _do_satmode() -> None:
                try:
                    rig.apply_transponder_state(dl_mode, ul_mode, ctcss_hz)
                except RigControlError as exc:
                    self._rig_error.emit(str(exc))
                except Exception as exc:
                    logger.error("RigDirect satmode: unexpected error in send thread: %s", exc)
                    self._rig_error.emit(str(exc))

            threading.Thread(target=_do_satmode, daemon=True).start()
        else:
            # Non-satmode rigs (FTX-1F, FT-991A):
            # FTX-1F Direct mode: raw CAT path — no Hamlib calls, no VFO
            #   switching, no disconnect needed.  apply_transponder_state()
            #   writes MD/CN/CT commands via os.open() and returns immediately.
            # FT-991A: independent socket; keep main connection alive.
            # Other non-satmode Direct rigs: disconnect first so V commands in
            #   send_mode_only cannot race with the Doppler F/I cycle.
            if isinstance(rig, HamlibDirectController) and rig._model_id in _RAW_CAT_MODEL_IDS:
                # FTX-1F / FT-991 Direct: raw CAT path, no disconnect required.
                # Use the same generation counter as the NET path so that rapid
                # transponder changes cancel the in-flight thread before it spends
                # time on serial port access.
                self._nonsatmode_gen += 1
                _gen = self._nonsatmode_gen
                _dl_hz = float((self._current_transmitter or {}).get("downlink_low") or 0)
                _ul_hz = float((self._current_transmitter or {}).get("uplink_low") or _dl_hz)
                _direct_rig = rig  # narrowed type for closure

                def _do_direct_cat(_gen: int = _gen) -> None:
                    try:
                        if self._nonsatmode_gen != _gen:
                            return
                        _direct_rig._send_freq_preset_direct(_dl_hz, _ul_hz)
                        if self._nonsatmode_gen != _gen:
                            return
                        _direct_rig.apply_transponder_state(dl_mode, ul_mode, ctcss_hz)
                    except RigControlError as exc:
                        self._rig_error.emit(str(exc))
                    except Exception as exc:
                        logger.error("RigDirect raw CAT: unexpected error in send thread: %s", exc)
                        self._rig_error.emit(str(exc))

                threading.Thread(target=_do_direct_cat, daemon=True).start()
            else:
                # Always disconnect so the Doppler loop is stopped before
                # freq preset and mode/CTCSS are written.  Mirrors satmode-rig
                # behaviour: the user must press Connect again after switching
                # transponders.  Avoids the "ガチャガチャ" (jitter) caused by
                # the Doppler loop and freq-preset independent socket interleaving
                # inside rigctld when the rig stays connected.
                #
                # Skip entirely for the SDR (rig.is_sdr): this whole
                # disconnect exists to avoid a VFO-command race between mode/
                # CTCSS setup and the Doppler F/I cycle, which does not apply
                # to SdrRigAdapter at all (set_mode/set_ctcss_tone/set_vfo are
                # all no-ops there; only set_frequency does anything, and
                # that is just a live SDR retune, not a CAT command
                # sequence). Without this guard, simply selecting a different
                # satellite/transponder — nothing to do with Autotrack — was
                # silently closing and requiring a manual reconnect of the
                # SDR every time (GitHub Issue #12 follow-up).
                if rig.is_connected and not getattr(rig, "is_sdr", False):
                    self._disconnect_rig()  # must run on UI thread

                # Increment generation so any in-flight thread can detect it is
                # superseded and exit before spending seconds on rigctld w commands.
                self._nonsatmode_gen += 1
                _gen = self._nonsatmode_gen
                cat_on = self._ctcss_cat_on
                cat_off = self._ctcss_cat_off

                def _do_nonsatmode(_gen: int = _gen) -> None:
                    if self._nonsatmode_gen != _gen:
                        return
                    # Rig is guaranteed disconnected here (disconnected above on
                    # the UI thread before this background thread was started).
                    # Write DL/UL frequencies and mode/CTCSS without racing the
                    # Doppler loop.
                    if isinstance(rig, HamlibNetController):
                        rig._send_split_init_independent()
                        rig._send_freq_preset_independent()
                    elif isinstance(rig, HamlibDirectController):
                        _dl_hz = float((self._current_transmitter or {}).get("downlink_low") or 0)
                        _ul_hz = float(
                            (self._current_transmitter or {}).get("uplink_low") or _dl_hz
                        )
                        rig._send_freq_preset_direct(_dl_hz, _ul_hz)
                    if self._nonsatmode_gen != _gen:
                        return
                    rig.send_mode_only(dl_mode, ul_mode)
                    if self._nonsatmode_gen != _gen:
                        return
                    # Dispatch CTCSS the same way _on_ctcss_send() does: CAT
                    # methods (ftx1/ft991/custom_cat) go through send_ctcss_cat,
                    # everything else uses standard Hamlib set_ctcss_tone().
                    # Generic Direct-mode rigs (e.g. IC-705) always use Hamlib
                    # regardless of ctcss_method — that setting is NET-mode-
                    # oriented and the Rig Settings dialog greys it out for
                    # Direct mode, but a stale value from a previous NET-mode
                    # configuration on the same slot could otherwise still be
                    # applied here.
                    if (
                        not _is_generic_direct_rig(rig)
                        and self._ctcss_method in self._CAT_CTCSS_METHODS
                    ):
                        rig.send_ctcss_cat(ctcss_hz, cat_on, cat_off)
                    else:
                        rig.set_ctcss_tone(ctcss_hz)

                threading.Thread(target=_do_nonsatmode, daemon=True).start()

    def _send_mode_only_to_rig(self) -> None:
        """Set mode on both VFOs via an independent connection on transponder change.

        Computes dl_mode / ul_mode from the current transponder, applying
        _MODE_INVERT when invert=True (e.g. RS-44 USB↔LSB).

        FT-991: keeps the main connection alive; send_mode_only() opens an
        independent socket for the mode commands, so no disconnect is needed.
        send_mode_only() is run in a background thread to avoid blocking the UI.

        FTX-1F / generic: disconnects first (on the UI thread, so status is
        updated immediately) so the Doppler cycle's F/I commands cannot race
        with the V commands inside send_mode_only().  send_mode_only() itself
        runs in a background thread.  The user must reconnect manually.

        Satmode NET rigs (IC-9700 etc.): S 1 Main resets VFO modes, so
        send_mode_only() must run AFTER connect() completes.  When the rig is
        already connected we can call it immediately; when it is not yet
        connected we skip here and let _on_rig_slot_connected() handle it.
        """
        from rig.controller import HamlibNetController

        if self._rig_controller is None or self._current_transmitter is None:
            return
        mode = str(self._current_transmitter.get("mode") or "")
        if not mode:
            return
        invert = bool(self._current_transmitter.get("invert", False))
        dl_mode = mode
        ul_mode = _MODE_INVERT.get(mode, mode) if invert else mode
        rig = self._rig_controller
        # Satmode NET rigs: if not yet connected, defer until _on_rig_slot_connected().
        if isinstance(rig, HamlibNetController) and rig.is_satmode and not rig.is_connected:
            return
        if rig.is_connected and self._ctcss_method != "ft991":
            self._disconnect_rig()  # UI update must happen on the UI thread
        logger.info(
            "CTCSS: tone=%s method=%s cat_on=%r",
            self._current_transmitter.get("ctcss_tone") if self._current_transmitter else None,
            self._ctcss_method,
            self._ctcss_cat_on,
        )

        def _do_send() -> None:
            rig.send_mode_only(dl_mode, ul_mode)

        threading.Thread(target=_do_send, daemon=True).start()

    # Methods that use custom CAT commands for CTCSS (not handled by Hamlib itself).
    _CAT_CTCSS_METHODS: frozenset[str] = frozenset({"custom_cat", "ftx1", "ft991"})

    def _send_ctcss_cat_to_rig(self, tone_hz: float | None = None) -> None:
        """Send CTCSS tone to the rig when a transponder is selected or the button is pressed.

        For custom CAT methods ("custom_cat", "ftx1", "ft991"), sends raw CAT
        commands directly to the serial port.  For the standard "hamlib" method,
        calls set_ctcss_tone() via the python-hamlib binding so the IC-9100 and
        other Hamlib-capable rigs are set automatically on transponder change.
        Runs in a background thread so the UI is not blocked.

        Args:
            tone_hz: Tone to send in Hz.  When None (automatic mode — called on
                     transponder change), reads ctcss_tone from _current_transmitter.
                     When explicitly provided (button press), the caller's value
                     takes precedence so the Activate button can force 74.4 Hz
                     regardless of what the transmitter record says.
        """
        if self._rig_controller is None:
            return
        if tone_hz is None:
            tone_hz = float(self._ctcss_tone_hz or 0.0)

        if self._ctcss_method in self._CAT_CTCSS_METHODS:
            # Send via direct serial port (direct_port mandatory for ftx1/ft991/icom_civ).
            # No connection guard: _send_cat_direct() works regardless of Doppler state
            # because _cmd_lock in _send_cat_direct() serialises against in-flight F/I
            # commands.  This mirrors the IC-9100 approach of setting CTCSS at
            # transponder-selection time so Connect() needs no re-send.
            rig = self._rig_controller
            cat_on = self._ctcss_cat_on
            cat_off = self._ctcss_cat_off

            def _send_cat() -> None:
                try:
                    rig.send_ctcss_cat(tone_hz, cat_on, cat_off)
                except Exception as exc:
                    self._rig_error.emit(f"send_ctcss_cat: {exc}")

            threading.Thread(target=_send_cat, daemon=True).start()

        elif self._ctcss_method == "hamlib":
            # Hamlib CTCSS: HamlibDirectController.set_ctcss_tone() handles both
            # the connected case (existing handle, under _rig_cmd_lock) and the
            # not-yet-connected case (temporary connection, then close).  This
            # way CTCSS is written to the rig at transponder-selection time, not
            # at Doppler-connect time, so no VFO switching occurs during the
            # Doppler cycle.  HamlibNetController requires an active connection;
            # for NET rigs the tone is sent here if already connected, and
            # re-sent in _on_rig_slot_connected if not.
            from rig.controller import HamlibDirectController

            if (
                not isinstance(self._rig_controller, HamlibDirectController)
                and not self._rig_controller.is_connected
            ):
                # NET / SDR: require active connection (Direct handles it via temp connection).
                return
            logger.info(
                "_send_ctcss_cat_to_rig hamlib: rig_type=%s tone_hz=%.1f",
                type(self._rig_controller).__name__,
                tone_hz,
            )
            rig = self._rig_controller

            def _send_hamlib() -> None:
                try:
                    ok = rig.set_ctcss_tone(tone_hz)
                    if not ok:
                        self._rig_error.emit(f"set_ctcss_tone({tone_hz} Hz): rig returned failure")
                except Exception as exc:
                    self._rig_error.emit(f"set_ctcss_tone: {exc}")

            threading.Thread(target=_send_hamlib, daemon=True).start()

    def _refresh_passes(self) -> None:
        """Fetch pass predictions for the selected satellite and update the pass list and chart."""
        if self._selected_norad is None:
            self._pass_list.clear()
            return

        now = datetime.now(UTC)
        item = self._sat_list.currentItem()
        name = item.text() if item else ""

        if self._selected_norad == MOON_ID:
            if not self._celestial_engine.is_loaded:
                self._pass_list.clear()
                return
            loc = self._location_manager.current if self._location_manager else None
            if loc is None:
                self._pass_list.clear()
                return
            passes = self._celestial_engine.moon_events(
                loc.latitude_deg,
                loc.longitude_deg,
                loc.elevation_m,
                now,
                now + timedelta(hours=24),
            )
            self._current_passes = passes
            self._pass_list.set_passes(passes)
            self._pass_chart.set_passes(passes, sat_name=name)
            return

        if self._pass_predictor is None:
            self._pass_list.clear()
            return
        passes = self._pass_predictor.get_passes(
            self._selected_norad,
            now,
            now + timedelta(hours=24),
        )
        # get_passes(start=now) misses a pass already in progress because its AOS
        # is in the past and Skyfield emits no retroactive AOS event.  Prepend the
        # ongoing pass when the satellite is currently above the horizon.
        current_pass = self._pass_predictor.get_current_pass(self._selected_norad, now)
        if current_pass is not None:
            passes = [current_pass, *passes]
        self._current_passes = passes
        self._pass_list.set_passes(passes)
        self._pass_chart.set_passes(passes, sat_name=name)

    def _on_chart_range_changed(self, hours: float) -> None:
        """Immediately call PassPredictor when the pass chart time range changes."""
        if self._selected_norad is None:
            return

        now = datetime.now(UTC)
        item = self._sat_list.currentItem()
        name = item.text() if item else ""

        if self._selected_norad == MOON_ID:
            if not self._celestial_engine.is_loaded:
                return
            loc = self._location_manager.current if self._location_manager else None
            if loc is None:
                return
            passes = self._celestial_engine.moon_events(
                loc.latitude_deg,
                loc.longitude_deg,
                loc.elevation_m,
                now,
                now + timedelta(hours=hours),
            )
            self._current_passes = passes
            self._pass_list.set_passes(passes)
            self._pass_chart.set_passes(passes, sat_name=name)
            return

        if self._pass_predictor is None:
            return
        passes = self._pass_predictor.get_passes(
            self._selected_norad,
            now,
            now + timedelta(hours=hours),
        )
        self._current_passes = passes
        self._pass_list.set_passes(passes)
        self._pass_chart.set_passes(passes, sat_name=name)

    def _on_target_search_requested(self, start: Any, end: Any) -> None:
        """Called when the Search button on the Target tab is pressed."""
        if self._selected_norad is None:
            return

        start_dt: datetime = start
        end_dt: datetime = end
        item = self._sat_list.currentItem()
        name = item.text() if item else ""

        if self._selected_norad == MOON_ID:
            if not self._celestial_engine.is_loaded:
                return
            loc = self._location_manager.current if self._location_manager else None
            if loc is None:
                return
            passes = self._celestial_engine.moon_events(
                loc.latitude_deg,
                loc.longitude_deg,
                loc.elevation_m,
                start_dt,
                end_dt,
            )
            self._current_passes = passes
            self._pass_list.set_passes(passes)
            self._pass_chart.set_passes(passes, sat_name=name)
            return

        if self._pass_predictor is None:
            return
        passes = self._pass_predictor.get_passes(self._selected_norad, start_dt, end_dt)
        self._current_passes = passes
        self._pass_list.set_passes(passes)
        self._pass_chart.set_passes(passes, sat_name=name)

    def _on_highlight_satellite(self, norad: int) -> None:
        """Highlight the satellite in the left list when its row is clicked on the Group tab."""
        for i in range(self._sat_list.count()):
            item = self._sat_list.item(i)
            if item is not None and int(item.data(Qt.ItemDataRole.UserRole)) == norad:
                self._sat_list.setCurrentRow(i)
                break

    def _on_group_results_ready(self, results: object) -> None:
        """Populate the Group Pass Chart tab and make it visible on first group search."""
        self._group_pass_chart.set_results(results)  # type: ignore[arg-type]
        self._tab_widget.setTabVisible(self._group_chart_tab_idx, True)
        # Cache for AOS/LOS notification checks
        self._group_pass_results = results  # type: ignore[assignment]
        # Pass search is now done — autotrack may proceed
        self._autotrack.mark_searches_ready()

    # ------------------------------------------------------------------ #
    # Menu handlers
    # ------------------------------------------------------------------ #

    def _on_set_qth(self) -> None:
        """File > Set QTH... handler."""
        if self._location_manager is None:
            QMessageBox.warning(self, _("Set QTH"), _("Location manager not initialized."))
            return
        from ui.qth_dialog import QTHDialog

        dialog = QTHDialog(self._location_manager, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            loc = self._location_manager.current
            if loc is None:
                return
            if self._engine is not None:
                self._engine.update_observer(loc.latitude_deg, loc.longitude_deg, loc.elevation_m)
            else:
                self._engine = SatelliteEngine(
                    self._tle_manager, loc.latitude_deg, loc.longitude_deg, loc.elevation_m
                )
            if self._pass_predictor is not None:
                self._pass_predictor.update_observer(
                    loc.latitude_deg, loc.longitude_deg, loc.elevation_m
                )
            else:
                self._pass_predictor = PassPredictor(
                    self._tle_manager, loc.latitude_deg, loc.longitude_deg, loc.elevation_m
                )
            self._pass_list.set_pass_predictor(self._pass_predictor)
            self._update_statusbar()

    def _on_settings(self) -> None:
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self._conn, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._on_settings_accepted()

    def _apply_world_map(self) -> None:
        """Apply the world map image selected in Settings to the WorldMapView.

        On first launch (no explicit selection), automatically download the
        NASA Topographic 1024px map so users see a nice map out of the box.
        """
        from ui.settings_dialog import SettingsDialog, _maps_dir

        # Auto-download the default map on first launch if not yet present.
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'world_map_file'"
        ).fetchone()
        if not (row and row["value"]):
            default_path = _maps_dir() / "nasa-topo_1024.jpg"
            if not default_path.exists():
                import threading

                def _download() -> None:
                    try:
                        import httpx

                        url = (
                            "https://raw.githubusercontent.com/csete/gpredict/"
                            "master/pixmaps/maps/nasa-topo_1024.jpg"
                        )
                        with httpx.Client(follow_redirects=True, timeout=30) as client:
                            resp = client.get(url)
                            if resp.status_code == 200:
                                default_path.write_bytes(resp.content)
                                # Emit signal so the Qt UI thread re-applies the map.
                                self._map_downloaded.emit()
                    except Exception:
                        pass

                threading.Thread(target=_download, daemon=True).start()

        path = SettingsDialog.get_world_map_path(self._conn)
        self._world_map.set_map_image(path)
        self._dashboard_view.set_map_image(path)

    def _apply_time_zone(self) -> None:
        """Load the saved time zone preference and apply it to all time-display widgets."""
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone_mode'"
        ).fetchone()
        use_utc = (row["value"] if row and row["value"] else "utc") != "local"

        # Sync menu checkmarks
        if hasattr(self, "_tz_utc_action"):
            self._tz_utc_action.setChecked(use_utc)
            self._tz_local_action.setChecked(not use_utc)

        self._pass_list.set_use_utc(use_utc)
        self._pass_chart.set_use_utc(use_utc)
        self._group_pass_chart.set_use_utc(use_utc)
        self._radar_view.set_use_utc(use_utc)
        self._dashboard_view._radar.set_use_utc(use_utc)
        self._at_dialog.set_use_utc(use_utc)
        self._notify_comms_tabs_use_utc(use_utc)

    def _on_time_zone_changed(self, use_utc: bool) -> None:
        """Persist the time zone preference and propagate to all display widgets."""
        value = "utc" if use_utc else "local"
        self._conn.execute(
            """
            INSERT OR REPLACE INTO app_settings (key, value, updated_at)
            VALUES ('time_zone_mode', ?, CURRENT_TIMESTAMP)
            """,
            (value,),
        )
        self._conn.commit()
        self._pass_list.set_use_utc(use_utc)
        self._pass_chart.set_use_utc(use_utc)
        self._group_pass_chart.set_use_utc(use_utc)
        self._radar_view.set_use_utc(use_utc)
        self._dashboard_view._radar.set_use_utc(use_utc)
        self._at_dialog.set_use_utc(use_utc)
        self._notify_comms_tabs_use_utc(use_utc)

    def _open_url_app_mode(self, url: str) -> None:
        """Open *url* in Chrome/Chromium app mode (no browser chrome/tabs).

        Searches for Chrome-family browsers in order of preference and launches
        with the ``--app=URL`` flag.  Falls back to ``QDesktopServices.openUrl``
        when no compatible browser is found.

        Args:
            url: the URL to open (may be passed as a raw string from QLabel.linkActivated)
        """
        _LINUX_CANDIDATES = [
            "google-chrome",
            "google-chrome-stable",
            "chromium-browser",
            "chromium",
            "brave-browser",
            "microsoft-edge",
        ]
        _MAC_CANDIDATES = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        _WIN_CANDIDATES = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(
                __import__("pathlib").Path.home()
                / "AppData"
                / "Local"
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe"
            ),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]

        browser_exe: str | None = None

        if sys.platform.startswith("linux"):
            for candidate in _LINUX_CANDIDATES:
                found = shutil.which(candidate)
                if found:
                    browser_exe = found
                    break
        elif sys.platform == "darwin":
            for candidate in _MAC_CANDIDATES:
                if shutil.which(candidate) or __import__("os.path").path.isfile(candidate):
                    browser_exe = candidate
                    break
        elif sys.platform == "win32":
            import os

            for candidate in _WIN_CANDIDATES:
                if os.path.isfile(candidate):
                    browser_exe = candidate
                    break

        if browser_exe:
            try:
                subprocess.Popen([browser_exe, f"--app={url}"])
                return
            except OSError:
                pass

        # Fallback: open with the system default browser
        QDesktopServices.openUrl(QUrl(url))

    def _on_settings_accepted(self) -> None:
        """After Settings OK, sync the enabled TLE sources and redraw the satellite list."""
        # Rebuild filter combo so any group additions/renames/removals are reflected
        self._rebuild_filter_combo()

        # Reload notification settings (warn_minutes / los_enabled etc. may have changed)
        self._notifier.reload_settings()

        # Reload autotrack lists (user may have added/removed lists in Settings)
        self._reload_autotrack_lists()

        self._apply_world_map()

        self._fetch_all_tle_sources()

    def _fetch_all_tle_sources(self) -> None:
        """Fetch every enabled TLE source once from a background thread, then refresh
        the satellite list.

        Shared by Settings > OK (_on_settings_accepted) and Satellite > Update TLE
        (_on_update_tle) so both entry points perform the same real fetch — Update TLE
        used to be a no-op stub that only displayed a "queued" message without ever
        actually fetching anything.

        The enabled sources here are CelesTrak's curated GROUP lists (amateur,
        cubesat, weather, ...). A satellite CelesTrak's own curation misses
        entirely — e.g. ORIGAMISAT-2 / NORAD 68795, confirmed absent from
        every one of these groups — can never be resolved by this loop no
        matter how many times it runs. Only fetch_active_tles()'s Phase 2
        per-satellite fallback covers that gap, so it's always run here too
        (regardless of its own 24h is_active_tle_stale() gate, since pressing
        Update TLE is an explicit request for a fresh attempt right now, not
        the passive background schedule). Without this, a user could press
        Update TLE indefinitely and the satellite would never update — which
        is exactly what happened (2026-08-10 report).

        fetch_provisional_tles() (NORAD >= 90000) had the exact same gap —
        never called from here at all, so pressing Update TLE could never
        refresh a provisional satellite no matter how many times it was
        pressed (2026-08-11 audit, prompted by a report about the startup
        path's own missing staleness gate). Added alongside
        fetch_active_tles() for the same reason, also bypassing its own
        staleness gate for the same "explicit request now" rationale.

        Guarded by _tle_fetch_in_progress against a second overlapping call
        while one is still running -- see that flag's docstring. Also emits
        an immediate status message before the (feedback-free) 6-group bulk
        loop starts, so the status bar doesn't just sit blank for however
        long that loop takes (each group can take 15-30s+ to time out on a
        bad connection, with nothing shown until it does) -- previously the
        only feedback at all was the one-time "queued" QMessageBox from
        _on_update_tle(), which doesn't reflect anything past that instant.
        """
        if self._tle_fetch_in_progress:
            logger.info("Update TLE requested while a fetch was already running — ignored")
            sb = self.statusBar()
            if sb:
                sb.showMessage(_("TLE update already in progress…"), 5000)
            return

        from ui.settings_dialog import SettingsDialog

        enabled = SettingsDialog.get_enabled_sources(self._conn)

        def _active_progress(msg: str) -> None:
            self._sync_progress.emit(f"🛰 {msg}")

        def _prov_progress(done: int, total: int) -> None:
            self._sync_progress.emit(f"🛰 Fetching provisional TLEs... ({done}/{total})")

        def _fetch_all() -> None:
            self._tle_fetch_in_progress = True
            try:
                updating_msg = _("Updating TLEs…")
                self._sync_progress.emit(f"🛰 {updating_msg}")

                # 2026-08-11 全面刷新: 最初に一度だけ両ホストへ接続確認を行い、
                # どちらか一方でも繋がらなければ直ちにエラー表示して中止する
                # （_refresh_satellite_names_sync() と同じ設計。理由はそちらの
                # コメント参照）。
                celestrak_ok = asyncio.run(self._tle_manager.is_celestrak_bulk_group_reachable())
                satnogs_ok = asyncio.run(self._tle_manager.is_satnogs_reachable())
                if not celestrak_ok or not satnogs_ok:
                    down = [
                        name
                        for name, ok in (("CelesTrak", celestrak_ok), ("SATNOGS", satnogs_ok))
                        if not ok
                    ]
                    msg = _("Cannot connect to {hosts}").format(hosts="/".join(down))
                    logger.warning("Manual TLE update aborted — unreachable: %s", down)
                    self._sync_progress.emit(f"❌ {msg}")
                    return

                for source_name in enabled:
                    logger.info("Manual TLE fetch: %s...", source_name)
                    try:
                        result = asyncio.run(self._tle_manager.fetch_and_update(source_name))
                        logger.info("Manual TLE fetch result (%s): %s", source_name, result)
                    except Exception as exc:
                        logger.warning(
                            "Manual TLE fetch error (%s): %s: %s",
                            source_name,
                            type(exc).__name__,
                            exc,
                        )

                blocked = False
                active: dict[str, int] = {}
                try:
                    active = asyncio.run(
                        self._tle_manager.fetch_active_tles(progress_callback=_active_progress)
                    )
                    logger.info("Manual active TLE fetch result: %s", active)
                    blocked = bool(active.get("celestrak_blocked") or active.get("satnogs_blocked"))
                except Exception as exc:
                    logger.warning("Manual active TLE fetch error: %s: %s", type(exc).__name__, exc)

                try:
                    prov = asyncio.run(
                        self._tle_manager.fetch_provisional_tles(progress_callback=_prov_progress)
                    )
                    logger.info("Manual provisional TLE fetch result: %s", prov)
                except Exception as exc:
                    logger.warning(
                        "Manual provisional TLE fetch error: %s: %s", type(exc).__name__, exc
                    )

                # Signal emit is thread-safe; Qt automatically queues it to the main thread.
                self._satellite_list_refresh.emit()
                # _schedule_active_tle_retry_if_blocked() (and the "blocked,
                # retry in Nh" message it shows) must run *after* the
                # provisional-TLE fetch above, not right after
                # fetch_active_tles() -- fetch_provisional_tles()'s own
                # progress_callback overwrites the status bar with its own
                # "Fetching provisional TLEs... (N/N)" messages, which would
                # otherwise silently clobber that message and then never get
                # cleared, since the final blank-out below is skipped
                # whenever `blocked` is True (2026-08-11: reported as the
                # status bar getting stuck forever on "Fetching provisional
                # TLEs..." even though every fetch had already finished).
                if active:
                    self._schedule_active_tle_retry_if_blocked(active)
                if not blocked:
                    self._sync_progress.emit("")
            finally:
                self._tle_fetch_in_progress = False

        threading.Thread(target=_fetch_all, daemon=True).start()

    def _on_add_transmitter(self) -> None:
        """Satellite > Add Transmitter... handler."""
        from ui.transmitter_dialog import TransmitterDialog

        norad = self._selected_norad
        dialog = TransmitterDialog(self._transmitter_manager, norad_cat_id=norad, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            QMessageBox.information(
                self,
                _("Add Transmitter"),
                _("Transmitter added successfully."),
            )

    def _on_edit_transmitter(self) -> None:
        """Satellite > Edit Transmitter... handler."""
        from ui.transmitter_dialog import TransmitterDialog

        current = self._sat_list.currentItem()
        if current is None:
            QMessageBox.warning(self, _("Edit Transmitter"), _("No satellite selected."))
            return
        norad = int(current.data(Qt.ItemDataRole.UserRole))

        rows = self._conn.execute(
            "SELECT * FROM transmitters WHERE norad_cat_id = ? AND alive = 1 ORDER BY description",
            (norad,),
        ).fetchall()
        if not rows:
            QMessageBox.information(
                self, _("Edit Transmitter"), _("No transmitters found for this satellite.")
            )
            return

        items = [
            f"{dict(r)['description']}  [{(dict(r).get('downlink_low') or 0) / 1e6:.3f} MHz]"
            for r in rows
        ]
        from PySide6.QtWidgets import QInputDialog

        item, ok = QInputDialog.getItem(
            self, _("Edit Transmitter"), _("Select transmitter to edit:"), items, 0, False
        )
        if not ok:
            return

        idx = items.index(item)
        existing = dict(rows[idx])
        dialog = TransmitterDialog(self._transmitter_manager, existing=existing, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted and self._selected_norad is not None:
            self._refresh_radio_control(self._selected_norad)

    def _on_delete_transmitter(self) -> None:
        """Satellite > Delete Transmitter... handler."""
        current = self._sat_list.currentItem()
        if current is None:
            QMessageBox.warning(self, _("Delete Transmitter"), _("No satellite selected."))
            return
        norad = int(current.data(Qt.ItemDataRole.UserRole))

        rows = self._conn.execute(
            "SELECT * FROM transmitters WHERE norad_cat_id = ? ORDER BY description",
            (norad,),
        ).fetchall()
        if not rows:
            QMessageBox.information(
                self, _("Delete Transmitter"), _("No transmitters found for this satellite.")
            )
            return

        items = [
            f"{dict(r)['description']}  [{(dict(r).get('downlink_low') or 0) / 1e6:.3f} MHz]"
            for r in rows
        ]
        from PySide6.QtWidgets import QInputDialog

        item, ok = QInputDialog.getItem(
            self, _("Delete Transmitter"), _("Select transmitter to delete:"), items, 0, False
        )
        if not ok:
            return

        idx = items.index(item)
        rec = dict(rows[idx])
        answer = QMessageBox.question(
            self,
            _("Delete Transmitter"),
            _("Delete transmitter '{desc}'?").format(desc=rec["description"]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._transmitter_manager.delete_transmitter(rec["uuid"])
        if self._selected_norad is not None:
            self._refresh_radio_control(self._selected_norad)

    def _on_add_manual_tle(self) -> None:
        """Satellite > Add Manual TLE... handler."""
        from ui.manual_tle_dialog import ManualTLEDialog

        dialog = ManualTLEDialog(self._tle_manager, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.added_norad is not None:
            self._load_satellites()
            QMessageBox.information(
                self,
                _("Add Manual TLE"),
                _("Satellite TLE added successfully (NORAD {n}).").format(n=dialog.added_norad),
            )

    def _on_update_tle(self) -> None:
        """Satellite > Update TLE handler.

        Used to be a no-op stub that only displayed this message without ever
        actually fetching anything (found while investigating a satellite missing
        from the list on Windows, 2026-07-23). Now shares the same real fetch
        used by Settings > OK.
        """
        self._fetch_all_tle_sources()
        QMessageBox.information(
            self, _("Update TLE"), _("TLE update has been queued in the background.")
        )

    def _on_sync_satnogs(self) -> None:
        """Data > Sync Frequencies from SATNOGS handler.

        Runs sync_from_satnogs() in a background thread and displays
        the result count in the status bar on completion.
        """
        threading.Thread(target=self._refresh_satnogs_sync, daemon=True).start()
        sb = self.statusBar()
        if sb:
            sb.showMessage(_("Syncing transmitter frequencies from SATNOGS..."), 5000)

    def _schedule_active_tle_retry_if_blocked(self, result: dict[str, int]) -> None:
        """Queue a one-shot retry if fetch_active_tles() had to cut a Phase 2
        fallback short to avoid tripping CelesTrak's/SATNOGS's own abuse
        protection (TLEManager._ErrorCountBreaker — see its docstring for
        the incident this addresses: pressing Update TLE could previously
        leave satellites like ORIGAMISAT-2 unresolved indefinitely if every
        run got itself blocked partway through).

        3 hours is comfortably longer than CelesTrak's documented 2-hour
        error-count window (https://celestrak.org/usage-policy.php), so the
        retry should never run into a still-active block from *this* run.

        Two independent mechanisms, because either one alone has a gap:
          - An in-memory APScheduler one-shot job (fixed id,
            replace_existing=True, so repeated trips just push the one
            pending retry further out) — fires on its own if the app stays
            running, without waiting for a restart. But it doesn't survive
            the app being closed: closeEvent() shuts the scheduler down
            with whatever's still pending simply discarded.
          - A retry-due time persisted via TLEManager.set_active_tle_retry_after()
            — survives a restart, and is checked at startup by
            _refresh_satellite_names_sync() (is_active_tle_retry_due(),
            alongside the normal 24h is_active_tle_stale() gate) — needed
            because fetch_active_tles() always logs a 'success' sync_log
            entry even when a phase's breaker tripped, so
            is_active_tle_stale() alone would treat a blocked run as
            "fresh for 24h" and never retry sooner (confirmed gap,
            2026-08-11: closing the app before the in-memory retry fires
            and reopening it later, but still within 24h, silently skipped
            re-fetching).

        On a clean (not blocked) result, clears any persisted marker from
        an earlier blocked run so a stale retry time doesn't linger.
        """
        blocked = bool(result.get("celestrak_blocked") or result.get("satnogs_blocked"))
        if not blocked:
            self._tle_manager.set_active_tle_retry_after(None)
            return

        run_at = datetime.now(UTC) + timedelta(hours=3)
        self._tle_manager.set_active_tle_retry_after(run_at)
        if self._scheduler is not None:
            self._scheduler.add_job(
                self._refresh_active_tle_sync,
                "date",
                run_date=run_at,
                id="active_tle_retry",
                replace_existing=True,
                misfire_grace_time=1800,
            )

        # Tells the user *why* this run is pausing, instead of the status bar
        # just going blank or getting stuck on a mid-fetch "CelesTrak: N
        # satellite(s)..." message with no indication that anything is wrong
        # or when it'll pick back up (the exact "is this just taking time?"
        # confusion reported 2026-08-10).
        #
        # Deliberately does NOT frame this as "Fetched X/Y" using
        # phase2_total/phase2_unresolved (as an earlier version did): those
        # numbers describe how many satellites Phase 2's SATNOGS bulk dump
        # resolved, which is unrelated to *why* this run is blocked (Phase
        # 1's single GROUP=active request getting a 403/timeout). Wording it
        # as "Fetched X/Y" made it look like Phase 2 itself got cut short,
        # when in the common case it had already finished cleanly via one
        # bulk request that has nothing to do with CelesTrak being blocked
        # (2026-08-11 report). The unresolved count is still worth surfacing
        # when nonzero, just not as the headline reason.
        which_blocked = [
            name
            for name, flag in (
                ("CelesTrak", result.get("celestrak_blocked")),
                ("SATNOGS", result.get("satnogs_blocked")),
            )
            if flag
        ]
        provider = "/".join(which_blocked) if which_blocked else _("a provider")
        unresolved = result.get("phase2_unresolved", 0)
        if unresolved:
            status_template = _(
                "{provider} blocked — retry in {hours}h"
                " ({unresolved} satellite(s) still unresolved)"
            )
            msg = status_template.format(provider=provider, hours=3, unresolved=unresolved)
        else:
            status_template = _("{provider} blocked — retry in {hours}h")
            msg = status_template.format(provider=provider, hours=3)
        self._sync_progress.emit(f"⚠ {msg}")

        logger.info(
            "Active TLE fetch was rate-limited by a provider; retry scheduled for %s",
            run_at.isoformat(),
        )

    def _refresh_active_tle_sync(self) -> None:
        """APScheduler job (interval hours=24, and also the one-shot retry
        scheduled by _schedule_active_tle_retry_if_blocked()): refresh
        regular (NORAD 10000-89999) satellite TLEs via
        TLEManager.fetch_active_tles().

        The startup path (_refresh_satellite_names_sync()) calls fetch_active_tles()
        directly instead of through this wrapper, since it also wires up a progress
        callback for the status bar — this method remains only as the periodic
        APScheduler job's entry point, where no visible progress reporting is needed.
        """
        try:
            result = asyncio.run(self._tle_manager.fetch_active_tles())
            logger.info("Active TLE fetch completed: %s", result)
            self._satellite_list_refresh.emit()
            self._schedule_active_tle_retry_if_blocked(result)
        except Exception as exc:
            logger.warning("Active TLE fetch failed: %s", exc)

    def _refresh_provisional_tle_sync(self) -> None:
        """Fetch TLEs for provisional (NORAD >= 90000) satellites from a background thread."""
        try:
            result = asyncio.run(self._tle_manager.fetch_provisional_tles())
            logger.info("Provisional TLE refresh completed: %s", result)
            self._satellite_list_refresh.emit()
        except Exception as exc:
            logger.warning("Provisional TLE refresh failed: %s", exc)

    def _refresh_meteor_tle_sync(self) -> None:
        """Ensure METEOR/HRPT satellites (e.g. NOAA 18/19) have a TLE from a background thread."""
        try:
            result = asyncio.run(self._tle_manager.fetch_meteor_tles())
            if result.get("found", 0) > 0:
                logger.info("METEOR satellite TLE refresh completed: %s", result)
                self._satellite_list_refresh.emit()
        except Exception as exc:
            logger.warning("METEOR satellite TLE refresh failed: %s", exc)

    def _refresh_satnogs_sync(self) -> None:
        """Sync SATNOGS transponders from a background thread.

        Checks reachability first (2026-08-11) instead of letting
        sync_from_satnogs() discover it via its own ConnectTimeout -- the
        old message ("SATNOGS sync failed: ConnectTimeout: ") was
        technically accurate but not the clear "cannot connect" wording
        used everywhere else this same fix was applied today.
        """
        if not asyncio.run(self._tle_manager.is_satnogs_reachable()):
            cannot_connect_satnogs_msg = _("Cannot connect to SATNOGS")
            msg = f"❌ {cannot_connect_satnogs_msg}"
            logger.warning("SATNOGS transmitter sync aborted — SATNOGS unreachable")
            self._satnogs_status.emit(msg)
            return
        try:
            result = asyncio.run(self._transmitter_manager.sync_from_satnogs())
            msg = _("SATNOGS sync: {ins} inserted, {upd} updated, {skp} skipped").format(
                ins=result["inserted"],
                upd=result["updated"],
                skp=result["skipped"],
            )
            logger.info("SATNOGS sync completed: %s", result)
        except Exception as exc:  # noqa: BLE001
            msg = _("SATNOGS sync failed: {etype}: {err}").format(etype=type(exc).__name__, err=exc)
            logger.warning("SATNOGS sync failed: %s: %s", type(exc).__name__, exc)
        self._satnogs_status.emit(msg)

    def _on_satnogs_status(self, msg: str) -> None:
        """Show the SATNOGS sync thread status in the status bar (called on UI thread)."""
        sb = self.statusBar()
        if sb:
            sb.showMessage(msg, 8000)

    def _on_rig_settings(self) -> None:
        from ui.rig_dialog import RigSettingsDialog

        dialog = RigSettingsDialog(self._conn, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._load_rig_settings()

    def _load_rig_settings(self) -> None:
        """Load Rig 1 and Rig 2 settings from the DB and instantiate controllers.

        Key priority for Rig 1:
          1. 'rig1_settings' (new key written by the tabbed dialog)
          2. 'rig_settings'  (legacy key — backward compatibility)
        Rig 2 is loaded only when its 'enabled' flag is True.
        If an SDR device is assigned to a slot in 'sdr_settings', that slot
        gets a SdrRigAdapter instead of a Hamlib controller.

        _build_rig_controller() / _build_sdr_rig_adapter() always return a
        fresh, unconnected instance, so accepting this dialog silently drops
        an existing connection unless we explicitly reconnect it below.
        """
        # Load SDR settings once so both rig slots can check assigned_rig
        sdr_cfg: dict[str, Any] = {}
        try:
            sdr_row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'sdr_settings'"
            ).fetchone()
            if sdr_row and sdr_row["value"]:
                sdr_cfg = json.loads(sdr_row["value"])
        except Exception as exc:
            logger.warning("Failed to load SDR settings: %s", exc)

        # ---------- Rig 1 ----------
        was_rig1_connected = self._rig_controller is not None and self._rig_controller.is_connected
        try:
            # If SDR is assigned to slot 1, build an SdrRigAdapter
            if sdr_cfg.get("assigned_rig") == 1 and sdr_cfg.get("enabled", False):
                self._rig_controller = self._build_sdr_rig_adapter(sdr_cfg)
                logger.info("Rig1: SDR assigned — %s", sdr_cfg.get("device_label", ""))
                self._radio_control.set_rig(self._rig_controller)
            else:
                row = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'rig1_settings'"
                ).fetchone()
                if row is None:
                    # Fallback: legacy key written by the old single-rig dialog
                    row = self._conn.execute(
                        "SELECT value FROM app_settings WHERE key = 'rig_settings'"
                    ).fetchone()
                if row is not None:
                    settings: dict[str, Any] = json.loads(row["value"])
                    self._rig_controller = self._build_rig_controller(settings)
                    self._ctcss_method = str(settings.get("ctcss_method", "hamlib"))
                    # Migrate legacy "icom_civ" value — Icom satmode rigs now use
                    # Hamlib standard CTCSS (icom_civ dropdown was removed).
                    if self._ctcss_method == "icom_civ":
                        self._ctcss_method = "hamlib"
                    # For preset methods, always use the current authoritative template from
                    # CTCSS_PRESET_TEMPLATES rather than the DB value, which may be stale.
                    if self._ctcss_method in CTCSS_PRESET_TEMPLATES:
                        self._ctcss_cat_on, self._ctcss_cat_off = CTCSS_PRESET_TEMPLATES[
                            self._ctcss_method
                        ]
                    else:
                        self._ctcss_cat_on = str(settings.get("ctcss_cat_on", ""))
                        self._ctcss_cat_off = str(settings.get("ctcss_cat_off", ""))
                    logger.info(
                        "Rig1Settings: method=%s cat_on=%r",
                        self._ctcss_method,
                        self._ctcss_cat_on,
                    )
                    self._radio_control.set_rig(self._rig_controller)
        except Exception as exc:
            logger.warning("Failed to load Rig 1 settings: %s", exc)

        if (
            was_rig1_connected
            and self._rig_controller is not None
            and not self._rig_controller.is_connected
        ):
            logger.info("Rig1: was connected before settings reload — reconnecting")
            self._radio_control._on_connect_rig1()

        # ---------- Rig 2 ----------
        was_rig2_connected = (
            self._rig2_controller is not None and self._rig2_controller.is_connected
        )
        try:
            # If SDR is assigned to slot 2, build an SdrRigAdapter
            if sdr_cfg.get("assigned_rig") == 2 and sdr_cfg.get("enabled", False):
                self._rig2_controller = self._build_sdr_rig_adapter(sdr_cfg)
                logger.info("Rig2: SDR assigned — %s", sdr_cfg.get("device_label", ""))
                self._radio_control.set_rig2(self._rig2_controller)
            else:
                row2 = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'rig2_settings'"
                ).fetchone()
                if row2 is not None:
                    s2: dict[str, Any] = json.loads(row2["value"])
                    if s2.get("enabled", False):
                        self._rig2_controller = self._build_rig_controller(s2)
                        logger.info("Rig2Settings: loaded, radio_type=%s", s2.get("radio_type"))
                    else:
                        self._rig2_controller = None
                    self._radio_control.set_rig2(self._rig2_controller)
        except Exception as exc:
            logger.warning("Failed to load Rig 2 settings: %s", exc)

        if (
            was_rig2_connected
            and self._rig2_controller is not None
            and not self._rig2_controller.is_connected
        ):
            logger.info("Rig2: was connected before settings reload — reconnecting")
            self._radio_control._on_connect_rig2()

        self._update_rig_label()

    def _start_satmode_warmup(self) -> None:
        """Run satmode_warmup() in the background for any Direct-mode satmode rig.

        Called once at startup, right after _load_rig_settings().  The warmup
        sends SATMODE ON to the IC-9100/IC-9700 so the dual-band hardware is
        fully initialized before the user presses Connect.
        """
        for rig in (self._rig_controller, self._rig2_controller):
            if isinstance(rig, HamlibDirectController) and rig.is_satmode:
                threading.Thread(
                    target=rig.satmode_warmup, daemon=True, name="satmode-warmup"
                ).start()
                logger.info("Startup: satmode warmup thread started for %s", rig)

    def _build_rig_controller(self, settings: dict[str, Any]) -> RigController:
        """Instantiate a RigController from a settings dictionary.

        Args:
            settings: dict with keys 'mode', 'host', 'net_port', 'model_id', 'port',
                      'baud_rate', 'radio_type', 'ctcss_method'.

        Returns:
            Configured (but not yet connected) RigController instance.
        """
        mode = settings.get("mode", "net")
        radio_type = str(settings.get("radio_type", "full_duplex"))
        if mode == "net":
            return HamlibNetController(
                host=str(settings.get("host", "localhost")),
                port=int(settings.get("net_port", 4532)),
                radio_type=radio_type,
                ctcss_method=str(settings.get("ctcss_method", "hamlib")),
                is_satmode_rig=bool(settings.get("icom_satmode_rig", False)),
            )
        return HamlibDirectController(
            model_id=int(settings.get("model_id", 1)),
            port=str(settings.get("port", "/dev/ttyUSB0")),
            baud_rate=int(settings.get("baud_rate", 9600)),
            civ_addr=str(settings.get("civ_addr", "")),
        )

    def _build_sdr_rig_adapter(self, sdr_cfg: dict[str, Any]) -> RigController:
        """Build a SdrRigAdapter from the sdr_settings dict.

        The adapter is returned unconfigured (connect() has not been called).
        device_info and audio settings are stored so that connect() opens the
        correct SoapySDR device with the right sample rate / gain.

        Args:
            sdr_cfg: dict as written by _SdrPanel.collect() in rig_dialog.py.

        Returns:
            A SdrRigAdapter with device_info set.
        """
        from rig.controller import SdrRigAdapter
        from sdr.device import SdrDeviceInfo

        adapter = SdrRigAdapter()

        device_args: dict[str, str] = {}
        raw = sdr_cfg.get("device_args")
        if isinstance(raw, dict):
            device_args = {str(k): str(v) for k, v in raw.items()}

        # SdrDeviceInfo stores identity (args / label); audio params are set
        # on the SdrDevice after open() via adapter.set_audio_params().
        info = SdrDeviceInfo(
            driver=device_args.get("driver"),
            label=str(sdr_cfg.get("device_label", "SDR")),
            serial=device_args.get("serial", ""),
            hardware=device_args.get("hardware", ""),
            args=device_args,
        )
        adapter.set_device_info(info)

        # Store audio params so the pipeline can apply them after open()
        adapter.set_audio_params(
            sample_rate_hz=float(sdr_cfg.get("sample_rate_hz") or 2_400_000),
            ppm=float(sdr_cfg.get("ppm") or 0),
            gain_auto=bool(sdr_cfg.get("gain_auto", True)),
            gain_db=float(sdr_cfg.get("gain_db") or 40.0),
            bias_tee=bool(sdr_cfg.get("bias_tee", False)),
        )
        return adapter

    def _on_lock_changed(self, locked: bool) -> None:
        """Update the _trsp_lock flag when the L button is toggled.

        Starts/stops the dial-feedback poller (_lock_watch_cycle()).
        Deliberately does NOT reset self._dial_feedback_offset_hz -- see
        its declaration in __init__ for why: turning Lock off must
        resume Doppler correction from wherever the operator left DL,
        not snap back to the pure nominal-based trajectory. The offset
        only resets on selecting a different transponder or pressing the
        T (Tune) button.
        """
        self._trsp_lock = locked
        if locked:
            self._lock_watch_worker.start()
        else:
            self._lock_watch_worker.stop()

    def _invalidate_sdr_retune_cache(self) -> None:
        """Let the next Doppler write retune the SDR unconditionally.

        SdrRigAdapter suppresses retunes smaller than its deadband (see
        _SDR_RETUNE_DEADBAND_HZ) so slow Doppler drift stops re-locking the
        tuner PLL every cycle.  That deadband must not swallow a *deliberate*
        move, though -- the smallest Passband Tune step is 100 Hz, well under
        it -- so every place that changes the target frequency for a reason
        other than Doppler calls this first.
        """
        for rig in (self._rig_controller, self._rig2_controller):
            if rig is not None and getattr(rig, "is_sdr", False):
                invalidate = getattr(rig, "invalidate_retune_cache", None)
                if invalidate is not None:
                    invalidate()

    @Slot(float)
    def _on_sdr_tune_offset(self, offset_hz: float) -> None:
        """Store the passband tune offset emitted by SdrControlWidget."""
        self._sdr_tune_offset = offset_hz
        self._invalidate_sdr_retune_cache()

    @Slot(bool)
    def _on_sdr_lock_changed(self, locked: bool) -> None:
        """Store SDR Control's own Lock state.

        No other transition logic is needed here (unlike Radio Control's
        Lock / _lock_watch_worker): _doppler_cycle() recomputes
        _sdr_tune_offset fresh every cycle straight from the SDR's actual
        live frequency while this is True, so turning it back off just
        resumes the normal dl_corr + _sdr_tune_offset write with whatever
        offset was last computed -- there is no separate "on release"
        step to perform, and no read-back ambiguity to guard against the
        way there is for a physical rig's VFO (the software always knows
        the SDR's exact current frequency).
        """
        self._sdr_lock = locked
        if not locked:
            # Nothing was written to the SDR while Lock was on, so the
            # adapter's deadband reference is stale by however far the
            # operator moved things.  Make the first write after release
            # unconditional so Doppler tracking resumes from exactly where
            # they left it.
            self._invalidate_sdr_retune_cache()

    @Slot(float)
    def _on_sdr_lock_offset_computed(self, offset_hz: float) -> None:
        """Main-thread slot: mirror an SDR-Lock-recomputed offset into the UI.

        Emitted from _rig_send()/_rig2_send()'s background thread (see
        _doppler_cycle()) since SdrControlWidget.sync_tune_offset() touches
        a Qt widget's state and must not be called off the main thread.
        """
        self._sdr_control.sync_tune_offset(offset_hz)

    @Slot(float)
    def _on_sdr_manual_freq_requested(self, freq_hz: float) -> None:
        """Apply a manually typed absolute SDR frequency (Freq box, transponder selected).

        Recomputes _sdr_tune_offset from the last Doppler-corrected
        downlink centre (self._latest_doppler.dl_corr) so the requested
        frequency persists through dl_rig1 = dl_corr + _sdr_tune_offset
        regardless of SDR Lock state, exactly like the ◀◀/◀/▶/▶▶ buttons
        — instead of writing to the SDR directly and letting the very
        next Doppler cycle silently revert it (the root confusion behind
        GitHub Issue #12). SdrControlWidget only emits this while a
        transponder is selected (self._latest_doppler is reset to None on
        every transponder change, so a stale reading from the previous
        selection can never be used here).
        """
        if self._latest_doppler is None or self._latest_doppler.dl_corr is None:
            return
        new_offset = freq_hz - self._latest_doppler.dl_corr
        self._sdr_tune_offset = new_offset
        self._sdr_control.sync_tune_offset(new_offset)
        self._invalidate_sdr_retune_cache()

    def _apply_mode_toggle_to_rig(self, dl_mode: str, ul_mode: str) -> None:
        """Apply an arbitrary DL/UL mode pair to Rig 1 in a background thread,
        without disconnecting or re-selecting the transponder.

        Shared by the Radio Control CW toggle button and the DATA (-D) mode
        toggle button — both need to flip a live SSB transponder to a
        different mode pair and back without forcing a reconnect (satmode
        rigs would otherwise disconnect via _apply_transponder_state_to_rig()).

        All HamlibDirectController rigs (satmode IC-9100/9700 etc., FTX-1F,
        FT-991, and generic Direct rigs alike) go through
        apply_transponder_state() — the exact same method transponder
        selection itself uses (main_window._apply_transponder_state_to_rig()
        calls it via a background thread too), so this toggle can never
        behave differently from a normal transponder selection for a given
        rig. It already dispatches internally to the raw-CAT path for
        FTX-1F/FT-991, to _apply_mode_and_ctcss_hamlib() for satmode rigs,
        and to the generic Hamlib send_mode_only()+set_ctcss_tone() fallback
        otherwise (GitHub Issue #16 — previously this went through
        send_mode_only() directly for satmode Direct rigs, a much thinner
        path with no VFO-restore step and no error surfacing).

        NET-mode rigs (HamlibNetController) are unchanged: send_mode_only()
        already works there per confirmed FTX-1 NET testing.
        """
        rig = self._rig_controller
        if rig is None:
            return

        from rig.controller import HamlibDirectController

        if isinstance(rig, HamlibDirectController):
            # Preserve the current CTCSS tone when reverting to the original
            # mode; suppress it when switching to CW (CW has no CTCSS).
            ctcss_hz = 0.0 if dl_mode in ("CW", "CW-R") else float(self._ctcss_tone_hz or 0.0)

            # FTX-1F/FT-991 raw CAT (_RAW_CAT_MODEL_IDS) writes mode via
            # os.open()/pyserial alongside an already-open Hamlib session —
            # no port conflict, no disconnect needed (mirrors
            # _apply_transponder_state_to_rig()'s dispatch for these rigs).
            if rig.is_connected and rig._model_id not in _RAW_CAT_MODEL_IDS:
                # Satmode and generic Direct rigs always open a FRESH
                # Hamlib session for apply_transponder_state() (satmode:
                # _apply_mode_and_ctcss_hamlib(); generic: send_mode_only()),
                # which conflicts with the already-open persistent
                # connection. Disconnect first, apply, then automatically
                # reconnect — same pattern already used for the satmode
                # CTCSS button (see _on_ctcss_send()'s satmode branch).
                self._disconnect_rig()  # must run on UI thread

                def _send_direct_reconnect() -> None:
                    try:
                        rig.apply_transponder_state(dl_mode, ul_mode, ctcss_hz)
                    except Exception as exc:
                        self._rig_error.emit(str(exc))
                    try:
                        rig.connect()
                    except Exception as exc:
                        self._rig_error.emit(f"reconnect after mode toggle: {exc}")
                        return
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(0, self._on_satmode_rig_reconnected)

                threading.Thread(target=_send_direct_reconnect, daemon=True).start()
                return

            def _send_direct() -> None:
                try:
                    rig.apply_transponder_state(dl_mode, ul_mode, ctcss_hz)
                except Exception as exc:
                    self._rig_error.emit(str(exc))

            threading.Thread(target=_send_direct, daemon=True).start()
        else:

            def _send() -> None:
                try:
                    rig.send_mode_only(dl_mode, ul_mode)
                except Exception as exc:
                    self._rig_error.emit(str(exc))

            threading.Thread(target=_send, daemon=True).start()

    def _on_ctcss_activate(self) -> None:
        """Send the satellite's activation tone (tone_hz from CTCSS_DB)."""
        if self._ctcss_activation_hz is not None:
            self._on_ctcss_send(self._ctcss_activation_hz)

    def _on_ctcss_send(self, tone_hz: float) -> None:
        """Send a CTCSS tone to the rig (background thread); errors shown in status bar."""
        self._current_ctcss_tone = tone_hz  # persist label until next transponder change

        from rig.controller import HamlibDirectController, HamlibNetController

        if self._rig_controller is None:
            return

        rig = self._rig_controller

        # Direct mode: CTCSS routing depends ONLY on the connected rig's model
        # (satmode -> Hamlib, FTX-1F/FT-991 -> their own raw CAT sequence,
        # other generic rigs -> Hamlib). The "CTCSS Method" dropdown in Rig
        # Settings is a NET-mode-only setting (greyed out for Direct mode) and
        # must never be consulted for a HamlibDirectController -- it can hold
        # a stale value left over from a previous NET-mode configuration or a
        # different rig model on the same slot, which would otherwise route
        # the wrong (irrelevant) CAT template to the currently connected rig.
        if isinstance(rig, HamlibDirectController) and rig._model_id in _RAW_CAT_MODEL_IDS:
            mode = str((self._current_transmitter or {}).get("mode") or "")
            invert = bool((self._current_transmitter or {}).get("invert", False))
            dl_mode = mode
            ul_mode = _MODE_INVERT.get(mode, mode) if invert else mode

            def _send_raw_cat() -> None:
                try:
                    rig.apply_transponder_state(dl_mode, ul_mode, tone_hz)
                except RigControlError as exc:
                    self._rig_error.emit(str(exc))
                except Exception as exc:
                    self._rig_error.emit(str(exc))

            threading.Thread(target=_send_raw_cat, daemon=True).start()
            return

        # NET mode: the CTCSS Method dropdown (ftx1/ft991/custom_cat templates)
        # is meaningful here and selects the raw CAT template to send.
        if isinstance(rig, HamlibNetController) and self._ctcss_method in self._CAT_CTCSS_METHODS:
            self._send_ctcss_cat_to_rig(tone_hz=tone_hz)
            return

        # Satmode Direct rigs (IC-9100 etc.): CTCSS must be set via CI-V while the
        # serial port is not held by Hamlib.  When connected, disconnect first, send
        # CI-V tone, then reconnect.  This path is NEVER reached for FT-991A / FTX-1F
        # (handled by the raw-CAT branch above) or NET rigs (not HamlibDirectController).
        if isinstance(rig, HamlibDirectController) and rig._satmode and rig.is_connected:
            self._disconnect_rig()  # release serial port on UI thread

            def _satmode_civ_reconnect() -> None:
                # Port is now free; set_ctcss_tone() will call _apply_ctcss_civ().
                try:
                    ok = rig.set_ctcss_tone(tone_hz)
                    if not ok:
                        self._rig_error.emit("CTCSS Error: no ACK from rig after 3 attempts")
                except Exception as exc:
                    self._rig_error.emit(f"set_ctcss_tone (satmode CI-V): {exc}")
                # Reconnect Doppler tracking.
                try:
                    rig.connect()
                except Exception as exc:
                    self._rig_error.emit(f"reconnect after CTCSS: {exc}")
                    return
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, self._on_satmode_rig_reconnected)

            threading.Thread(target=_satmode_civ_reconnect, daemon=True).start()
            return

        # Generic rigs' set_ctcss_tone() handles both the connected case and
        # the not-yet-connected case internally (HamlibDirectController via
        # _apply_ctcss_hamlib_standalone(), HamlibNetController via an
        # independent socket) — no is_connected guard needed here, unlike
        # before those were added.
        def _send() -> None:
            try:
                ok = rig.set_ctcss_tone(tone_hz)
                if not ok:
                    self._rig_error.emit(f"set_ctcss_tone({tone_hz} Hz): rig returned failure")
            except Exception as exc:
                self._rig_error.emit(f"set_ctcss_tone: {exc}")

        threading.Thread(target=_send, daemon=True).start()

    @Slot()
    def _on_satmode_rig_reconnected(self) -> None:
        """Called on the UI thread after satmode CI-V CTCSS + reconnect completes."""
        self._radio_control.refresh_status()
        self._update_rig_label()
        self._on_rig_slot_connected(1)

    def _on_tune_requested(self) -> None:
        """T button pressed: reset to the centre frequency of the current transponder band."""
        if self._current_transmitter is None:
            return
        # Discards any accumulated Lock dial-feedback offset -- T means
        # "go back to the exact centre", overriding any manual retuning.
        # The persistent per-transponder Offset (GitHub Issue #18) is
        # deliberately NOT discarded here -- it is a saved, known correction
        # for this transponder (not an in-session manual retune), so "centre"
        # means the offset-corrected centre. This is what lets pressing T at
        # the start of a pass replace re-finding the frequency by ear/RIT.
        self._dial_feedback_offset_hz = 0.0
        rx_offset_hz = float(self._current_transmitter.get("rx_offset_hz") or 0.0)
        dl_low = self._current_transmitter.get("downlink_low")
        dl_high = self._current_transmitter.get("downlink_high")
        ul_low = self._current_transmitter.get("uplink_low")
        ul_high = self._current_transmitter.get("uplink_high")

        if dl_low is not None and dl_high is not None:
            self._tune_dl_override = (float(dl_low) + float(dl_high)) / 2 + rx_offset_hz
        elif dl_low is not None:
            self._tune_dl_override = float(dl_low) + rx_offset_hz

        if ul_low is not None and ul_high is not None:
            self._tune_ul_override = (float(ul_low) + float(ul_high)) / 2
        elif ul_low is not None:
            self._tune_ul_override = float(ul_low)

    def _load_cycle_setting(self) -> None:
        """Load rig_cycle_ms from the DB and apply it to DopplerWorker and the UI.

        Controls DopplerWorker's cycle interval, not self._timer (the 1s
        display-refresh timer is fixed and no longer user-configurable —
        see __init__).
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'rig_cycle_ms'"
            ).fetchone()
            if row is not None:
                ms = int(row["value"])
                ms = max(10, min(10000, ms))
                self._doppler_worker.set_interval(ms / 1000.0)
                self._radio_control.set_cycle(ms)
        except Exception as exc:
            logger.warning("Failed to load cycle setting: %s", exc)

    def _on_cycle_changed(self, ms: int) -> None:
        """Update DopplerWorker's interval and save to DB when the Cycle spinbox changes."""
        ms = max(10, min(10000, ms))
        self._doppler_worker.set_interval(ms / 1000.0)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('rig_cycle_ms', ?)",
                (str(ms),),
            )
            self._conn.commit()
        except Exception as exc:
            logger.warning("Failed to save cycle setting: %s", exc)

    def _on_tx_owner_changed(self, device: object, owner: object) -> None:
        """Update the status bar TX-owner indicator.

        Shows which Communications tab currently holds the shared soundcard
        output lock (see `AudioDeviceManager` in
        `src/comms/audio_device_manager.py`), so a tab whose transmit
        silently fails with "Sound card output is in use by X" can be
        cross-checked against what the status bar says is holding it.
        """
        if owner:
            self._tx_owner_label.setText(_("\U0001f50a TX: {owner}").format(owner=owner))
            self._tx_owner_label.setVisible(True)
        else:
            self._tx_owner_label.setVisible(False)

    def _update_rig_label(self) -> None:
        """Update the RIG status label in the status bar.

        Shows "RIG: Off" when neither rig is configured or connected.
        Shows "RIG: 1" / "RIG: 1+2" to indicate which rigs are active.
        """
        r1 = self._rig_controller is not None and self._rig_controller.is_connected
        r2 = self._rig2_controller is not None and self._rig2_controller.is_connected
        if r1 and r2:
            self._rig_label.setText(_("RIG: 1+2"))
        elif r1:
            self._rig_label.setText(_("RIG: 1"))
        elif r2:
            self._rig_label.setText(_("RIG: 2"))
        else:
            self._rig_label.setText(_("RIG: Off"))

    def _on_rotator_settings(self) -> None:
        from ui.rotator_dialog import RotatorSettingsDialog

        dialog = RotatorSettingsDialog(self._conn, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._load_rotator_settings()

    def _load_rotator_settings(self) -> None:
        """Load rotator settings from the DB and instantiate the controller."""
        try:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'rotator_settings'"
            ).fetchone()
            if row is None:
                return
            settings: dict[str, Any] = json.loads(row["value"])
            mode = settings.get("mode", "net")
            if mode == "net":
                host = str(settings.get("host", "localhost"))
                port = int(settings.get("net_port", 4533))
                self._rotator_controller = HamlibRotatorController(
                    net_mode=True,
                    net_host=host,
                    net_port=port,
                )
            else:
                model_id = int(settings.get("model_id", 1))
                serial_port = str(settings.get("port", "/dev/ttyUSB0"))
                baud = int(settings.get("baud_rate", 9600))
                self._rotator_controller = HamlibRotatorController(
                    model_id=model_id,
                    port=serial_port,
                    baud_rate=baud,
                )
            self._radio_control.set_rotator(self._rotator_controller)
            self._update_rot_label()
        except Exception as exc:
            logger.warning("Failed to load rotator settings: %s", exc)
        try:
            south_row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'rotator_south_init'"
            ).fetchone()
            checked = bool(int(south_row["value"])) if south_row else False
            self._rotator_south_init = checked
            self._radio_control.set_south_init(checked)
        except Exception as exc:
            logger.warning("Failed to load rotator_south_init: %s", exc)

    def _on_rotator_connected(self) -> None:
        """Send the current satellite position to the rotator immediately after connect."""
        if self._rotator_controller is None or not self._rotator_controller.is_connected:
            return

        # Fetch actual rotator position and show on radar; fall back to (0, 0)
        # if get_position() returns the default RotatorState.
        rot_ctrl = self._rotator_controller

        def _fetch_init_pos() -> None:
            pos = rot_ctrl.get_position()
            self._rot_pos_updated.emit(pos.azimuth_deg, pos.elevation_deg)

        threading.Thread(target=_fetch_init_pos, daemon=True).start()

        if self._selected_norad is None or self._engine is None:
            return
        obs = self._engine.observe(self._selected_norad)
        if obs is None:
            return
        rot = self._rotator_controller
        az = self._apply_south_offset(obs.azimuth_deg)
        el = obs.elevation_deg
        threading.Thread(target=lambda: rot.set_position(az, el), daemon=True).start()
        self._update_rot_label()

    def _apply_south_offset(self, az: float) -> float:
        """Apply 180-degree offset when the rotator is south-initialized."""
        if not self._rotator_south_init:
            return az
        return (az + 180) % 360

    def _on_south_init_changed(self, checked: bool) -> None:
        """Update state and persist south_init setting when the checkbox is toggled."""
        self._rotator_south_init = checked
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('rotator_south_init', ?)",
                ("1" if checked else "0",),
            )
            self._conn.commit()
        except Exception as exc:
            logger.warning("Failed to save rotator_south_init: %s", exc)

    def _update_rot_label(self) -> None:
        """Update the ROT label in the status bar."""
        if self._rotator_controller is None:
            self._rot_label.setText(_("ROT: Off"))
        elif self._rotator_controller.is_connected:
            self._rot_label.setText(_("ROT: On"))
        else:
            self._rot_label.setText(_("ROT: Off"))

    def _load_saved_language(self) -> None:
        """Apply the saved UI language before any widgets are built.

        Falls back to the OS locale on first launch (no saved preference
        yet): Japanese if the system locale is Japanese, English otherwise.
        Any explicit choice made via View > Language is always saved and
        takes priority over this detection on subsequent launches.
        """
        from i18n import set_language

        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'ui_language'"
        ).fetchone()
        lang = row["value"] if row and row["value"] else self._detect_system_language()
        set_language(lang)

    @staticmethod
    def _detect_system_language() -> str:
        """Return "ja" if the OS locale is Japanese, else "en"."""
        from PySide6.QtCore import QLocale

        if QLocale.system().name().startswith("ja"):
            return "ja"
        return "en"

    def _on_set_language(self, lang: str) -> None:
        from i18n import set_language

        self._conn.execute(
            """
            INSERT OR REPLACE INTO app_settings (key, value, updated_at)
            VALUES ('ui_language', ?, CURRENT_TIMESTAMP)
            """,
            (lang,),
        )
        self._conn.commit()
        set_language(lang)
        QMessageBox.information(
            self,
            _("Language"),
            _("FBSAT59 will restart now to apply the language change."),
        )
        self._restart_application()

    def _restart_application(self) -> None:
        """Spawn a fresh FBSAT59 process and quit this one (used after a language change).

        Works for both a source checkout (``python src/main.py``) and a frozen
        PyInstaller bundle (AppImage/.exe/.dmg), since sys.executable/sys.argv
        resolve correctly in both cases (see app_update_dialog.py's Linux
        installer, which relies on the same assumption).
        """
        if getattr(sys, "frozen", False):
            args = [sys.executable, *sys.argv[1:]]
        else:
            args = [sys.executable, *sys.argv]

        from PySide6.QtWidgets import QApplication

        def _spawn() -> None:
            try:
                subprocess.Popen(args)
            except Exception:
                logger.exception("Failed to spawn a new FBSAT59 process for restart")

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(_spawn)
        QApplication.quit()

    def _on_satellite_color(self) -> None:
        """Show the satellite/transmitter color legend dialog."""
        dialog = QDialog(self)
        dialog.setWindowTitle(_("Satellite/Transmitter Color Legend"))
        dialog.setMinimumWidth(480)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(0)

        # Build the legend using an HTML table rendered in a QLabel
        rows_html = [
            (
                "#2ecc71",
                "bold",
                _("Green (bold)"),
                _("AMSAT status: Operational — confirmed working by AMSAT."),
            ),
            (
                "#f1c40f",
                "normal",
                _("Yellow"),
                _("AMSAT status: Partially operational — degraded but active."),
            ),
            (
                "#e74c3c",
                "normal",
                _("Red"),
                _("AMSAT status: Non-operational — confirmed failed by AMSAT."),
            ),
            (
                "#e67e22",
                "normal",
                _("Orange"),
                _("SATNOGS alive — reception reported, no AMSAT data available."),
            ),
            (
                "#9b59b6",
                "italic",
                _("Purple (italic)"),
                _(
                    "TLE pending — alive satellite awaiting TLE assignment "
                    "(provisional NORAD ≥ 90000, within 30-day grace period). "
                    "Position cannot be displayed yet."
                ),
            ),
            (
                "#7f8c8d",
                "normal",
                _("Gray"),
                _("Status unknown — not confirmed operational by any source."),
            ),
            (
                "#4488ff",
                "normal",
                _("Blue"),
                _("Celestial Bodies (Moon etc.) — below the horizon."),
            ),
            (
                "#4488ff",
                "bold",
                _("Blue (bold)"),
                _("Celestial Bodies (Moon etc.) — above the horizon."),
            ),
        ]

        # Transponder combo box background colors (Radio Control tab). Shown only
        # while the dropdown is open — see radio_control_widget.py's
        # _XPDR_INACTIVE_BG / _XPDR_INVALID_BG.
        xpdr_rows_html = [
            (
                "#b8860b",
                _("Yellow background"),
                _(
                    "SATNOGS status: Inactive — this transmitter is no longer "
                    "operating. Shown only while the transponder dropdown is open."
                ),
            ),
            (
                "#8b0000",
                _("Red background"),
                _(
                    "SATNOGS status: Invalid — this transmitter's data is known "
                    "to be incorrect. Shown only while the transponder dropdown is open."
                ),
            ),
        ]

        def _heading_row(text: str) -> str:
            return (
                "<tr><td colspan='2' style='padding:14px 8px 4px; "
                f"font-weight:bold; color:#111;'>{text}</td></tr>"
            )

        def _swatch(color: str) -> str:
            # Qt's rich text engine (QTextDocument) doesn't apply width/height/
            # display:inline-block to an empty <span>, and doesn't understand the
            # 'background' shorthand — only 'background-color' on an element with
            # actual content renders as a filled box.
            return (
                f'<span style="background-color:{color}; border:1px solid #555;">'
                f"&nbsp;&nbsp;&nbsp;&nbsp;</span>&nbsp;"
            )

        table_rows = _heading_row(_("Satellite List"))
        for color, style, label, desc in rows_html:
            weight = "bold" if style == "bold" else "normal"
            fstyle = "italic" if style == "italic" else "normal"
            label_html = (
                f'<span style="color:{color}; font-weight:{weight};'
                f' font-style:{fstyle};">{label}</span>'
            )
            table_rows += (
                f"<tr>"
                f"<td style='padding:6px 8px; white-space:nowrap;'>"
                f"{_swatch(color)}{label_html}</td>"
                f"<td style='padding:6px 8px; color:#111;'>{desc}</td>"
                f"</tr>"
            )

        table_rows += _heading_row(_("Radio Control — Transmitter Selection"))
        for color, label, desc in xpdr_rows_html:
            table_rows += (
                f"<tr>"
                f"<td style='padding:6px 8px; white-space:nowrap;'>"
                f"{_swatch(color)}{label}</td>"
                f"<td style='padding:6px 8px; color:#111;'>{desc}</td>"
                f"</tr>"
            )

        html = (
            "<html><body style='color:#111;'>"
            "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;'>"
            + table_rows
            + "</table></body></html>"
        )

        legend_label = QLabel(html)
        legend_label.setWordWrap(True)
        legend_label.setTextFormat(Qt.TextFormat.RichText)
        legend_label.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(legend_label)

        from PySide6.QtWidgets import QDialogButtonBox  # noqa: PLC0415

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)

        dialog.exec()

    def _on_auto_fetch_rules(self) -> None:
        """Show a dialog explaining the automatic TLE and transponder fetch schedule."""
        intro = _(
            "FBSAT59 automatically fetches and updates TLE orbital data "
            "and transmitter information in the background. Manual updates are normally "
            "not required. Use manual sync only when you need the very latest data "
            "immediately (e.g. right before a pass of a newly launched satellite)."
        )
        schedule_table = _(
            "<table border='0' cellspacing='4'>"
            "<tr><td><b>Space Stations</b> (ISS, CSS…)</td><td>every <b>1 hour</b></td></tr>"
            "<tr><td><b>Amateur Satellites</b></td><td>every <b>2 hours</b></td></tr>"
            "<tr><td><b>CubeSats</b></td><td>every <b>4 hours</b></td></tr>"
            "<tr><td><b>Weather Satellites</b></td><td>every <b>6 hours</b></td></tr>"
            "<tr><td><b>Earth Observation / Science</b></td>"
            "<td>every <b>12 hours</b></td></tr>"
            "<tr><td><b>Provisional TLEs</b> (NORAD ≥ 90000)</td>"
            "<td>every <b>12 hours</b></td></tr>"
            "<tr><td><b>Active TLE fallback</b> (NORAD 10000–89999)</td>"
            "<td>every <b>24 hours</b></td></tr>"
            "<tr><td><b>AMSAT operational status</b></td><td>every <b>24 hours</b></td></tr>"
            "<tr><td><b>METEOR/HRPT satellites dropped from CelesTrak groups</b></td>"
            "<td>every <b>12 hours</b></td></tr>"
            "<tr><td><b>Transmitter Database (SATNOGS)</b></td>"
            "<td>every <b>7 days</b></td></tr>"
            "</table>"
        )
        xpdr_paragraph = _(
            "Transmitter data is fetched from SATNOGS automatically on first launch, "
            "then refreshed automatically every 7 days in the background. "
            "Use <b>Satellite → Sync SATNOGS</b> to refresh transmitter "
            "frequencies and modes manually whenever you need the very latest data sooner "
            "(e.g. right after a newly launched satellite registers its transmitter)."
        )
        startup_paragraph = _(
            "On each launch the app syncs satellite names and statuses from SATNOGS, "
            "and fetches any TLE sources whose cached data has expired."
        )
        heading_main = _("Auto Fetch Rules")
        heading_schedule = _("TLE Auto-Fetch Schedule")
        heading_xpdr = _("Transmitter Database (SATNOGS)")
        heading_startup = _("At Startup")
        msg = (
            f"<h3>{heading_main}</h3>"
            f"<p>{intro}</p>"
            f"<h4>{heading_schedule}</h4>"
            f"{schedule_table}"
            f"<h4>{heading_xpdr}</h4>"
            f"<p>{xpdr_paragraph}</p>"
            f"<h4>{heading_startup}</h4>"
            f"<p>{startup_paragraph}</p>"
        )
        dlg = QMessageBox(self)
        dlg.setWindowTitle(_("Auto Fetch Rules"))
        dlg.setTextFormat(Qt.TextFormat.RichText)
        dlg.setText(msg)
        dlg.setIcon(QMessageBox.Icon.Information)
        dlg.exec()

    def _on_clear_tle_sync_history(self) -> None:
        """Help > Clear TLE Sync History… handler.

        Deletes every sync_log row tied to TLE fetching (all the auto-fetch
        schedule entries listed in Auto Fetch Rules above, except the
        transmitter/satellite-name syncs, which are unrelated to TLE
        freshness). Every staleness gate that consults sync_log
        (is_active_tle_stale(), is_source_stale(), is_group_empty()) then
        treats those sources as never having run, so the next relevant
        trigger — app startup, the periodic APScheduler jobs, or Satellite >
        Update TLE — refetches everything from scratch instead of skipping
        sources it thinks are still fresh.

        Exists because on a release build there is no SQL console to delete
        these rows directly (unlike a dev checkout, where deleting the
        celestrak-active row was used repeatedly on 2026-08-09/10 to force a
        fetch_active_tles() retry while chasing a stale ORIGAMISAT-2 TLE) —
        see CLAUDE.md for that investigation. Note that Satellite > Update
        TLE (as of 2026-08-10) already bypasses is_active_tle_stale() on its
        own, so this is not required just to force a manual refresh — it's
        for reproducing what a *fresh* startup/scheduled run would do.
        """
        from data.tle_manager import TLE_SOURCES

        tle_sync_types = [str(s["name"]) for s in TLE_SOURCES] + [
            "celestrak-active",
            "satnogs-provisional",
            "legacy-tle-check",
            "meteor-tle-check",
        ]

        reply = QMessageBox.question(
            self,
            _("Clear TLE Sync History"),
            _(
                "This clears the record of when each TLE source was last fetched.\n\n"
                "It does not delete any TLE data itself — only the timestamps used to "
                "decide whether a source is due for a refresh. The next app startup, "
                "scheduled background fetch, or Satellite → Update TLE will treat every "
                "TLE source as never having been fetched and refresh all of them."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        placeholders = ",".join("?" for _t in tle_sync_types)
        cur = self._conn.execute(
            f"DELETE FROM sync_log WHERE sync_type IN ({placeholders})",
            tle_sync_types,
        )
        self._conn.commit()
        QMessageBox.information(
            self,
            _("Clear TLE Sync History"),
            _("Cleared {n} sync history record(s).").format(n=cur.rowcount),
        )

    def _on_about(self) -> None:
        from PySide6.QtWidgets import QApplication

        ver = QApplication.applicationVersion() or "0.1.0"
        QMessageBox.information(
            self,
            _("About FBSAT59"),
            f"FBSAT59  v{ver}\n\n"
            + _("Modern satellite tracking software for amateur radio operators.\n")
            + "https://github.com/JF9SOM/fbsat59",
        )

    def _on_github(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl("https://github.com/JF9SOM/fbsat59"))

    def _on_sdr_install(self) -> None:
        from ui.sdr_install_dialog import SdrInstallDialog

        dlg = SdrInstallDialog(self)
        dlg.exec()

    def _on_check_updates(self) -> None:
        from PySide6.QtWidgets import QApplication

        from ui.app_update_dialog import AppUpdateDialog

        dlg = AppUpdateDialog(self)
        dlg.quit_requested.connect(QApplication.quit)
        dlg.exec()

    def _on_hamlib_update(self) -> None:
        from ui.hamlib_update_dialog import HamlibUpdateDialog

        dlg = HamlibUpdateDialog(self)
        dlg.exec()

    def _on_ft8lib_help(self) -> None:
        from ui.ft8lib_dialog import Ft8LibDialog

        dlg = Ft8LibDialog(self)
        dlg.exec()

    def _on_q65lib_help(self) -> None:
        from ui.q65lib_dialog import Q65LibDialog

        dlg = Q65LibDialog(self)
        dlg.exec()

    def _on_ft4wsjt_help(self) -> None:
        from ui.ft4wsjt_dialog import Ft4WsjtDialog

        dlg = Ft4WsjtDialog(self)
        dlg.exec()

    def _on_direwolf_help(self) -> None:
        from ui.direwolf_dialog import DirewolfDialog

        dlg = DirewolfDialog(self)
        dlg.exec()

    def _on_gr_satellites_help(self) -> None:
        from ui.gr_satellites_dialog import GrSatellitesDialog

        dlg = GrSatellitesDialog(self)
        dlg.exec()

    def _on_rig_slot_connected(self, slot: int) -> None:
        """Called when Rig 1 or Rig 2 connects.  Starts SDR pipeline if the slot is an SDR.

        For satmode NET rigs (IC-9700 etc.), also sends mode after connect so that
        the mode command arrives after S 1 Main has activated satmode (which resets
        VFO modes on these rigs).  Only slot 1 (primary rig) participates in mode
        tracking.
        """
        # CTCSS is sent at transponder-selection time for all rigs (IC-9100 style).
        # No re-send needed here.

        from rig.controller import SdrRigAdapter
        from sdr import SOAPY_AVAILABLE

        if not SOAPY_AVAILABLE:
            return

        rig = self._rig_controller if slot == 1 else self._rig2_controller
        if not isinstance(rig, SdrRigAdapter):
            return

        device = rig.sdr_device
        if device is None:
            return

        # Load IQ save dir from settings
        try:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key='sdr_settings'"
            ).fetchone()
            import json as _json

            sdr_cfg = _json.loads(row["value"]) if row and row["value"] else {}
        except Exception:
            sdr_cfg = {}

        iq_dir = sdr_cfg.get("iq_save_dir", "")
        self._sdr_control.set_iq_save_dir(str(iq_dir))

        from sdr.pipeline import SDRPipeline

        pipeline = SDRPipeline(device, parent=self)
        rig.attach_pipeline(pipeline)
        self._sdr_control.set_pipeline(pipeline)
        pipeline.start()
        self._notify_comms_tabs_sdr_pipeline(pipeline)
        self._update_rig_label()

    def _on_rig_slot_disconnected(self, slot: int) -> None:
        """Called when Rig 1 or Rig 2 disconnects via the UI button."""
        rig = self._rig_controller if slot == 1 else self._rig2_controller
        if getattr(rig, "is_sdr", False):
            self._sdr_control.set_pipeline(None)
            self._notify_comms_tabs_sdr_pipeline(None)
        self._update_rig_label()

    def _notify_comms_tabs_sdr_pipeline(self, pipeline: Any) -> None:
        """Tell any open Communications tab holding a stale SDR pipeline reference to refresh it.

        MainWindow creates a brand-new SDRPipeline every time Rig 1/2
        (re)connects as the SDR. SdrControlWidget itself always tracks the
        current one correctly (via set_pipeline()), but CW/FT4/Q65 each
        grab a one-shot pipeline reference the first time they subscribe
        (Start press for CW, __init__ for FT4/Q65) and have no other way
        to notice it was replaced — the Level meter (CW) or decode buffer
        (FT4/Q65) would then silently stop receiving anything after any
        later SDR reconnect, with no obvious indication anything had gone
        wrong (GitHub Issue #12 follow-up). refresh_sdr_pipeline() is
        duck-typed; only these three tabs define it.

        SSTV and Telemetry are deliberately NOT covered here: both already
        re-fetch the pipeline fresh from their own rig_connected/
        rig_disconnected handlers (connected directly to
        RadioControlWidget, independent of this method), so calling a
        second refresh path on them would double-subscribe their audio
        callback instead of fixing anything.
        """
        for tab in list(self._comms_tab_keys.keys()):
            refresh = getattr(tab, "refresh_sdr_pipeline", None)
            if refresh is not None:
                refresh(pipeline)

    def _notify_comms_tabs_use_utc(self, use_utc: bool) -> None:
        """Tell any open Communications tab that supports it to switch its
        own timestamp display between UTC and Local (View > Time Zone).

        Duck-typed like _notify_comms_tabs_sdr_pipeline() above; FT4/APRS/
        Telemetry deliberately do not define set_use_utc() and always show
        UTC (matches ADIF/ham-log convention for QSO timestamps) — only
        tabs that opt in via defining the method are affected.
        """
        for tab in list(self._comms_tab_keys.keys()):
            set_use_utc = getattr(tab, "set_use_utc", None)
            if set_use_utc is not None:
                set_use_utc(use_utc)

    def _autotrack_on_aos(self, sat_name: str) -> None:
        """Called by Autotrack when a new satellite's AOS is detected.

        Connects rig and rotator if not already connected, then starts
        SDR recordings if the respective checkboxes are enabled.
        """
        # Connect Rig 1
        if self._rig_controller is not None and not self._rig_controller.is_connected:
            self._rig_controller.connect()
            if self._rig_controller.is_connected:
                self._radio_control.refresh_status()
                self._update_rig_label()
                from ui.autotrack_record_dialog import AutotrackRecordDialog  # noqa: F401

                self._on_rig_slot_connected(1)
        # Connect Rig 2
        if self._rig2_controller is not None and not self._rig2_controller.is_connected:
            self._rig2_controller.connect()
            if self._rig2_controller.is_connected:
                self._radio_control.refresh_status()
                self._update_rig_label()
                self._on_rig_slot_connected(2)
        # Connect rotator
        if self._rotator_controller is not None and not self._rotator_controller.is_connected:
            self._rotator_controller.connect()
            self._radio_control.refresh_status()

        # Start SDR recordings
        norad = self._autotrack_tracking_norad or 0
        if self._autotrack_audio_record:
            try:
                self._sdr_control.start_audio_recording_for_autotrack(norad, sat_name)
            except Exception as exc:
                logger.warning("Autotrack audio record start failed: %s", exc)
        if self._autotrack_iq_record:
            try:
                self._sdr_control.start_iq_recording_for_autotrack()
            except Exception as exc:
                logger.warning("Autotrack IQ record start failed: %s", exc)

        # Open METEOR/HRPT tab and start reception if checkbox is enabled
        if self._autotrack_meteor_record:
            self._meteor_autotrack_aos(norad)

    def _autotrack_on_los(self) -> None:
        """Called by Autotrack when the current satellite's LOS is detected.

        Stops SDR recordings, then disconnects rig and rotator.
        """
        # Stop recordings
        if self._autotrack_audio_record:
            try:
                self._sdr_control.stop_audio_recording_for_autotrack()
            except Exception as exc:
                logger.warning("Autotrack audio record stop failed: %s", exc)
        if self._autotrack_iq_record:
            try:
                self._sdr_control.stop_iq_recording_for_autotrack()
            except Exception as exc:
                logger.warning("Autotrack IQ record stop failed: %s", exc)

        # Disconnect rig
        if self._rig_controller is not None and self._rig_controller.is_connected:
            is_sdr = getattr(self._rig_controller, "is_sdr", False)
            self._rig_controller.disconnect()
            if is_sdr:
                self._sdr_control.set_pipeline(None)
        if self._rig2_controller is not None and self._rig2_controller.is_connected:
            is_sdr2 = getattr(self._rig2_controller, "is_sdr", False)
            self._rig2_controller.disconnect()
            if is_sdr2:
                self._sdr_control.set_pipeline(None)
        # Disconnect rotator
        if self._rotator_controller is not None and self._rotator_controller.is_connected:
            self._rotator_controller.disconnect()

        self._radio_control.refresh_status()
        self._update_rig_label()
        self._autotrack_tracking_norad = None

        # Stop METEOR reception at LOS if checkbox is enabled
        if self._autotrack_meteor_record:
            self._meteor_autotrack_los()

    def _meteor_autotrack_aos(self, norad: int) -> None:
        """Open METEOR/HRPT tab (if needed) and start reception at AOS."""
        from comms.meteor.satdump import METEOR_NORAD_IDS
        from ui.meteor_tab import MeteorTab

        if norad not in METEOR_NORAD_IDS:
            return

        # Open the tab if not already open (reuses _on_open_meteor which also
        # syncs the pipeline combo to the arriving satellite's NORAD)
        self._on_open_meteor(norad=norad, downlink_hz=0)

        # Now find the tab and start
        tab_label = _("METEOR / HRPT")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                w = self._tab_widget.widget(i)
                if isinstance(w, MeteorTab):
                    w.autotrack_start(norad)
                return

    def _meteor_autotrack_los(self) -> None:
        """Stop METEOR reception at LOS."""
        from ui.meteor_tab import MeteorTab

        tab_label = _("METEOR / HRPT")
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) == tab_label:
                w = self._tab_widget.widget(i)
                if isinstance(w, MeteorTab):
                    w.autotrack_stop()
                return

    def _on_show_qr(self) -> None:
        if not self._web_server_url:
            QMessageBox.information(self, _("QR Code"), _("Web server is not running."))
            return
        try:
            from web.qrcode_helper import generate_qr_png

            png_bytes = generate_qr_png(self._web_server_url)
            dialog = QDialog(self)
            dialog.setWindowTitle(f"QR — {self._web_server_url}")
            dlg_layout = QVBoxLayout(dialog)
            img_label = QLabel()
            pixmap = QPixmap()
            pixmap.loadFromData(png_bytes)
            img_label.setPixmap(pixmap)
            dlg_layout.addWidget(img_label)
            dlg_layout.addWidget(QLabel(self._web_server_url))
            dialog.exec()
        except Exception as exc:
            logger.warning("QR code generation failed: %s", exc)
            QMessageBox.warning(self, _("QR Code"), _("Failed to generate QR code."))

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #

    def _release_rig_split_on_exit(self) -> None:
        """Send a split-off / simplex command to each configured rig on app exit.

        Reads rig settings directly from the DB and sends the command via a
        brand-new connection, completely independent of RigController state.
        This runs before background threads are stopped so nothing races.
        """
        import socket as _socket
        import time as _time

        for key in ("rig1_settings", "rig2_settings"):
            try:
                row = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (key,)
                ).fetchone()
                if not row and key == "rig1_settings":
                    row = self._conn.execute(
                        "SELECT value FROM app_settings WHERE key = 'rig_settings'"
                    ).fetchone()
                if not row:
                    continue
                settings: dict[str, Any] = json.loads(row[0])
                if settings.get("is_sdr"):
                    continue
                # Rig 2 has an explicit enabled flag (see _load_rig_settings());
                # skip disabled slots so exit doesn't touch an unused port and
                # log a misleading "split released" message.
                if key == "rig2_settings" and not settings.get("enabled", False):
                    continue
                mode = settings.get("mode", "net")

                if mode == "net":
                    host = str(settings.get("host", "localhost"))
                    port = int(settings.get("net_port", 4532))
                    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect((host, port))
                    sock.sendall(b"S 0 VFOA\n")
                    buf = b""
                    deadline = _time.monotonic() + 2.0
                    while _time.monotonic() < deadline:
                        try:
                            chunk = sock.recv(256)
                        except OSError:
                            break
                        if not chunk:
                            break
                        buf += chunk
                        if b"RPRT" in buf:
                            break
                    sock.close()
                    logger.info("exit: split released via rigctld %s:%d", host, port)

                else:
                    model_id = int(settings.get("model_id", 1))
                    serial_port = str(settings.get("port", "/dev/ttyUSB0"))
                    baud_rate = int(settings.get("baud_rate", 9600))
                    slot_ctrl = (
                        self._rig_controller if key == "rig1_settings" else self._rig2_controller
                    )
                    if model_id in _FTX1_MODEL_IDS:
                        # FTX-1F: restore VFO-A (Main) as TX via raw CAT FT0;
                        # FT0; = VFO-A TX, FT1; = VFO-B TX (FTX-1F convention).
                        import serial as _serial

                        with _serial.Serial(serial_port, baud_rate, timeout=1) as ser:
                            ser.write(b"FT0;")
                        logger.info("exit: TX restored to Main via raw CAT FT0; %s", serial_port)
                    elif model_id in _FT991_DIRECT_MODEL_IDS:
                        # FT-991/991A/991AM: use raw CAT FT2; (VFO-A TX = split OFF).
                        # ST command is not supported on FT-991A (?; response).
                        # pyserial opens independently of Hamlib, safe to call
                        # whether or not HamlibDirectController is still connected.
                        import serial as _serial

                        with _serial.Serial(serial_port, baud_rate, timeout=1) as ser:
                            ser.write(b"FT2;")
                        logger.info("exit: split released via raw CAT FT2; %s", serial_port)
                    elif model_id in _IC705_MODEL_IDS:
                        # IC-705: Hamlib's set_split_vfo() intermittently rejects
                        # this call on IC-705's backend (confirmed live), so send
                        # the raw CI-V split-OFF frame directly instead — same
                        # bypass-Hamlib approach as FTX-1F/FT-991 above.
                        import serial as _serial

                        addr_str = str(settings.get("civ_addr") or "").strip()
                        civ = (
                            int(normalize_civ_addr(addr_str), 16)
                            if addr_str
                            else _IC705_DEFAULT_CIV_ADDR
                        )
                        frame = bytes([0xFE, 0xFE, civ, 0xE0, 0x0F, 0x00, 0xFD])
                        with _serial.Serial(serial_port, baud_rate, timeout=1) as ser:
                            ser.write(frame)
                            ser.read(32)  # drain the rig's ACK — see _init_split()
                        logger.info(
                            "exit: split released via raw CI-V %s civ=0x%02X",
                            serial_port,
                            civ,
                        )
                    else:
                        if slot_ctrl is not None and slot_ctrl.is_connected:
                            # Release Hamlib's hold on the port first so the
                            # fresh session below can open it (mirrors
                            # FTX-1F/FT-991 above, which bypass this entirely
                            # via a separate raw-pyserial handle).
                            with contextlib.suppress(Exception):
                                slot_ctrl.disconnect()
                        try:
                            import Hamlib as _H
                        except ImportError:
                            continue
                        rig = _H.Rig(model_id)
                        rig.set_conf("rig_pathname", serial_port)
                        rig.set_conf("serial_speed", str(baud_rate))
                        rig.open()
                        _time.sleep(0.3)
                        with contextlib.suppress(Exception):
                            rig.set_func(_H.RIG_FUNC_SATMODE, 0)
                        with contextlib.suppress(Exception):
                            rig.set_split_vfo(_H.RIG_VFO_CURR, 0, _H.RIG_VFO_B)
                        rig.close()
                        logger.info(
                            "exit: split released via Hamlib Direct %s model %d",
                            serial_port,
                            model_id,
                        )
            except Exception as exc:
                logger.warning("exit: could not release split for %s — %s", key, exc)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop the timer, web server, and scheduler when the window is closed."""
        # Communications tabs (APRS, Telemetry, FT4, Q65, SSTV, CW Decoder,
        # METEOR/HRPT) are child widgets of the tab bar, not top-level
        # windows, so Qt never auto-invokes their closeEvent() just because
        # the main window is closing. Close them explicitly first so any
        # background subprocess (Direwolf, SatDump, gr-satellites) is
        # terminated and shared audio device locks are released instead of
        # being orphaned when this process exits.
        for i in range(self._tab_widget.count()):
            tab_widget = self._tab_widget.widget(i)
            if tab_widget is not None:
                with contextlib.suppress(Exception):
                    tab_widget.close()

        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('satellite_filter', ?)",
                (self._filter_combo.currentText(),),
            )
            self._conn.commit()
        # Send simplex command to each configured rig before tearing down.
        with contextlib.suppress(Exception):
            self._release_rig_split_on_exit()
        # Signal background threads to exit before tearing down other resources.
        self._shutdown_flag.set()
        self._timer.stop()
        self._doppler_worker.stop()
        if self._web_server is not None:
            with contextlib.suppress(Exception):
                self._web_server.stop()
        if self._scheduler is not None:
            with contextlib.suppress(Exception):
                self._scheduler.shutdown(wait=False)
        event.accept()
