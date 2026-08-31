"""Automatic telemetry frame upload to the SatNOGS DB.

Posts each decoded telemetry frame to ``https://db.satnogs.org/api/telemetry/``
using the SiDS (Simple Downlink Sharing Convention): one form-urlencoded HTTP
POST per frame, authenticated with the user's permanent SatNOGS DB API key
(``Authorization: Token <key>``).

The POSTs run on a single background worker thread draining a queue so they
never block frame decoding or the UI. Failures are logged to ``fbsat59.log``
and dropped -- there is no retry, matching the fire-and-forget style of
``comms.log_broadcast``.

Settings live in ``app_settings`` under the ``satnogs_upload_settings`` key
(JSON: ``{"enabled": bool, "api_key": str}``). The station callsign and
location are read fresh from ``app_settings`` on every submit (keys
``callsign`` and ``observer_location``) so a settings change needs no restart.

Reference implementation: SkyRoof's ``SatnogsUploader.cs`` (VE3NEA/SkyRoof,
GPL-3.0).
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)

# Production SatNOGS DB telemetry endpoint (SiDS). The db-dev staging server
# exists only to test the SatNOGS platform itself and must not be used.
SATNOGS_TELEMETRY_URL = "https://db.satnogs.org/api/telemetry/"

SATNOGS_UPLOAD_SETTINGS_KEY = "satnogs_upload_settings"

_HTTP_TIMEOUT_S = 15.0
# A long pass of a chatty beacon is a few hundred frames; cap the backlog so a
# stalled network cannot grow the queue without bound.
_QUEUE_MAXSIZE = 2000

SatnogsUploadSettings = dict[str, bool | str]

# (api_key, form_fields) -> (http_status, response_body)
PostFn = Callable[[str, dict[str, str]], tuple[int, str]]


# --------------------------------------------------------------------------- #
# Settings (app_settings JSON blob, same pattern as comms.log_broadcast)
# --------------------------------------------------------------------------- #


def load_satnogs_upload_settings(conn: sqlite3.Connection) -> SatnogsUploadSettings:
    """Load SatNOGS upload preferences from app_settings.

    Returns a dict with keys ``enabled`` (bool) and ``api_key`` (str).
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (SATNOGS_UPLOAD_SETTINGS_KEY,),
    ).fetchone()
    settings: SatnogsUploadSettings = {"enabled": False, "api_key": ""}
    if row and row[0]:
        try:
            stored = json.loads(str(row[0]))
            if isinstance(stored, dict):
                settings.update(stored)
        except (ValueError, TypeError):
            pass
    return settings


def save_satnogs_upload_settings(conn: sqlite3.Connection, settings: SatnogsUploadSettings) -> None:
    """Persist SatNOGS upload preferences to app_settings."""
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at)"
        " VALUES (?, ?, CURRENT_TIMESTAMP)",
        (SATNOGS_UPLOAD_SETTINGS_KEY, json.dumps(settings)),
    )
    conn.commit()


