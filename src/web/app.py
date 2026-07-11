"""
FastAPI application.

create_app() receives dependency objects and returns a configured FastAPI instance.
For tests it can be created with an in-memory DB and None engine.

Endpoints:
    GET  /api/satellites                    — satellite list
    GET  /api/favorites                     — favorite satellite list
    POST /api/favorites/{norad}             — add to favorites
    DEL  /api/favorites/{norad}             — remove from favorites
    GET  /api/satellites/{norad}/transmitters — transmitter list
    GET  /api/satellites/{norad}/passes     — pass prediction
    GET  /api/tle/status                    — TLE quality list
    GET  /api/status                        — server status / version
    WS   /ws/tracking?norad=XXXXX          — real-time tracking data
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.engine import PassPredictor, SatelliteEngine
from core.location import Location, LocationManager
from data.tle_manager import TLEManager
from web.rig_state import RigWebState
from web.websocket import ConnectionManager

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_FALLBACKS = [
    Path(__file__).parent / "static",
    # PyInstaller bundle: data collected to web/static relative to _MEIPASS
    *(
        [Path(sys._MEIPASS) / "web" / "static"]  # type: ignore[attr-defined]
        if getattr(sys, "frozen", False)
        else []
    ),
    Path("src/web/static"),
]


def _find_static_dir() -> Path | None:
    for candidate in _STATIC_FALLBACKS:
        if candidate.is_dir():
            return candidate
    return None


logger = logging.getLogger(__name__)

APP_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class SatelliteOut(BaseModel):
    """Satellite basic information response."""

    norad_cat_id: int
    name: str
    alt_names: list[str]
    status: str
    updated_at: str | None


class TransmitterOut(BaseModel):
    """Transmitter/transponder information response."""

    uuid: str
    norad_cat_id: int
    description: str
    type: str | None
    downlink_low: int | None
    downlink_high: int | None
    uplink_low: int | None
    uplink_high: int | None
    mode: str | None
    invert: bool
    baud: int | None
    ctcss_tone: float | None
    ctcss_tone_type: str | None
    alive: bool
    source: str
    manual_override: bool
    notes: str


class PassOut(BaseModel):
    """Pass prediction response (timestamps as ISO 8601 UTC strings)."""

    norad_cat_id: int
    aos: str
    tca: str
    los: str
    max_elevation_deg: float
    max_elevation_time: str  # same as TCA (convenience alias for API consumers)
    aos_azimuth_deg: float
    los_azimuth_deg: float
    duration_s: float
    duration_seconds: float  # same as duration_s (convenience alias for frontend)
    quality: str  # "excellent" | "good" | "fair" | "low"
    track_points: list[dict[str, float]] | None = None  # included only when track=true


class GroupPassOut(BaseModel):
    """Group pass prediction response."""

    norad_cat_id: int
    sat_name: str
    aos: str
    tca: str
    los: str
    max_elevation_deg: float
    aos_azimuth_deg: float
    los_azimuth_deg: float
    duration_seconds: float
    quality: str


class TLEStatusOut(BaseModel):
    """TLE quality information response."""

    norad_cat_id: int
    name: str
    quality_score: str | None
    epoch: str | None
    fetched_at: str | None
    source: str | None


class ServerStatusOut(BaseModel):
    """Server status response."""

    version: str
    status: str
    satellite_count: int
    tle_count: int
    uptime_s: float


class BrowserLocationIn(BaseModel):
    """Location request from the browser Geolocation API."""

    latitude: float
    longitude: float
    accuracy_m: float | None = None
    elevation_m: float = 0.0


class LocationOut(BaseModel):
    """Observer (QTH) location response."""

    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    source: str
    accuracy_m: float | None
    city: str
    country: str
    status_text: str


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def pass_quality(max_elevation_deg: float) -> str:
    """
    Return the quality rank for a pass based on its maximum elevation.

    Returns:
        "excellent" (>=60°) / "good" (>=30°) / "fair" (>=10°) / "low" (<10°)
    """
    if max_elevation_deg >= 60.0:
        return "excellent"
    if max_elevation_deg >= 30.0:
        return "good"
    if max_elevation_deg >= 10.0:
        return "fair"
    return "low"


def _parse_alt_names(raw: Any) -> list[str]:
    """Convert the alt_names column (JSON string or None) to list[str]."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _build_tracking_payload(
    norad: int,
    engine: SatelliteEngine | None,
    rig_state: RigWebState | None = None,
) -> dict[str, Any]:
    """Build the tracking data dict to send over WebSocket."""
    if engine is None:
        return {"norad": norad, "error": "engine not available"}
    obs = engine.observe(norad)
    if obs is None:
        return {"norad": norad, "error": "no TLE data"}
    payload: dict[str, Any] = {
        "norad": norad,
        "timestamp": obs.timestamp.isoformat(),
        "elevation_deg": round(obs.elevation_deg, 4),
        "azimuth_deg": round(obs.azimuth_deg, 4),
        "range_km": round(obs.range_km, 3),
        "range_rate_km_s": round(obs.range_rate_km_s, 6),
        "is_above_horizon": obs.is_above_horizon,
    }
    if rig_state is not None:
        payload["rig"] = {
            "connected": rig_state.rig_connected,
            "engaged": rig_state.rig_engaged,
            "dl_hz": rig_state.dl_hz,
            "ul_hz": rig_state.ul_hz,
            "dl_doppler_hz": rig_state.dl_doppler_hz,
            "ul_doppler_hz": rig_state.ul_doppler_hz,
            "mode": rig_state.mode,
        }
        payload["rot"] = {
            "connected": rig_state.rot_connected,
            "engaged": rig_state.rot_engaged,
            "az": rig_state.rot_az,
            "el": rig_state.rot_el,
        }
    return payload


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _get_amsat_map(db: sqlite3.Connection) -> dict[str, str]:
    """Fetch the AMSAT operational status map from the DB."""
    row = db.execute("SELECT value FROM app_settings WHERE key = 'amsat_status_data'").fetchone()
    if row is None or not row[0]:
        return {}
    try:
        return dict(json.loads(row[0]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _amsat_status(name: str, amsat_map: dict[str, str]) -> str | None:
    """Return the operational status for a satellite from the AMSAT map (word-boundary matching)."""
    lower = name.lower()
    if lower in amsat_map:
        return amsat_map[lower]
    for key, status in amsat_map.items():
        idx = lower.find(key)
        if idx == -1:
            continue
        before_ok = idx == 0 or not lower[idx - 1].isalnum()
        after_idx = idx + len(key)
        after_ok = after_idx >= len(lower) or not lower[after_idx].isalnum()
        if before_ok and after_ok:
            return status
    return None


def _parse_dt_utc(s: str) -> datetime:
    """Convert an ISO 8601 string to a UTC-aware datetime. Naive strings are treated as UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _compute_track_points(
    norad: int,
    aos: datetime,
    los: datetime,
    engine: SatelliteEngine,
    step_s: int = 15,
) -> list[dict[str, float]]:
    """Return elevation/azimuth samples from AOS to LOS at step_s-second intervals."""
    points: list[dict[str, float]] = []
    t = aos
    while t <= los:
        obs = engine.observe(norad, at=t)
        if obs is not None:
            points.append({"az": round(obs.azimuth_deg, 2), "el": round(obs.elevation_deg, 2)})
        t += timedelta(seconds=step_s)
    return points


def _location_to_out(loc: Location, mgr: LocationManager) -> LocationOut:
    """Convert a Location object to a LocationOut response model."""
    return LocationOut(
        latitude_deg=loc.latitude_deg,
        longitude_deg=loc.longitude_deg,
        elevation_m=loc.elevation_m,
        source=loc.source.value,
        accuracy_m=loc.accuracy_m,
        city=loc.city,
        country=loc.country,
        status_text=mgr.status_text,
    )


def create_app(
    conn: sqlite3.Connection,
    tle_manager: TLEManager,
    pass_predictor: PassPredictor | None = None,
    engine: SatelliteEngine | None = None,
    start_time: datetime | None = None,
    location_manager: LocationManager | None = None,
    rig_state: RigWebState | None = None,
) -> FastAPI:
    """
    Create and return a configured FastAPI application.

    Args:
        conn:             SQLite connection (for satellite/transmitter/TLE queries)
        tle_manager:      TLE manager (for quality list retrieval)
        pass_predictor:   Pass predictor. Pass endpoints return empty list when None.
        engine:           Satellite engine. WebSocket returns an error when None.
        start_time:       Uptime calculation base. Defaults to now when None.
        location_manager: Location manager. Location endpoints return 503 when None.

    Returns:
        Configured FastAPI instance
    """
    _start = start_time or datetime.now(UTC)
    manager = ConnectionManager()

    app = FastAPI(
        title="FBSAT59 API",
        version=APP_VERSION,
        description="REST / WebSocket API for the satellite tracking software FBSAT59",
    )

    _static = _find_static_dir()
    if _static is not None:
        app.mount("/static", StaticFiles(directory=str(_static)), name="static")

    # ------------------------------------------------------------------ #
    # Dependency functions (captured by closure)
    # ------------------------------------------------------------------ #

    def get_conn() -> sqlite3.Connection:
        """Return the SQLite connection."""
        return conn

    def get_tle_manager() -> TLEManager:
        """Return the TLEManager."""
        return tle_manager

    # ------------------------------------------------------------------ #
    # REST endpoints
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse, response_model=None)
    async def root() -> HTMLResponse:
        """Return the main page for mobile browsers."""
        static_dir = _find_static_dir()
        if static_dir is not None:
            index_html = static_dir / "index.html"
            if index_html.is_file():
                return HTMLResponse(content=index_html.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>FBSAT59</h1><p>index.html not found</p>", status_code=503)

    @app.get("/api/amsat", response_model=dict[str, str])
    async def get_amsat_status(
        db: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, str]:
        """Return the AMSAT status map as {"lowercase_name": "operational|non_operational"}."""
        row = db.execute(
            "SELECT value FROM app_settings WHERE key = 'amsat_status_data'"
        ).fetchone()
        if row is None:
            return {}
        try:
            return dict(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}

    @app.get("/api/satellites", response_model=list[SatelliteOut])
    async def list_satellites(
        group: str | None = Query(default=None, description="tle_group filter"),
        db: sqlite3.Connection = Depends(get_conn),
    ) -> list[SatelliteOut]:
        """Return the satellite list sorted by name. Filtered by group when specified."""
        if group == "operational":
            amsat_map = _get_amsat_map(db)
            rows = db.execute(
                "SELECT norad_cat_id, name, alt_names, status, updated_at"
                " FROM satellites ORDER BY name"
            ).fetchall()
            return [
                SatelliteOut(
                    norad_cat_id=row["norad_cat_id"],
                    name=row["name"],
                    alt_names=_parse_alt_names(row["alt_names"]),
                    status=row["status"] or "unknown",
                    updated_at=row["updated_at"],
                )
                for row in rows
                if _amsat_status(str(row["name"]), amsat_map) == "operational"
            ]
        if group == "favorites":
            rows = db.execute(
                "SELECT norad_cat_id, name, alt_names, status, updated_at"
                " FROM satellites WHERE is_favorite = 1 ORDER BY name"
            ).fetchall()
        elif group and group != "all":
            rows = db.execute(
                "SELECT s.norad_cat_id, s.name, s.alt_names, s.status, s.updated_at"
                " FROM satellites s"
                " JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id"
                " WHERE t.tle_group = ?"
                " ORDER BY s.name",
                (group,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT norad_cat_id, name, alt_names, status, updated_at"
                " FROM satellites ORDER BY name"
            ).fetchall()
        return [
            SatelliteOut(
                norad_cat_id=row["norad_cat_id"],
                name=row["name"],
                alt_names=_parse_alt_names(row["alt_names"]),
                status=row["status"] or "unknown",
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    @app.get("/api/favorites", response_model=list[SatelliteOut])
    async def list_favorites(
        db: sqlite3.Connection = Depends(get_conn),
    ) -> list[SatelliteOut]:
        """Return the list of favorite satellites (is_favorite=1)."""
        rows = db.execute(
            "SELECT norad_cat_id, name, alt_names, status, updated_at"
            " FROM satellites WHERE is_favorite = 1 ORDER BY name"
        ).fetchall()
        return [
            SatelliteOut(
                norad_cat_id=row["norad_cat_id"],
                name=row["name"],
                alt_names=_parse_alt_names(row["alt_names"]),
                status=row["status"] or "unknown",
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    @app.post("/api/favorites/{norad}", status_code=204, response_model=None)
    async def add_favorite(
        norad: int,
        db: sqlite3.Connection = Depends(get_conn),
    ) -> None:
        """Add the specified satellite to favorites (is_favorite=1). Returns 404 if not found."""
        if (
            db.execute("SELECT 1 FROM satellites WHERE norad_cat_id = ?", (norad,)).fetchone()
            is None
        ):
            raise HTTPException(status_code=404, detail=f"Satellite {norad} not found")
        db.execute("UPDATE satellites SET is_favorite = 1 WHERE norad_cat_id = ?", (norad,))
        db.commit()

    @app.delete("/api/favorites/{norad}", status_code=204, response_model=None)
    async def remove_favorite(
        norad: int,
        db: sqlite3.Connection = Depends(get_conn),
    ) -> None:
        """Remove the satellite from favorites (is_favorite=0). Returns 404 when not found."""
        if (
            db.execute("SELECT 1 FROM satellites WHERE norad_cat_id = ?", (norad,)).fetchone()
            is None
        ):
            raise HTTPException(status_code=404, detail=f"Satellite {norad} not found")
        db.execute("UPDATE satellites SET is_favorite = 0 WHERE norad_cat_id = ?", (norad,))
        db.commit()

    @app.get(
        "/api/satellites/{norad}/transmitters",
        response_model=list[TransmitterOut],
    )
    async def list_transmitters(
        norad: int,
        db: sqlite3.Connection = Depends(get_conn),
    ) -> list[TransmitterOut]:
        """Return the transmitter list for the specified satellite. Returns 404 if not found."""
        if (
            db.execute("SELECT 1 FROM satellites WHERE norad_cat_id = ?", (norad,)).fetchone()
            is None
        ):
            raise HTTPException(status_code=404, detail=f"Satellite {norad} not found")

        rows = db.execute(
            "SELECT * FROM transmitters WHERE norad_cat_id = ? ORDER BY alive DESC, description",
            (norad,),
        ).fetchall()
        return [
            TransmitterOut(
                uuid=row["uuid"],
                norad_cat_id=row["norad_cat_id"],
                description=row["description"],
                type=row["type"],
                downlink_low=row["downlink_low"],
                downlink_high=row["downlink_high"],
                uplink_low=row["uplink_low"],
                uplink_high=row["uplink_high"],
                mode=row["mode"],
                invert=bool(row["invert"]),
                baud=row["baud"],
                ctcss_tone=row["ctcss_tone"],
                ctcss_tone_type=row["ctcss_tone_type"],
                alive=bool(row["alive"]),
                source=row["source"] or "satnogs",
                manual_override=bool(row["manual_override"]),
                notes=row["notes"] or "",
            )
            for row in rows
        ]

    @app.get("/api/satellites/{norad}/passes", response_model=list[PassOut])
    async def get_passes(
        norad: int,
        hours: float = Query(default=24.0, gt=0, le=168, description="Prediction window (hours)"),
        min_el: float = Query(default=5.0, ge=0, le=90, description="Minimum elevation (degrees)"),
        track: bool = Query(default=False, description="Include 15-second track points (az, el)"),
        from_dt: str | None = Query(default=None, description="Start datetime (ISO 8601)"),
        to_dt: str | None = Query(default=None, description="End datetime (ISO 8601)"),
        db: sqlite3.Connection = Depends(get_conn),
    ) -> list[PassOut]:
        """
        Return pass predictions for the specified satellite.

        With track=true each pass includes 15-second elevation/azimuth samples.
        from_dt/to_dt allows a custom time range; defaults to now + hours when omitted.
        Returns an empty list when pass_predictor is None (no engine).
        """
        if (
            db.execute("SELECT 1 FROM satellites WHERE norad_cat_id = ?", (norad,)).fetchone()
            is None
        ):
            raise HTTPException(status_code=404, detail=f"Satellite {norad} not found")

        now = datetime.now(UTC)
        if from_dt is not None or to_dt is not None:
            try:
                start = _parse_dt_utc(from_dt) if from_dt is not None else now
                end = _parse_dt_utc(to_dt) if to_dt is not None else start + timedelta(hours=hours)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Invalid datetime: {exc}") from exc
            if end <= start:
                raise HTTPException(status_code=422, detail="to_dt must be after from_dt")
        else:
            start = now
            end = now + timedelta(hours=hours)

        if pass_predictor is None:
            return []

        passes = pass_predictor.get_passes(norad, start, end, min_elevation_deg=min_el)
        result: list[PassOut] = []
        for p in passes:
            tp = (
                _compute_track_points(norad, p.aos, p.los, engine)
                if track and engine is not None
                else None
            )
            result.append(
                PassOut(
                    norad_cat_id=p.norad_cat_id,
                    aos=p.aos.isoformat(),
                    tca=p.tca.isoformat(),
                    los=p.los.isoformat(),
                    max_elevation_deg=p.max_elevation_deg,
                    max_elevation_time=p.tca.isoformat(),
                    aos_azimuth_deg=p.aos_azimuth_deg,
                    los_azimuth_deg=p.los_azimuth_deg,
                    duration_s=p.duration_s,
                    duration_seconds=p.duration_s,
                    quality=pass_quality(p.max_elevation_deg),
                    track_points=tp,
                )
            )
        return result

    @app.get("/api/passes/group", response_model=list[GroupPassOut])
    async def get_group_passes(
        group: str = Query(default="amateur", description="Group filter"),
        from_dt: str | None = Query(default=None, description="Start datetime (ISO 8601)"),
        to_dt: str | None = Query(default=None, description="End datetime (ISO 8601)"),
        min_el: float = Query(default=5.0, ge=0, le=90, description="Minimum elevation (degrees)"),
        db: sqlite3.Connection = Depends(get_conn),
    ) -> list[GroupPassOut]:
        """Return pass predictions for all group satellites sorted by AOS.

        Returns an empty list when pass_predictor is None.
        """
        now = datetime.now(UTC)
        try:
            start = _parse_dt_utc(from_dt) if from_dt is not None else now
            end = _parse_dt_utc(to_dt) if to_dt is not None else now + timedelta(hours=24)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid datetime: {exc}") from exc
        if end <= start:
            raise HTTPException(status_code=422, detail="to_dt must be after from_dt")

        if pass_predictor is None:
            return []

        if group == "operational":
            amsat_map = _get_amsat_map(db)
            all_sat_rows = db.execute(
                "SELECT s.norad_cat_id, s.name FROM satellites s"
                " JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id"
            ).fetchall()
            sat_rows = [
                r for r in all_sat_rows if _amsat_status(str(r["name"]), amsat_map) == "operational"
            ]
        elif group == "all":
            sat_rows = db.execute(
                "SELECT s.norad_cat_id, s.name FROM satellites s"
                " JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id"
            ).fetchall()
        else:
            sat_rows = db.execute(
                "SELECT s.norad_cat_id, s.name FROM satellites s"
                " JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id"
                " WHERE t.tle_group = ?",
                (group,),
            ).fetchall()

        results: list[GroupPassOut] = []
        for row in sat_rows:
            norad = int(row["norad_cat_id"])
            name = str(row["name"])
            try:
                passes = pass_predictor.get_passes(norad, start, end, min_elevation_deg=min_el)
            except Exception as exc:
                logger.warning("group_passes: skip norad=%d: %s", norad, exc)
                continue
            for p in passes:
                results.append(
                    GroupPassOut(
                        norad_cat_id=norad,
                        sat_name=name,
                        aos=p.aos.isoformat(),
                        tca=p.tca.isoformat(),
                        los=p.los.isoformat(),
                        max_elevation_deg=p.max_elevation_deg,
                        aos_azimuth_deg=p.aos_azimuth_deg,
                        los_azimuth_deg=p.los_azimuth_deg,
                        duration_seconds=p.duration_s,
                        quality=pass_quality(p.max_elevation_deg),
                    )
                )
        results.sort(key=lambda x: x.aos)
        return results

    @app.get("/api/tle/status", response_model=list[TLEStatusOut])
    async def tle_status(
        tm: TLEManager = Depends(get_tle_manager),
    ) -> list[TLEStatusOut]:
        """Return TLE quality for all satellites sorted by quality score ascending (poor first)."""
        rows = tm.get_all_quality_status()
        return [
            TLEStatusOut(
                norad_cat_id=r["norad_cat_id"],
                name=r["name"],
                quality_score=r.get("quality_score"),
                epoch=r.get("epoch"),
                fetched_at=r.get("fetched_at"),
                source=r.get("source"),
            )
            for r in rows
        ]

    @app.get("/api/status", response_model=ServerStatusOut)
    async def server_status(
        db: sqlite3.Connection = Depends(get_conn),
    ) -> ServerStatusOut:
        """Return server status, version, DB record counts, and uptime."""
        sat_count: int = db.execute("SELECT COUNT(*) FROM satellites").fetchone()[0]
        tle_count: int = db.execute("SELECT COUNT(*) FROM tle_data").fetchone()[0]
        return ServerStatusOut(
            version=APP_VERSION,
            status="ok",
            satellite_count=sat_count,
            tle_count=tle_count,
            uptime_s=(datetime.now(UTC) - _start).total_seconds(),
        )

    @app.get("/api/location", response_model=LocationOut)
    async def get_location() -> LocationOut:
        """Return the current observer (QTH) location. Returns 503 if not configured."""
        if location_manager is None:
            raise HTTPException(status_code=503, detail="location manager not configured")
        loc = location_manager.current or location_manager.load_saved()
        if loc is None:
            raise HTTPException(status_code=404, detail="location not set")
        return _location_to_out(loc, location_manager)

    @app.post("/api/location/browser", response_model=LocationOut)
    async def post_browser_location(body: BrowserLocationIn) -> LocationOut:
        """
        Store coordinates received from the browser Geolocation API.

        POST coordinates obtained by navigator.geolocation.getCurrentPosition()
        on the frontend to this endpoint to set the observer location.
        """
        if location_manager is None:
            raise HTTPException(status_code=503, detail="location manager not configured")
        loc = location_manager.set_browser_location(
            latitude_deg=body.latitude,
            longitude_deg=body.longitude,
            accuracy_m=body.accuracy_m,
            elevation_m=body.elevation_m,
        )
        return _location_to_out(loc, location_manager)

    # ------------------------------------------------------------------ #
    # Rig / Rotator control (mobile web UI)
    # ------------------------------------------------------------------ #

    @app.post("/api/rig/toggle", status_code=204, response_model=None)
    async def toggle_rig() -> None:
        """Toggle rig Doppler correction on/off (used by mobile Antenna tab)."""
        if rig_state is None:
            raise HTTPException(status_code=503, detail="rig state not available")
        if not rig_state.rig_connected:
            raise HTTPException(status_code=409, detail="rig not connected")
        rig_state.rig_toggle_requested = True

    @app.post("/api/rot/toggle", status_code=204, response_model=None)
    async def toggle_rot() -> None:
        """Toggle rotator tracking on/off (used by mobile Antenna tab)."""
        if rig_state is None:
            raise HTTPException(status_code=503, detail="rig state not available")
        if not rig_state.rot_connected:
            raise HTTPException(status_code=409, detail="rotator not connected")
        rig_state.rot_toggle_requested = True

    class RigConnectIn(BaseModel):
        """Request body for POST /api/rig/connect."""

        norad: int
        xpdr_uuid: str

    @app.post("/api/rig/connect", status_code=204, response_model=None)
    async def rig_connect(body: RigConnectIn) -> None:
        """Select satellite + transponder and connect rig from mobile UI."""
        if rig_state is None:
            raise HTTPException(status_code=503, detail="rig state not available")
        rig_state.requested_norad = body.norad
        rig_state.requested_xpdr_uuid = body.xpdr_uuid
        rig_state.rig_connect_requested = True

    @app.post("/api/rig/disconnect", status_code=204, response_model=None)
    async def rig_disconnect() -> None:
        """Disconnect rig from mobile UI."""
        if rig_state is None:
            raise HTTPException(status_code=503, detail="rig state not available")
        rig_state.rig_disconnect_requested = True

    # ------------------------------------------------------------------ #
    # WebSocket — /ws/tracking
    # ------------------------------------------------------------------ #

    @app.websocket("/ws/tracking")
    async def ws_tracking(
        websocket: WebSocket,
        norad: int = Query(
            default=25544, description="NORAD catalog number of the satellite to track"
        ),
    ) -> None:
        """
        Satellite tracking WebSocket endpoint.

        Sends elevation, azimuth, range, and range-rate as JSON every second.
        Sends an error payload when the engine is not configured or TLE is missing.

        Example payload::

            {
                "norad": 25544,
                "timestamp": "2026-05-10T12:00:00+00:00",
                "elevation_deg": 45.12,
                "azimuth_deg": 180.34,
                "range_km": 412.5,
                "range_rate_km_s": -2.134567,
                "is_above_horizon": true
            }
        """
        await manager.connect(websocket)
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    payload = _build_tracking_payload(norad, engine, rig_state)
                except Exception as exc:
                    logger.warning("WS: payload build error: %s", exc)
                    payload = {"norad": norad, "error": str(exc)}
                await manager.send_json(websocket, payload)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("WS: unexpected error norad=%d: %s", norad, exc)
        finally:
            await manager.disconnect(websocket)

    return app
