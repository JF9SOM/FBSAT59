"""
Satellite tracking core engine

SatelliteEngine   — Real-time elevation, azimuth, range, and velocity calculation using Skyfield
PassPredictor     — AOS/TCA/LOS prediction for a given time window
DopplerCalculator — Doppler correction (supports inverting transponders)

Designed to be thread-safe because it is called from both the Qt UI and the FastAPI WebSocket.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
from skyfield.api import EarthSatellite, Time, load, wgs84

if TYPE_CHECKING:
    from data.tle_manager import TLEManager

# Speed of light in km/s
_C_KM_S: float = 299_792.458


# ---------------------------------------------------------------------------
# Data classes (types for calculation results)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """Satellite observation at a given instant"""

    norad_cat_id: int
    timestamp: datetime  # UTC
    elevation_deg: float  # Elevation angle (degrees)
    azimuth_deg: float  # Azimuth angle (degrees, north=0, east=90)
    range_km: float  # Range (km)
    range_rate_km_s: float  # Line-of-sight velocity (km/s, positive=receding, negative=approaching)
    is_above_horizon: bool  # Whether the satellite is above the horizon


@dataclass(frozen=True)
class ObservationWithSubpoint:
    """An Observation plus the satellite's sub-satellite point, computed
    together from a single propagation (see observe_multi_with_subpoints)."""

    observation: Observation
    subpoint_lat_deg: float
    subpoint_lon_deg: float


@dataclass(frozen=True)
class PassInfo:
    """Information for a single satellite pass"""

    norad_cat_id: int
    aos: datetime  # Acquisition of Signal (UTC)
    tca: datetime  # Time of Closest Approach (UTC)
    los: datetime  # Loss of Signal (UTC)
    max_elevation_deg: float  # Maximum elevation at TCA (degrees)
    aos_azimuth_deg: float  # Azimuth at AOS
    los_azimuth_deg: float  # Azimuth at LOS
    duration_s: float  # Pass duration (seconds)


@dataclass(frozen=True)
class DopplerCorrection:
    """Doppler correction result"""

    downlink_hz: float  # Corrected downlink frequency (Hz)
    uplink_hz: float | None  # Corrected uplink frequency (Hz); None for receive-only
    downlink_shift_hz: float  # Doppler shift amount (Hz)
    uplink_shift_hz: float | None


# ---------------------------------------------------------------------------
# SatelliteEngine
# ---------------------------------------------------------------------------


class SatelliteEngine:
    """
    Skyfield wrapper. Computes real-time satellite observations relative to the ground station.

    EarthSatellite objects are kept in an LRU cache and managed in a thread-safe way.
    All computation methods are read-only and can be safely run concurrently within the GIL,
    but writes to the cache are protected by an explicit lock.
    """

    def __init__(
        self,
        tle_manager: TLEManager,
        latitude_deg: float,
        longitude_deg: float,
        elevation_m: float = 0.0,
    ) -> None:
        """
        Args:
            tle_manager: TLE data source
            latitude_deg: Ground station latitude (degrees, positive north)
            longitude_deg: Ground station longitude (degrees, positive east)
            elevation_m: Ground station elevation (m)
        """
        self._tle_manager = tle_manager
        self._ts = load.timescale()
        self._ground_station = wgs84.latlon(latitude_deg, longitude_deg, elevation_m)

        # Cache of norad_cat_id → EarthSatellite (protected by lock)
        self._sat_cache: dict[int, EarthSatellite] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def observe(
        self,
        norad_cat_id: int,
        at: datetime | None = None,
    ) -> Observation | None:
        """
        Return the current (or at a given time) observation for the specified satellite.

        Args:
            norad_cat_id: NORAD satellite number
            at: Reference time (UTC). Uses current time if None.

        Returns:
            Observation, or None if the TLE does not exist.
        """
        sat = self._get_satellite(norad_cat_id)
        if sat is None:
            return None

        try:
            t = self._to_skyfield_time(at)
            topo = (sat - self._ground_station).at(t)
            alt, az, dist = topo.altaz()
            range_rate = self._calc_range_rate(topo)
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).exception(
                "Skyfield observe() failed for NORAD %s", norad_cat_id
            )
            return None

        return Observation(
            norad_cat_id=norad_cat_id,
            timestamp=t.utc_datetime(),
            elevation_deg=float(alt.degrees),
            azimuth_deg=float(az.degrees),
            range_km=float(dist.km),
            range_rate_km_s=float(range_rate),
            is_above_horizon=float(alt.degrees) > 0.0,
        )

    def observe_multi(
        self,
        norad_cat_ids: list[int],
        at: datetime | None = None,
    ) -> dict[int, Observation]:
        """Fetch observations for multiple satellites at once. IDs with no TLE are skipped."""
        result: dict[int, Observation] = {}
        for norad in norad_cat_ids:
            obs = self.observe(norad, at)
            if obs is not None:
                result[norad] = obs
        return result

    def subpoint(
        self,
        norad_cat_id: int,
        at: datetime | None = None,
    ) -> tuple[float, float] | None:
        """
        Return the satellite's sub-satellite point (latitude, longitude).

        Args:
            norad_cat_id: NORAD satellite number
            at: Reference time (UTC). Uses current time if None.

        Returns:
            (latitude_deg, longitude_deg), or None if the TLE does not exist.
        """
        sat = self._get_satellite(norad_cat_id)
        if sat is None:
            return None
        t = self._to_skyfield_time(at)
        geocentric = sat.at(t)
        sp = wgs84.subpoint_of(geocentric)
        return float(sp.latitude.degrees), float(sp.longitude.degrees)

    def subpoint_with_alt(
        self,
        norad_cat_id: int,
        at: datetime | None = None,
    ) -> tuple[float, float, float] | None:
        """Return the satellite's sub-satellite point (latitude, longitude, altitude km).

        Args:
            norad_cat_id: NORAD satellite number
            at: Reference time (UTC). Uses current time if None.

        Returns:
            (latitude_deg, longitude_deg, altitude_km), or None if the TLE does not exist.
        """
        sat = self._get_satellite(norad_cat_id)
        if sat is None:
            return None
        t = self._to_skyfield_time(at)
        geocentric = sat.at(t)
        sp = wgs84.geographic_position_of(geocentric)
        return (
            float(sp.latitude.degrees),
            float(sp.longitude.degrees),
            float(sp.elevation.km),
        )

    def subpoints(
        self,
        norad_cat_ids: list[int],
        at: datetime | None = None,
    ) -> dict[int, tuple[float, float]]:
        """Fetch sub-satellite points for multiple satellites at once.

        Satellites without TLE are skipped.
        """
        result: dict[int, tuple[float, float]] = {}
        for norad in norad_cat_ids:
            sp = self.subpoint(norad, at)
            if sp is not None:
                result[norad] = sp
        return result

    def observe_multi_with_subpoints(
        self,
        norad_cat_ids: list[int],
        at: datetime | None = None,
    ) -> dict[int, ObservationWithSubpoint]:
        """Like calling subpoints() + observe_multi() together, but computes
        each satellite's position only once instead of twice.

        subpoint() and observe() each independently call sat.at(t) — for a
        caller that wants both (e.g. the world map, which needs subpoints
        for the dots and elevation for autotrack caching), calling them
        separately propagates every satellite's orbit twice for the same
        instant. Profiling _update_world_map() with ~1500 visible
        satellites showed this combined method cuts that loop's wall-clock
        cost by roughly 4x (0.84s -> 0.21s) — the ground station's own
        position is also hoisted out of the per-satellite loop here, since
        it doesn't depend on which satellite is being observed
        (2026-07-10).
        """
        result: dict[int, ObservationWithSubpoint] = {}
        t = self._to_skyfield_time(at)
        ground_at_t = self._ground_station.at(t)
        for norad in norad_cat_ids:
            sat = self._get_satellite(norad)
            if sat is None:
                continue
            try:
                geocentric = sat.at(t)
                sp = wgs84.subpoint_of(geocentric)
                topo = geocentric - ground_at_t
                alt, az, dist = topo.altaz()
                range_rate = self._calc_range_rate(topo)
            except Exception:
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "Skyfield observe_multi_with_subpoints() failed for NORAD %s", norad
                )
                continue
            obs = Observation(
                norad_cat_id=norad,
                timestamp=t.utc_datetime(),
                elevation_deg=float(alt.degrees),
                azimuth_deg=float(az.degrees),
                range_km=float(dist.km),
                range_rate_km_s=float(range_rate),
                is_above_horizon=float(alt.degrees) > 0.0,
            )
            result[norad] = ObservationWithSubpoint(
                observation=obs,
                subpoint_lat_deg=float(sp.latitude.degrees),
                subpoint_lon_deg=float(sp.longitude.degrees),
            )
        return result

    def invalidate_cache(self, norad_cat_id: int | None = None) -> None:
        """Clear the cache after a TLE update. Pass None to clear all entries."""
        with self._cache_lock:
            if norad_cat_id is None:
                self._sat_cache.clear()
            else:
                self._sat_cache.pop(norad_cat_id, None)

    def update_observer(
        self,
        latitude_deg: float,
        longitude_deg: float,
        elevation_m: float = 0.0,
    ) -> None:
        """Update the observer location (call when QTH changes)."""
        with self._cache_lock:
            self._ground_station = wgs84.latlon(latitude_deg, longitude_deg, elevation_m)

    # ------------------------------------------------------------------ #
    # Internal utilities
    # ------------------------------------------------------------------ #

    def _get_satellite(self, norad_cat_id: int) -> EarthSatellite | None:
        with self._cache_lock:
            if norad_cat_id in self._sat_cache:
                return self._sat_cache[norad_cat_id]

        try:
            sat = self._tle_manager.get_earth_satellite(norad_cat_id)
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).exception(
                "Failed to build EarthSatellite for NORAD %s", norad_cat_id
            )
            return None

        if sat is None:
            return None

        with self._cache_lock:
            self._sat_cache[norad_cat_id] = sat
        return sat

    def _to_skyfield_time(self, dt: datetime | None) -> Time:
        if dt is None:
            return self._ts.now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return self._ts.from_datetime(dt)

    @staticmethod
    def _calc_range_rate(topo: Any) -> float:
        """Compute line-of-sight velocity (km/s). Positive = receding, negative = approaching."""
        pos = topo.position.km
        vel = topo.velocity.km_per_s
        range_km = float(np.linalg.norm(pos))
        if range_km < 1e-9:
            return 0.0
        return float(np.dot(pos, vel) / range_km)


# ---------------------------------------------------------------------------
# PassPredictor
# ---------------------------------------------------------------------------


class PassPredictor:
    """
    Predicts AOS/TCA/LOS within a given time window.

    Uses Skyfield's find_events() and groups events into individual passes.
    Thread-safe (no methods that mutate internal state).
    """

    def __init__(
        self,
        tle_manager: TLEManager,
        latitude_deg: float,
        longitude_deg: float,
        elevation_m: float = 0.0,
    ) -> None:
        self._tle_manager = tle_manager
        self._ts = load.timescale()
        self._ground_station = wgs84.latlon(latitude_deg, longitude_deg, elevation_m)
        self._engine = SatelliteEngine(tle_manager, latitude_deg, longitude_deg, elevation_m)

    def update_observer(
        self,
        latitude_deg: float,
        longitude_deg: float,
        elevation_m: float = 0.0,
    ) -> None:
        """Update the observer location (call when QTH changes)."""
        self._ground_station = wgs84.latlon(latitude_deg, longitude_deg, elevation_m)
        self._engine.update_observer(latitude_deg, longitude_deg, elevation_m)

    def get_passes(
        self,
        norad_cat_id: int,
        start: datetime,
        end: datetime,
        min_elevation_deg: float = 5.0,
    ) -> list[PassInfo]:
        """
        Return the list of passes within the given time window.

        Args:
            norad_cat_id: NORAD satellite number
            start: Search start time (UTC)
            end: Search end time (UTC)
            min_elevation_deg: Only return passes above this elevation (default 5 degrees)

        Returns:
            List of PassInfo sorted by AOS ascending. Empty list if TLE does not exist.
        """
        sat = self._engine._get_satellite(norad_cat_id)
        if sat is None:
            return []

        t0 = self._to_skyfield_time(start)
        t1 = self._to_skyfield_time(end)

        try:
            times, events = sat.find_events(
                self._ground_station,
                t0,
                t1,
                altitude_degrees=min_elevation_deg,
            )
        except Exception:
            return []

        return self._group_events(norad_cat_id, sat, times, events)

    def get_current_pass(
        self,
        norad_cat_id: int,
        now: datetime | None = None,
    ) -> PassInfo | None:
        """Return the ongoing pass if the satellite is currently above the horizon.

        get_passes(start=now) misses the current pass when AOS already occurred
        before now — Skyfield's find_events() only emits future events, so
        _group_events() discards the incomplete TCA/LOS-only triplet.

        This method searches from 3 hours before now so that the real AOS is
        always inside the search window (typical LEO passes are < 20 minutes).

        Args:
            norad_cat_id: NORAD satellite number
            now: Reference time (UTC); defaults to datetime.now(UTC)

        Returns:
            PassInfo whose aos <= now <= los, or None if not currently in a pass.
        """
        if now is None:
            now = datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        obs = self._engine.observe(norad_cat_id)
        if obs is None or not obs.is_above_horizon:
            return None

        # 3-hour back-search is far more than any LEO pass duration
        search_start = now - timedelta(hours=3)
        search_end = now + timedelta(hours=3)
        passes = self.get_passes(
            norad_cat_id,
            search_start,
            search_end,
            min_elevation_deg=0.0,
        )
        for p in passes:
            if p.aos <= now <= p.los:
                return p
        return None

    # ------------------------------------------------------------------ #
    # Internal processing
    # ------------------------------------------------------------------ #

    def _group_events(
        self,
        norad_cat_id: int,
        sat: EarthSatellite,
        times: object,
        events: object,
    ) -> list[PassInfo]:
        """Group a sequence of AOS(0)/TCA(1)/LOS(2) events into individual passes."""
        passes: list[PassInfo] = []
        # Skyfield guarantees events are ordered AOS→TCA→LOS
        # However the sequence may start with TCA/LOS if the satellite is already
        # visible at the start of the search window.
        pending: dict[str, object] = {}

        times_list = list(times)  # type: ignore[call-overload]
        events_list = list(events)  # type: ignore[call-overload]

        for t, ev in zip(times_list, events_list, strict=False):
            if ev == 0:  # AOS
                pending = {"aos": t}
            elif ev == 1 and "aos" in pending:  # TCA
                pending["tca"] = t
            elif ev == 2 and "tca" in pending:  # LOS — one pass complete
                pending["los"] = t
                info = self._build_pass_info(norad_cat_id, sat, pending)
                if info is not None:
                    passes.append(info)
                pending = {}

        return passes

    def _build_pass_info(
        self,
        norad_cat_id: int,
        sat: EarthSatellite,
        ev: dict[str, Any],
    ) -> PassInfo | None:
        try:
            aos_t = ev["aos"]
            tca_t = ev["tca"]
            los_t = ev["los"]

            topo_aos = (sat - self._ground_station).at(aos_t)
            topo_tca = (sat - self._ground_station).at(tca_t)
            topo_los = (sat - self._ground_station).at(los_t)

            alt_tca, _, _ = topo_tca.altaz()
            _, az_aos, _ = topo_aos.altaz()
            _, az_los, _ = topo_los.altaz()

            aos_dt: datetime = aos_t.utc_datetime()
            tca_dt: datetime = tca_t.utc_datetime()
            los_dt: datetime = los_t.utc_datetime()

            return PassInfo(
                norad_cat_id=norad_cat_id,
                aos=aos_dt,
                tca=tca_dt,
                los=los_dt,
                max_elevation_deg=float(alt_tca.degrees),
                aos_azimuth_deg=float(az_aos.degrees),
                los_azimuth_deg=float(az_los.degrees),
                duration_s=(los_dt - aos_dt).total_seconds(),
            )
        except Exception:
            return None

    def _to_skyfield_time(self, dt: datetime) -> Time:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return self._ts.from_datetime(dt)


# ---------------------------------------------------------------------------
# DopplerCalculator
# ---------------------------------------------------------------------------


class DopplerCalculator:
    """
    Doppler shift calculator.

    Computes frequency corrections from the satellite's line-of-sight velocity (range_rate_km_s).
    The uplink correction is always in the opposite direction to the downlink one, for
    inverting and non-inverting transponders alike (see correct_uplink()).

    Physical model:
        f_received = f_nominal * (1 - range_rate / c)
        shift_hz   = -f_nominal * range_rate / c
        (range_rate > 0 = receding → received frequency lower than nominal = shift < 0)
    """

    @staticmethod
    def shift_hz(nominal_hz: float, range_rate_km_s: float) -> float:
        """
        Return the Doppler shift amount (Hz).

        Positive = received frequency higher than nominal (approaching)
        Negative = received frequency lower than nominal (receding)
        """
        return -nominal_hz * range_rate_km_s / _C_KM_S

    @staticmethod
    def correct_downlink(
        downlink_hz: float,
        range_rate_km_s: float,
    ) -> tuple[float, float]:
        """
        Correct the downlink frequency.

        Returns:
            (corrected frequency Hz, shift amount Hz)
        """
        shift = DopplerCalculator.shift_hz(downlink_hz, range_rate_km_s)
        return downlink_hz + shift, shift

    @staticmethod
    def correct_uplink(
        uplink_hz: float,
        range_rate_km_s: float,
    ) -> tuple[float, float]:
        """
        Correct the uplink frequency.

        Physical model:
            Transmit higher when the satellite recedes (and lower when it
            approaches) so the signal arrives at the satellite's receiver on
            the nominal frequency.  The UL correction is therefore always in
            the OPPOSITE direction to the DL correction.

        This does NOT depend on whether the transponder inverts.  Uplink
        Doppler pre-compensation is purely about what the satellite's
        *receiver* hears; inversion only affects which downlink frequency
        results and which sideband to use (the latter is handled separately
        by _MODE_INVERT in ui/main_window.py).  There used to be an `invert`
        argument here that flipped the sign to match the DL direction -- that
        was physically wrong and left an operator's own signal drifting
        across the passband by 2 x uplink_freq x (range_rate/c): about
        +-5.8 kHz on a V/U bird and +-17.4 kHz on a U/V one over a pass.
        SSB/CW operators never reported it because they retune by ear
        continuously; it only became obvious with FT4, where nothing retunes
        (GitHub Issue #16, confirmed against a reporter's IC-9700 log).

        Args:
            uplink_hz: Nominal uplink frequency (Hz)
            range_rate_km_s: Line-of-sight velocity (km/s, positive = receding)

        Returns:
            (corrected frequency Hz, shift amount Hz)
        """
        # Opposite direction to the DL correction -- see docstring.
        shift = -DopplerCalculator.shift_hz(uplink_hz, range_rate_km_s)
        return uplink_hz + shift, shift

    @classmethod
    def correct_transponder(
        cls,
        downlink_hz: float,
        uplink_hz: float | None,
        range_rate_km_s: float,
    ) -> DopplerCorrection:
        """
        Simultaneously correct both the downlink and uplink frequencies of a transponder.

        Takes no `invert` argument: Doppler correction is identical for
        inverting and non-inverting transponders (see correct_uplink()).

        Args:
            downlink_hz: Nominal downlink frequency (Hz)
            uplink_hz: Nominal uplink frequency (Hz). None for receive-only.
            range_rate_km_s: Line-of-sight velocity (km/s)

        Returns:
            DopplerCorrection
        """
        dl_corrected, dl_shift = cls.correct_downlink(downlink_hz, range_rate_km_s)

        ul_corrected: float | None = None
        ul_shift: float | None = None
        if uplink_hz is not None:
            ul_corrected, ul_shift = cls.correct_uplink(uplink_hz, range_rate_km_s)

        return DopplerCorrection(
            downlink_hz=dl_corrected,
            uplink_hz=ul_corrected,
            downlink_shift_hz=dl_shift,
            uplink_shift_hz=ul_shift,
        )