def get_station_callsign(conn: sqlite3.Connection) -> str:
    """Return the saved station callsign (trimmed), or '' if unset."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'callsign'").fetchone()
    return str(row[0]).strip() if row and row[0] else ""


def get_station_latlon(conn: sqlite3.Connection) -> tuple[float, float] | None:
    """Return ``(latitude_deg, longitude_deg)`` from the saved observer
    location, or ``None`` if no valid location is stored.
    """
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'observer_location'").fetchone()
    if not row or not row[0]:
        return None
    try:
        data = json.loads(str(row[0]))
        return float(data["latitude_deg"]), float(data["longitude_deg"])
    except (ValueError, TypeError, KeyError):
        return None


# --------------------------------------------------------------------------- #
# SiDS field builder
# --------------------------------------------------------------------------- #


def _client_version() -> str:
    try:
        from fbsat59._version import __version__  # noqa: PLC0415

        return f"FBSAT59 {__version__}"
    except Exception:  # noqa: BLE001
        return "FBSAT59"


def build_submission(
    conn: sqlite3.Connection,
    raw_frame: bytes,
    norad: int,
    received_at: datetime,
) -> tuple[str, dict[str, str]] | None:
    """Build ``(api_key, sids_form_fields)`` for *raw_frame*.

    Returns ``None`` when a prerequisite is missing: upload disabled, no API
    key, no callsign, or no saved location. The API key is returned
    separately from the form so it is never written to a log line.

    *raw_frame* is the frame as received (full AX.25 frame without the FCS);
    it is hex-encoded into the ``frame`` field. *received_at* is coerced to
    UTC for the ``timestamp`` field.
    """
    settings = load_satnogs_upload_settings(conn)
    if not settings.get("enabled"):
        return None
    api_key = str(settings.get("api_key", "")).strip()
    if not api_key:
        return None
    callsign = get_station_callsign(conn)
    if not callsign:
        return None
    latlon = get_station_latlon(conn)
    if latlon is None:
        return None
    lat, lon = latlon

    ts = received_at.astimezone(UTC)
    fields = {
        "noradID": str(norad),
        "source": callsign.upper(),
        "locator": "longLat",
        "longitude": f"{abs(lon):.4f}{'E' if lon >= 0 else 'W'}",
        "latitude": f"{abs(lat):.4f}{'N' if lat >= 0 else 'S'}",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
        "frame": raw_frame.hex(),
        "version": _client_version(),
    }
    return api_key, fields


# --------------------------------------------------------------------------- #
# Uploader
# --------------------------------------------------------------------------- #


def _http_post(api_key: str, fields: dict[str, str]) -> tuple[int, str]:
    resp = httpx.post(
        SATNOGS_TELEMETRY_URL,
        data=fields,
        headers={"Authorization": f"Token {api_key}"},
        timeout=_HTTP_TIMEOUT_S,
    )
    return resp.status_code, resp.text


class SatnogsUploader:
    """Background uploader of decoded telemetry frames to the SatNOGS DB.

    A single instance is shared process-wide via :func:`get_satnogs_uploader`.
    :meth:`submit` is called on the GUI thread from the frame-decode slot; it
    resolves every SiDS field there (cheap ``app_settings`` reads) and hands
    only the finished form plus the API key to the worker thread, which does
    nothing but the POST -- so no SQLite handle is ever touched off-thread.
    """

    def __init__(self, post_fn: PostFn | None = None) -> None:
        self._post: PostFn = post_fn or _http_post
        self._queue: queue.Queue[tuple[str, dict[str, str]] | None] = queue.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._thread = threading.Thread(target=self._run, name="SatnogsUploader", daemon=True)
        self._thread.start()

    def submit(
        self,
        conn: sqlite3.Connection,
        raw_frame: bytes,
        norad: int | None,
        received_at: datetime,
    ) -> bool:
        """Queue *raw_frame* for upload. Returns ``True`` if it was queued.

        No-op (returns ``False``) when *norad* is ``None`` or any upload
        prerequisite is missing.
        """
        if norad is None:
            return False
        built = build_submission(conn, raw_frame, norad, received_at)
        if built is None:
            return False
        try:
            self._queue.put_nowait(built)
            return True
        except queue.Full:
            logger.warning("SatNOGS upload queue full -- dropping frame for NORAD %s", norad)
            return False

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop the worker thread. Used by tests; the process-wide singleton
        relies on the thread being a daemon at interpreter exit.
        """
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            api_key, fields = item
            try:
                status, body = self._post(api_key, fields)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SatNOGS upload failed: %s", exc)
                continue
            if 200 <= status < 300:
                logger.info(
                    "SatNOGS frame uploaded (HTTP %d) for NORAD %s",
                    status,
                    fields.get("noradID", "?"),
                )
            else:
                logger.warning(
                    "SatNOGS upload rejected: HTTP %d %s",
                    status,
                    body.strip()[:300],
                )


_uploader: SatnogsUploader | None = None


def get_satnogs_uploader() -> SatnogsUploader:
    """Return the process-wide :class:`SatnogsUploader` singleton."""
    global _uploader
    if _uploader is None:
        _uploader = SatnogsUploader()
    return _uploader
