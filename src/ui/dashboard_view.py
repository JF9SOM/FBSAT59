"""
Dashboard view — combined satellite tracking panel.

DashboardView  — side-by-side zoomed local map + radar + status bar.
Shows the selected satellite's footprint area (zoomed map) on the left,
a compact radar on the right, and a one-line status bar at the bottom
containing the key data normally shown in the Satellite Detail panel.

Usage::

    dash = DashboardView()
    dash.update_satellite(norad, name, obs, transmitter, sat_color)
    layout.addWidget(dash)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.celestial_engine import MOON_ID
from i18n import _
from ui.radar_view import RadarView, SatTrackData, az_el_to_xy  # noqa: F401
from ui.world_map import WorldMapView

if TYPE_CHECKING:
    from core.engine import Observation

# Half-span of the zoomed map in degrees.
# 50° means the visible region extends ±50° lat/lon from the satellite.
_ZOOM_SPAN_DEG = 50.0


class DashboardView(QWidget):
    """Combined zoomed-map / radar / status-bar widget for the Dashboard tab.

    Call ``update_satellite()`` every second with the latest observation data.
    The zoomed map automatically follows the selected satellite's footprint.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_norad: int | None = None
        self._selected_name: str = ""
        self._current_transmitter: dict[str, Any] | None = None
        # Smoothed zoom-center with velocity-predictive correction.
        # The zoom center is placed at:
        #   predicted_pos = current_sat_pos + velocity * _ZOOM_LEAD_S
        # where velocity is estimated from the previous two positions (1 Hz diff).
        # A small lerp is still applied to absorb any residual jitter.
        self._zoom_lat: float | None = None
        self._zoom_lon: float | None = None
        self._prev_sat_lat: float | None = None  # satellite position 1 tick ago
        self._prev_sat_lon: float | None = None
        # How many seconds ahead the zoom center is predicted (= lerp lag compensation).
        # At 0.25 lerp the center converges in ~4 s → lead by ~3 s.
        self._ZOOM_LEAD_S: float = 3.0
        # Lerp factor applied on top of velocity prediction (absorbs high-freq jitter).
        self._ZOOM_LERP: float = 0.25
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top: map (left) + radar (right) ────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Zoomed local map — grid lines disabled for a cleaner look
        self._local_map = WorldMapView()
        self._local_map.setMinimumSize(200, 200)
        self._local_map.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._local_map.set_show_grid(False)
        splitter.addWidget(self._local_map)

        # Compact radar
        self._radar = RadarView()
        self._radar.setMinimumSize(200, 200)
        self._radar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        splitter.addWidget(self._radar)

        splitter.setStretchFactor(0, 2)  # map : radar = 2 : 1
        splitter.setStretchFactor(1, 1)
        # Set explicit initial sizes so the radar starts at ~33% regardless of sizeHint.
        # These are overridden when the user drags the splitter handle.
        splitter.setSizes([660, 330])
        root.addWidget(splitter, stretch=1)

        # ── Bottom: status bar ──────────────────────────────────────────
        status_bar = QWidget()
        status_bar.setFixedHeight(36)
        status_bar.setStyleSheet("background: #1a1f2e; border-top: 1px solid #2d3250;")
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(8, 2, 8, 2)
        sb_layout.setSpacing(16)

        font = QFont()
        font.setPointSize(9)

        def _lbl(text: str, color: str = "#c8d0e0") -> QLabel:
            lbl = QLabel(text)
            lbl.setFont(font)
            lbl.setStyleSheet(f"color: {color};")
            return lbl

        self._sb_sat = _lbl("—", "#58a6ff")
        self._sb_el = _lbl("EL: —")
        self._sb_az = _lbl("AZ: —")
        range_label = _("Range")
        self._sb_range = _lbl(f"{range_label}: —")
        self._sb_vis = _lbl("—", "#8b949e")
        self._sb_dl = _lbl("DL: —", "#2ecc71")
        self._sb_ul = _lbl("UL: —", "#f1c40f")
        self._sb_mode = _lbl("—", "#8b949e")

        for w in (
            self._sb_sat,
            self._sb_el,
            self._sb_az,
            self._sb_range,
            self._sb_vis,
            self._sb_dl,
            self._sb_ul,
            self._sb_mode,
        ):
            sb_layout.addWidget(w)
        sb_layout.addStretch()

        root.addWidget(status_bar)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_satellite(self, norad: int, name: str) -> None:
        """Set the currently selected satellite (called once on selection)."""
        self._selected_norad = norad
        self._selected_name = name
        self._sb_sat.setText(name)
        # Reset lerp/velocity state so the new satellite snaps to position immediately.
        self._zoom_lat = None
        self._zoom_lon = None
        self._prev_sat_lat = None
        self._prev_sat_lon = None

    def set_transmitter(self, xpdr: dict[str, Any] | None) -> None:
        """Update the active transponder (for frequency display in status bar)."""
        self._current_transmitter = xpdr

    def set_map_image(self, path: str | None) -> None:
        """Apply the same background map image that the main WorldMapView uses."""
        self._local_map.set_map_image(path)

    def set_observer(self, lat: float, lon: float) -> None:
        """Update the observer position on the zoomed map."""
        self._local_map.set_observer_location(lat, lon)

    def update_observation(
        self,
        obs: Observation | None,
        subpoint: tuple[float, float, float] | None = None,
        sat_color: QColor | None = None,
        dl_hz: float | None = None,
        ul_hz: float | None = None,
        dl_doppler: float | None = None,
        ul_doppler: float | None = None,
        track_data: SatTrackData | None = None,
    ) -> None:
        """Refresh all displays from a new Observation.

        Args:
            obs:        Latest satellite observation (None clears displays)
            subpoint:   (lat_deg, lon_deg, alt_km) sub-satellite point
            sat_color:  Colour used for the satellite dot on the map
            dl_hz:      Doppler-corrected downlink frequency (Hz)
            ul_hz:      Doppler-corrected uplink frequency (Hz)
            dl_doppler: Doppler shift on DL (Hz, for display)
            ul_doppler: Doppler shift on UL (Hz)
            track_data: Full SatTrackData including pass track and AOS/LOS times.
                        When provided, the radar shows the same track as the Radar tab.
        """
        if obs is None or self._selected_norad is None:
            self._clear()
            return

        color = sat_color or QColor("#58a6ff")
        is_visible_tab = self.isVisible()

        # ── Zoomed map (skip repaint when tab is hidden to reduce CPU load) ──
        if subpoint is not None and is_visible_tab:
            lat, lon, alt_km = subpoint

            # Velocity-predictive zoom center.
            #
            # Step 1 — estimate velocity from the previous satellite position.
            #   v_lat = lat - prev_lat  (degrees / second at 1 Hz)
            #   v_lon = lon - prev_lon  (with ±180° wrap)
            #
            # Step 2 — compute a predicted target ahead by _ZOOM_LEAD_S seconds.
            #   target = current_pos + velocity * _ZOOM_LEAD_S
            #
            # Step 3 — lerp the zoom center toward that predicted target.
            #   This absorbs any residual SGP4 jitter while keeping the center
            #   ahead of the satellite so the dot stays near the middle of the view.
            #
            # On the very first tick (or after a satellite change) snap immediately.
            if self._zoom_lat is None or self._zoom_lon is None:
                self._zoom_lat, self._zoom_lon = lat, lon
                self._prev_sat_lat, self._prev_sat_lon = lat, lon
            else:
                prev_lat = self._prev_sat_lat if self._prev_sat_lat is not None else lat
                prev_lon = self._prev_sat_lon if self._prev_sat_lon is not None else lon

                # --- velocity estimate (degrees/s, longitude wrapped) ---
                v_lat = lat - prev_lat
                raw_v_lon = lon - prev_lon
                if raw_v_lon > 180.0:
                    raw_v_lon -= 360.0
                elif raw_v_lon < -180.0:
                    raw_v_lon += 360.0
                v_lon = raw_v_lon

                # Guard against stale previous position from hidden tab: if the
                # satellite appears to have moved faster than physically possible
                # (~0.1 deg/s), clamp velocity to zero and snap the zoom center.
                _MAX_V = 0.15  # degrees/s (well above any LEO ground track speed)
                if abs(v_lat) > _MAX_V or abs(v_lon) > _MAX_V:
                    self._zoom_lat, self._zoom_lon = lat, lon
                else:
                    # --- predicted target position ---
                    tgt_lat = lat + v_lat * self._ZOOM_LEAD_S
                    tgt_lon = lon + v_lon * self._ZOOM_LEAD_S
                    # Clamp latitude to [-90, 90]
                    tgt_lat = max(-90.0, min(90.0, tgt_lat))
                    # Wrap longitude to [-180, 180]
                    if tgt_lon > 180.0:
                        tgt_lon -= 360.0
                    elif tgt_lon < -180.0:
                        tgt_lon += 360.0

                    # --- lerp toward predicted target ---
                    d_lon = tgt_lon - self._zoom_lon
                    if d_lon > 180.0:
                        d_lon -= 360.0
                    elif d_lon < -180.0:
                        d_lon += 360.0
                    self._zoom_lat += (tgt_lat - self._zoom_lat) * self._ZOOM_LERP
                    self._zoom_lon += d_lon * self._ZOOM_LERP
                    if self._zoom_lon > 180.0:
                        self._zoom_lon -= 360.0
                    elif self._zoom_lon < -180.0:
                        self._zoom_lon += 360.0

            # Store current position for next tick's velocity estimate.
            # Done here (inside is_visible_tab) so _prev_sat_* always reflects
            # what was used in the last rendered frame, avoiding stale velocity
            # jumps when the tab is hidden and then shown.
            self._prev_sat_lat, self._prev_sat_lon = lat, lon

            self._local_map.set_zoom_region(self._zoom_lat, self._zoom_lon, _ZOOM_SPAN_DEG)
            self._local_map.set_satellites(
                {self._selected_norad: (self._selected_name, lat, lon, color)}
            )
            # Moon has no footprint — skip draw_footprint to avoid drawing a
            # misleadingly large circle using range_km as altitude.
            if self._selected_norad != MOON_ID:
                self._local_map.draw_footprint(self._selected_norad, lat, lon, alt_km)

        # ── Radar (skip repaint when tab is hidden) ────────────────────
        if is_visible_tab:
            if track_data is not None:
                radar_track = track_data
            else:
                radar_track = SatTrackData(
                    name=self._selected_name,
                    norad_cat_id=self._selected_norad,
                    azimuth_deg=obs.azimuth_deg,
                    elevation_deg=obs.elevation_deg,
                    is_visible=obs.is_above_horizon,
                    track=[],
                    aos_time=None,
                    los_time=None,
                )
            self._radar.set_tracks([radar_track])

        # ── Status bar ─────────────────────────────────────────────────
        self._sb_el.setText(f"EL: {obs.elevation_deg:.1f}°")
        self._sb_az.setText(f"AZ: {obs.azimuth_deg:.1f}°")
        range_label = _("Range")
        self._sb_range.setText(f"{range_label}: {obs.range_km:.0f} km")
        if obs.is_above_horizon:
            self._sb_vis.setText(_("Visible ▲"))
            self._sb_vis.setStyleSheet("color: #2ecc71;")
        else:
            self._sb_vis.setText(_("Below horizon"))
            self._sb_vis.setStyleSheet("color: #8b949e;")

        # Frequency
        if dl_hz is not None:
            dop_str = f" ({dl_doppler:+.0f} Hz)" if dl_doppler is not None else ""
            self._sb_dl.setText(f"DL: {dl_hz / 1e6:.6f}{dop_str}")
        else:
            self._sb_dl.setText("DL: —")

        if ul_hz is not None:
            dop_str = f" ({ul_doppler:+.0f} Hz)" if ul_doppler is not None else ""
            self._sb_ul.setText(f"UL: {ul_hz / 1e6:.6f}{dop_str}")
        else:
            self._sb_ul.setText("UL: —")

        if self._current_transmitter:
            mode = str(self._current_transmitter.get("mode") or "")
            self._sb_mode.setText(mode or "—")
        else:
            self._sb_mode.setText("—")

    def clear_satellite(self) -> None:
        """Reset all displays."""
        self._selected_norad = None
        self._selected_name = ""
        self._current_transmitter = None
        self._local_map.clear_zoom()
        self._local_map.set_satellites({})
        self._local_map.clear_footprint()
        self._radar.set_tracks([])
        self._clear()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _clear(self) -> None:
        self._sb_sat.setText("—")
        self._sb_el.setText("EL: —")
        self._sb_az.setText("AZ: —")
        range_label = _("Range")
        self._sb_range.setText(f"{range_label}: —")
        self._sb_vis.setText("—")
        self._sb_vis.setStyleSheet("color: #8b949e;")
        self._sb_dl.setText("DL: —")
        self._sb_ul.setText("UL: —")
        self._sb_mode.setText("—")
