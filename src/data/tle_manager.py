"""
TLE (Two-Line Element) automatic update manager

Fetches TLEs from multiple sources (CelesTrak, Space-Track, AMSAT),
applies quality scoring, and saves them to SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from skyfield.api import EarthSatellite, load

logger = logging.getLogger(__name__)

# TLE source definitions (in priority order)
# CelesTrak GP API: https://celestrak.org/NORAD/documentation/gp-data-formats.php
TLE_SOURCES: list[dict[str, Any]] = [
    {
        "name": "celestrak-stations",
        "url": "https://celestrak.org/NORAD/elements/gp.php",
        "params": {"GROUP": "STATIONS", "FORMAT": "TLE"},
        "group": "stations",
        "priority": 0,
        "update_interval_hours": 1,
    },
    {
        "name": "celestrak-amateur",
        "url": "https://celestrak.org/NORAD/elements/gp.php",
        "params": {"GROUP": "AMATEUR", "FORMAT": "TLE"},
        "group": "amateur",
        "priority": 1,
        "update_interval_hours": 2,
    },
    {
        "name": "celestrak-cubesat",
        "url": "https://celestrak.org/NORAD/elements/gp.php",
        "params": {"GROUP": "CUBESAT", "FORMAT": "TLE"},
        "group": "cubesat",
        "priority": 2,
        "update_interval_hours": 4,
    },
    {
        "name": "celestrak-weather",
        "url": "https://celestrak.org/NORAD/elements/gp.php",
        "params": {"GROUP": "WEATHER", "FORMAT": "TLE"},
        "group": "weather",
        "priority": 3,
        "update_interval_hours": 6,
    },
    {
        "name": "celestrak-earth-obs",
        "url": "https://celestrak.org/NORAD/elements/gp.php",
        "params": {"GROUP": "resource", "FORMAT": "TLE"},
        "group": "earth-obs",
        "priority": 4,
        "update_interval_hours": 12,
    },
    {
        "name": "celestrak-science",
        "url": "https://celestrak.org/NORAD/elements/gp.php",
        "params": {"GROUP": "SCIENCE", "FORMAT": "TLE"},
        "group": "science",
        "priority": 5,
        "update_interval_hours": 12,
    },
]


_SOURCE_DB_VALUE: dict[str, str] = {
    "celestrak-stations": "celestrak",
    "celestrak-amateur": "celestrak",
    "celestrak-cubesat": "celestrak",
    "celestrak-weather": "celestrak",
    "celestrak-earth-obs": "celestrak",
    "celestrak-science": "celestrak",
    "celestrak-single": "celestrak",
    "satnogs-provisional": "satnogs",
}

# SATNOGS TLE API endpoint for per-satellite lookup
SATNOGS_TLE_URL = "https://db.satnogs.org/api/tle/"


def _to_db_source(source_name: str) -> str:
    """Convert a source name to a value that satisfies the DB CHECK constraint"""
    return _SOURCE_DB_VALUE.get(source_name, source_name)


def _calc_quality(epoch_dt: datetime) -> str:
    """Return the quality score based on elapsed time since the TLE epoch"""
    age = (
        datetime.now(UTC) - epoch_dt.replace(tzinfo=UTC)
        if epoch_dt.tzinfo is None
        else datetime.now(UTC) - epoch_dt
    )
    hours = age.total_seconds() / 3600
    if hours < 6:
        return "excellent"
    elif hours < 24:
        return "good"
    elif hours < 72:
        return "fair"
    return "poor"


class TLEManager:
    """
    Class responsible for fetching, saving, and quality-managing TLEs.
    Falls back to the cache when offline.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._ts = load.timescale()

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #

    def get_tle(self, norad_cat_id: int) -> dict[str, Any] | None:
        """Retrieve TLE data for a satellite from the DB"""
        row = self._conn.execute(
            "SELECT * FROM tle_data WHERE norad_cat_id = ?",
            (norad_cat_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_earth_satellite(self, norad_cat_id: int) -> EarthSatellite | None:
        """Return an EarthSatellite object usable with Skyfield"""
        tle = self.get_tle(norad_cat_id)
        if not tle:
            return None
        return EarthSatellite(tle["line1"], tle["line2"], tle["name"], self._ts)

    def get_all_quality_status(self) -> list[dict[str, Any]]:
        """Return the TLE quality status list for all satellites"""
        rows = self._conn.execute("""
            SELECT s.norad_cat_id, s.name, t.quality_score,
                   t.fetched_at, t.epoch, t.source
            FROM satellites s
            LEFT JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id
            ORDER BY t.quality_score ASC NULLS FIRST
        """).fetchall()
        return [dict(r) for r in rows]

    def needs_update(self, norad_cat_id: int, max_age_hours: float = 4.0) -> bool:
        """Determine whether the TLE needs to be updated"""
        row = self._conn.execute(
            "SELECT fetched_at FROM tle_data WHERE norad_cat_id = ?",
            (norad_cat_id,),
        ).fetchone()
        if not row:
            return True
        fetched = datetime.fromisoformat(row["fetched_at"])
        return datetime.now(UTC) - fetched > timedelta(hours=max_age_hours)

    # ------------------------------------------------------------------ #
    # Update
    # ------------------------------------------------------------------ #

    async def fetch_and_update(
        self,
        source_name: str = "celestrak-amateur",
        progress_callback: Any = None,
    ) -> dict[str, int]:
        """
        Fetch TLEs from the specified source and update the DB.
        Returns: {"inserted": N, "updated": N, "errors": N}
        """
        source = next((s for s in TLE_SOURCES if s["name"] == source_name), TLE_SOURCES[0])
        tle_group = str(source.get("group", "amateur"))
        stats = {"inserted": 0, "updated": 0, "errors": 0}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(source["url"], params=source.get("params", {}))
                r.raise_for_status()
                text = r.text
        except httpx.HTTPError as e:
            logger.warning(f"fetch error from {source_name}: {e}")
            stats["errors"] = 1
            return stats

        # Parse TLE text format (3-line groups)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        tle_triples = []
        i = 0
        while i < len(lines) - 2:
            if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
                tle_triples.append((lines[i], lines[i + 1], lines[i + 2]))
                i += 3
            else:
                i += 1

        now = datetime.now(UTC).isoformat()
        db_source = _to_db_source(source_name)
        for idx, (name, line1, line2) in enumerate(tle_triples):
            if progress_callback:
                progress_callback(idx + 1, len(tle_triples))

            try:
                sat = EarthSatellite(line1, line2, name, self._ts)
                norad = int(line1[2:7])
                epoch_dt = sat.epoch.utc_datetime()
                quality = _calc_quality(epoch_dt)

                # Ensure the satellite record exists
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO satellites (norad_cat_id, name, updated_at)
                    VALUES (?, ?, ?)
                """,
                    (norad, name, now),
                )

                existing = self._conn.execute(
                    "SELECT norad_cat_id FROM tle_data WHERE norad_cat_id = ?",
                    (norad,),
                ).fetchone()

                # Append to history
                self._conn.execute(
                    """
                    INSERT INTO tle_history (norad_cat_id, name, line1, line2, epoch, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (norad, name, line1, line2, epoch_dt.isoformat(), source_name),
                )

                if existing:
                    # Only overwrite tle_group if the existing value is 'amateur'
                    # (the default/generic group).  This prevents a later amateur-group
                    # fetch from reverting a satellite that was correctly classified as
                    # 'cubesat', 'weather', etc. back to 'amateur'.
                    self._conn.execute(
                        """
                        UPDATE tle_data SET
                            name=?, line1=?, line2=?, epoch=?,
                            source=?,
                            tle_group = CASE WHEN tle_group = 'amateur' THEN ? ELSE tle_group END,
                            fetched_at=?, quality_score=?
                        WHERE norad_cat_id=?
                    """,
                        (
                            name,
                            line1,
                            line2,
                            epoch_dt.isoformat(),
                            db_source,
                            tle_group,
                            now,
                            quality,
                            norad,
                        ),
                    )
                    stats["updated"] += 1
                else:
                    self._conn.execute(
                        """
                        INSERT INTO tle_data
                            (norad_cat_id, name, line1, line2, epoch,
                             source, tle_group, fetched_at, quality_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            norad,
                            name,
                            line1,
                            line2,
                            epoch_dt.isoformat(),
                            db_source,
                            tle_group,
                            now,
                            quality,
                        ),
                    )
                    stats["inserted"] += 1

            except Exception as e:
                logger.warning(f"parse error for {name}: {e}")
                stats["errors"] += 1

        self._conn.commit()
        self._log_sync(source_name, stats)
        return stats

    async def fetch_single(self, norad_cat_id: int) -> bool:
        """Fetch the TLE for a single satellite from CelesTrak and add it to the DB.

        Use this when a satellite is not included in a group fetch
        (e.g. ORIGAMI-2 / NORAD 57168) and needs to be added individually.
        """
        url = "https://celestrak.org/NORAD/elements/gp.php"
        params = {"CATNR": str(norad_cat_id), "FORMAT": "TLE"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
                if len(lines) >= 3:
                    name, line1, line2 = lines[0], lines[1], lines[2]
                    return self.add_manual_tle(norad_cat_id, name, line1, line2)
        except httpx.HTTPError as e:
            logger.warning(f"fetch_single error: {e}")
        return False

    def add_manual_tle(
        self,
        norad_cat_id: int,
        name: str,
        line1: str,
        line2: str,
    ) -> bool:
        """Manually add or update a TLE (e.g. when entered via the GUI)"""
        try:
            sat = EarthSatellite(line1, line2, name, self._ts)
            epoch_dt = sat.epoch.utc_datetime()
            quality = _calc_quality(epoch_dt)
            now = datetime.now(UTC).isoformat()

            self._conn.execute(
                """
                INSERT OR IGNORE INTO satellites (norad_cat_id, name, updated_at)
                VALUES (?, ?, ?)
            """,
                (norad_cat_id, name, now),
            )

            self._conn.execute(
                """
                INSERT OR REPLACE INTO tle_data
                    (norad_cat_id, name, line1, line2, epoch,
                     source, fetched_at, quality_score)
                VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)
            """,
                (norad_cat_id, name, line1, line2, epoch_dt.isoformat(), now, quality),
            )
            self._conn.commit()
            return True
        except Exception as e:
            logger.warning(f"invalid TLE: {e}")
            return False

    def is_active_tle_stale(self, max_age_hours: float = 24.0) -> bool:
        """Return True if the celestrak-active TLE fetch is older than max_age_hours."""
        row = self._conn.execute(
            "SELECT finished_at FROM sync_log WHERE sync_type = 'celestrak-active'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return True
        last = datetime.fromisoformat(str(row["finished_at"]))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return datetime.now(UTC) - last > timedelta(hours=max_age_hours)

    def is_source_stale(self, source_name: str) -> bool:
        """Return True if the given TLE source has never been fetched (no sync_log entry).

        Unlike is_active_tle_stale(), this only returns True when there is no record at all
        (i.e. first run after a fresh install).  The APScheduler interval jobs handle
        subsequent periodic refreshes, so we only need to detect the very first-run case.
        """
        row = self._conn.execute(
            "SELECT finished_at FROM sync_log WHERE sync_type = ? ORDER BY id DESC LIMIT 1",
            (source_name,),
        ).fetchone()
        return row is None

    def is_group_empty(self, source_name: str) -> bool:
        """Return True if the tle_group associated with source_name is suspiciously sparse.

        This detects two failure cases:
        1. Upgrade case: a previous beta overwrote all tle_group values back to 'amateur'
           (so sync_log has an entry but the group still has 0 satellites).
        2. Failed fetch case: a previous fetch attempt failed mid-way (e.g. due to
           database lock from concurrent instances), leaving 0 or very few satellites.

        We use a minimum threshold rather than strict 0 to catch partially-failed fetches:
        - cubesat: expects hundreds of satellites; < 5 means the fetch failed
        - weather / earth-obs / science: expects dozens; < 5 means failed
        - stations: expects ~10; < 3 means failed
        Only applies to group-specific sources (not 'celestrak-amateur').
        """
        source = next((s for s in TLE_SOURCES if s["name"] == source_name), None)
        if source is None:
            return False
        tle_group = str(source.get("group", "amateur"))
        # Amateur is the catch-all default; a low count there doesn't indicate a problem.
        if tle_group == "amateur":
            return False
        # Minimum expected satellites per group; anything below triggers a re-fetch.
        min_expected = 3 if tle_group == "stations" else 5
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM tle_data WHERE tle_group = ?",
            (tle_group,),
        ).fetchone()
        return (row["cnt"] if row else 0) < min_expected

    async def fetch_active_tles(self) -> dict[str, int]:
        """Fill TLE gaps for SATNOGS-registered satellites (NORAD 10000-89999).

        Two-phase approach:
          Phase 1 — CelesTrak bulk groups (single request per group, fast):
            Downloads several CelesTrak groups that collectively cover most
            SATNOGS-registered satellites not already handled by the targeted
            group fetches (amateur, cubesat, etc.).
            Note: CelesTrak GROUP=active currently returns 403, so we use the
            available subsets instead.

          Phase 2 — SATNOGS TLE API fallback (individual requests, concurrent):
            For each satellite in NORAD 10000-89999 that still has no TLE after
            phase 1, queries the SATNOGS TLE API (/api/tle/?norad_cat_id=X).
            Requests are issued concurrently (up to 20 at a time) to limit
            total run time to a few minutes.
            Same 30-day grace-period / auto-hide logic as fetch_provisional_tles().

        New satellite records are never created; only existing satellites are updated.
        Manual TLEs are never overwritten.
        Existing tle_group values are preserved on UPDATE.

        Returns:
            {"inserted": N, "updated": N, "no_tle": N,
             "hidden_unknown": N, "hidden_expired": N, "errors": N}
        """
        import asyncio as _asyncio  # noqa: PLC0415

        # CelesTrak groups accessible without authentication that provide good coverage
        # of SATNOGS-registered satellites (GROUP=active returns 403).
        _BULK_GROUPS = [
            "satnogs",  # ~664 satellites tracked by SatNOGS network
            "last-30-days",  # recently launched satellites
            "argos",  # data-collection / beacon satellites
            "orbcomm",  # low-orbit messaging
            "spire",  # commercial CubeSat constellation
        ]
        stats: dict[str, int] = {
            "inserted": 0,
            "updated": 0,
            "no_tle": 0,
            "hidden_unknown": 0,
            "hidden_expired": 0,
            "errors": 0,
        }
        now = datetime.now(UTC).isoformat()

        # Visible satellites we care about (NORAD 10000-89999, excludes provisional)
        wanted: set[int] = {
            int(r["norad_cat_id"])
            for r in self._conn.execute(
                "SELECT norad_cat_id FROM satellites"
                " WHERE is_hidden = 0"
                "   AND norad_cat_id BETWEEN 10000 AND 89999"
            ).fetchall()
        }
        # Current TLE map: {norad: (source, tle_group)}
        existing_tles: dict[int, tuple[str, str]] = {
            int(r["norad_cat_id"]): (
                str(r["source"] or ""),
                str(r["tle_group"] or "amateur"),
            )
            for r in self._conn.execute(
                "SELECT norad_cat_id, source, tle_group FROM tle_data"
            ).fetchall()
        }

        # ── Phase 1: CelesTrak bulk group fetches ────────────────────────────
        def _process_tle_text(text: str, source_label: str) -> None:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            i = 0
            while i < len(lines) - 2:
                if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
                    name, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
                    i += 3
                    try:
                        norad = int(line1[2:7])
                    except (ValueError, IndexError):
                        continue
                    if norad not in wanted:
                        continue
                    ex_src, _ex_grp = existing_tles.get(norad, ("", ""))
                    if ex_src == "manual":
                        continue
                    try:
                        sat_obj = EarthSatellite(line1, line2, name, self._ts)
                        epoch_dt = sat_obj.epoch.utc_datetime()
                        quality = _calc_quality(epoch_dt)
                    except Exception:
                        stats["errors"] += 1
                        i += 1
                        continue
                    self._conn.execute(
                        "INSERT INTO tle_history"
                        " (norad_cat_id, name, line1, line2, epoch, source)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (norad, name, line1, line2, epoch_dt.isoformat(), source_label),
                    )
                    if ex_src:
                        self._conn.execute(
                            "UPDATE tle_data SET"
                            " name=?, line1=?, line2=?, epoch=?,"
                            " source='celestrak', fetched_at=?, quality_score=?"
                            " WHERE norad_cat_id=?",
                            (name, line1, line2, epoch_dt.isoformat(), now, quality, norad),
                        )
                        stats["updated"] += 1
                    else:
                        self._conn.execute(
                            "INSERT INTO tle_data"
                            " (norad_cat_id, name, line1, line2, epoch,"
                            "  source, tle_group, fetched_at, quality_score)"
                            " VALUES (?, ?, ?, ?, ?, 'celestrak', 'amateur', ?, ?)",
                            (norad, name, line1, line2, epoch_dt.isoformat(), now, quality),
                        )
                        self._conn.execute(
                            "UPDATE satellites SET tle_no_result_since = NULL"
                            " WHERE norad_cat_id = ?",
                            (norad,),
                        )
                        existing_tles[norad] = ("celestrak", "amateur")
                        stats["inserted"] += 1
                else:
                    i += 1

        url_ct = "https://celestrak.org/NORAD/elements/gp.php"
        async with httpx.AsyncClient(timeout=60.0) as client:
            for group in _BULK_GROUPS:
                try:
                    r = await client.get(url_ct, params={"GROUP": group, "FORMAT": "TLE"})
                    r.raise_for_status()
                    _process_tle_text(r.text, f"celestrak-{group}")
                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                    stats["errors"] += 1
                except httpx.HTTPError as exc:
                    logger.warning(f"active fetch error ({group}): {exc}")
                    stats["errors"] += 1

        self._conn.commit()

        # ── Phase 2: SATNOGS TLE API fallback for still-missing satellites ───
        still_missing = [
            (
                int(r["norad_cat_id"]),
                str(r["name"]),
                str(r["status"] or "unknown"),
                str(r["tle_no_result_since"]) if r["tle_no_result_since"] else None,
            )
            for r in self._conn.execute(
                """
                SELECT s.norad_cat_id, s.name, s.status, s.tle_no_result_since
                FROM satellites s
                LEFT JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id
                WHERE s.is_hidden = 0
                  AND s.norad_cat_id BETWEEN 10000 AND 89999
                  AND t.norad_cat_id IS NULL
                """
            ).fetchall()
        ]

        if still_missing:
            semaphore = _asyncio.Semaphore(20)  # max 20 concurrent requests

            async def _fetch_one(
                norad: int, sat_name: str, sat_status: str, no_result_since: str | None
            ) -> None:
                async with semaphore:
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as c:
                            resp = await c.get(
                                SATNOGS_TLE_URL,
                                params={"norad_cat_id": norad, "format": "json"},
                            )
                            resp.raise_for_status()
                            data = resp.json()
                    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                        stats["errors"] += 1
                        return
                    except Exception as exc:
                        logger.warning(f"SATNOGS TLE fallback error {norad}: {exc}")
                        stats["errors"] += 1
                        return

                    if isinstance(data, list):
                        data = data[0] if data else {}
                    if not isinstance(data, dict) or "tle1" not in data:
                        # No TLE available — apply grace-period / hide logic.
                        # 'dead' satellites are treated like 'unknown': hide immediately.
                        if sat_status in ("unknown", "dead"):
                            self._conn.execute(
                                "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                                " WHERE norad_cat_id = ?",
                                (now, norad),
                            )
                            stats["hidden_unknown"] += 1
                        else:
                            if no_result_since is None:
                                self._conn.execute(
                                    "UPDATE satellites"
                                    " SET tle_no_result_since = ?, updated_at = ?"
                                    " WHERE norad_cat_id = ?",
                                    (now, now, norad),
                                )
                            else:
                                since_dt = datetime.fromisoformat(no_result_since)
                                if since_dt.tzinfo is None:
                                    since_dt = since_dt.replace(tzinfo=UTC)
                                if datetime.now(UTC) - since_dt > timedelta(days=30):
                                    self._conn.execute(
                                        "UPDATE satellites"
                                        " SET is_hidden = 2, updated_at = ?"
                                        " WHERE norad_cat_id = ?",
                                        (now, norad),
                                    )
                                    stats["hidden_expired"] += 1
                        stats["no_tle"] += 1
                        return

                    line1: str = str(data["tle1"])
                    line2: str = str(data["tle2"])
                    try:
                        sat_obj = EarthSatellite(line1, line2, sat_name, self._ts)
                        epoch_dt = sat_obj.epoch.utc_datetime()
                        quality = _calc_quality(epoch_dt)
                    except Exception as exc:
                        logger.warning(f"SATNOGS TLE parse error {norad}: {exc}")
                        stats["errors"] += 1
                        return

                    self._conn.execute(
                        "INSERT OR REPLACE INTO tle_data"
                        " (norad_cat_id, name, line1, line2, epoch,"
                        "  source, tle_group, fetched_at, quality_score)"
                        " VALUES (?, ?, ?, ?, ?, 'satnogs', 'amateur', ?, ?)",
                        (norad, sat_name, line1, line2, epoch_dt.isoformat(), now, quality),
                    )
                    self._conn.execute(
                        "UPDATE satellites SET tle_no_result_since = NULL WHERE norad_cat_id = ?",
                        (norad,),
                    )
                    stats["inserted"] += 1

            await _asyncio.gather(
                *[
                    _fetch_one(norad, name, status, nrs)
                    for norad, name, status, nrs in still_missing
                ]
            )
            self._conn.commit()

        self._log_sync("celestrak-active", stats)
        return stats

    async def fetch_legacy_tles(
        self,
        progress_callback: Any = None,
    ) -> dict[str, int]:
        """Check very old satellites (NORAD < 10000) against CelesTrak one by one.

        For each visible satellite with NORAD ID < 10000 that has no TLE, queries
        CelesTrak individually using the CATNR parameter.

        - If CelesTrak returns a TLE → the satellite is still in orbit; store the TLE
          with source='celestrak' and tle_group='legacy'.
        - If CelesTrak returns nothing → the satellite has most likely re-entered;
          set is_hidden=2 so it no longer appears in any list.

        This method is designed as a one-time startup cleanup.  On subsequent calls
        all targets are either hidden or already have a TLE, so the query returns
        zero rows and the method returns immediately.

        Returns:
            {"found": N, "hidden": N, "errors": N}
        """
        rows = self._conn.execute(
            """
            SELECT s.norad_cat_id, s.name FROM satellites s
            LEFT JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id
            WHERE s.norad_cat_id < 10000
              AND s.is_hidden = 0
              AND t.norad_cat_id IS NULL
            """
        ).fetchall()

        if not rows:
            return {"found": 0, "hidden": 0, "errors": 0}

        stats: dict[str, int] = {"found": 0, "hidden": 0, "errors": 0}
        now = datetime.now(UTC).isoformat()
        url = "https://celestrak.org/NORAD/elements/gp.php"

        async with httpx.AsyncClient(timeout=15.0) as client:
            for idx, row in enumerate(rows):
                norad = int(row["norad_cat_id"])
                if progress_callback:
                    progress_callback(idx + 1, len(rows))

                try:
                    r = await client.get(
                        url,
                        params={"CATNR": str(norad), "FORMAT": "TLE"},
                    )
                    r.raise_for_status()
                    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]

                    if len(lines) >= 3:
                        # CelesTrak still tracks this satellite → save the TLE
                        name, line1, line2 = lines[0], lines[1], lines[2]
                        sat_obj = EarthSatellite(line1, line2, name, self._ts)
                        epoch_dt = sat_obj.epoch.utc_datetime()
                        quality = _calc_quality(epoch_dt)

                        self._conn.execute(
                            """
                            INSERT OR REPLACE INTO tle_data
                                (norad_cat_id, name, line1, line2, epoch,
                                 source, tle_group, fetched_at, quality_score)
                            VALUES (?, ?, ?, ?, ?, 'celestrak', 'legacy', ?, ?)
                            """,
                            (norad, name, line1, line2, epoch_dt.isoformat(), now, quality),
                        )
                        stats["found"] += 1
                    else:
                        # Not found in CelesTrak → presumed re-entered; hide it
                        self._conn.execute(
                            "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                            " WHERE norad_cat_id = ?",
                            (now, norad),
                        )
                        stats["hidden"] += 1

                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                    stats["errors"] += 1
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        # 404 = CelesTrak no longer tracks this satellite → hide it
                        self._conn.execute(
                            "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                            " WHERE norad_cat_id = ?",
                            (now, norad),
                        )
                        stats["hidden"] += 1
                    else:
                        logger.warning(f"legacy TLE fetch error for {norad}: {exc}")
                        stats["errors"] += 1
                except httpx.HTTPError as exc:
                    logger.warning(f"legacy TLE fetch error for {norad}: {exc}")
                    stats["errors"] += 1
                except Exception as exc:
                    logger.warning(f"legacy TLE parse error for {norad}: {exc}")
                    stats["errors"] += 1

        self._conn.commit()
        self._log_sync("legacy-tle-check", stats)
        return stats

    async def fetch_meteor_tles(
        self,
        progress_callback: Any = None,
    ) -> dict[str, int]:
        """Ensure every METEOR/HRPT satellite has a satellites row and current TLE.

        comms.meteor.satdump.METEOR_NORAD_IDS lists the fixed set of satellites the
        METEOR / HRPT reception tab supports.  Some of them (e.g. NOAA 18 / NOAA 19)
        have been retired from CelesTrak's curated GROUP=WEATHER listing even though
        CelesTrak still has current elements for them via an individual CATNR query —
        so the normal group fetch (fetch_and_update) never creates a satellites row
        for them and their TLE goes stale forever.  This method queries CelesTrak
        individually for any METEOR satellite that is missing a TLE or whose
        non-manual TLE has gone stale, so they always appear in the main satellite
        list (needed for AOS/LOS pass prediction) regardless of CelesTrak's group
        curation.  source='manual' TLEs are never touched.

        Returns:
            {"found": N, "skipped": N, "errors": N}
        """
        from comms.meteor.satdump import METEOR_NORAD_IDS

        targets: list[int] = []
        for norad in METEOR_NORAD_IDS:
            existing = self._conn.execute(
                "SELECT source FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            if existing is None or (
                existing["source"] != "manual" and self.needs_update(norad, max_age_hours=24.0)
            ):
                targets.append(norad)

        stats: dict[str, int] = {"found": 0, "skipped": 0, "errors": 0}
        if not targets:
            return stats

        now = datetime.now(UTC).isoformat()
        url = "https://celestrak.org/NORAD/elements/gp.php"

        async with httpx.AsyncClient(timeout=15.0) as client:
            for idx, norad in enumerate(targets):
                if progress_callback:
                    progress_callback(idx + 1, len(targets))

                try:
                    r = await client.get(url, params={"CATNR": str(norad), "FORMAT": "TLE"})
                    r.raise_for_status()
                    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]

                    if len(lines) < 3:
                        stats["skipped"] += 1
                        continue

                    name, line1, line2 = lines[0], lines[1], lines[2]
                    sat_obj = EarthSatellite(line1, line2, name, self._ts)
                    epoch_dt = sat_obj.epoch.utc_datetime()
                    quality = _calc_quality(epoch_dt)

                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO satellites (norad_cat_id, name, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (norad, name, now),
                    )
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO tle_data
                            (norad_cat_id, name, line1, line2, epoch,
                             source, tle_group, fetched_at, quality_score)
                        VALUES (?, ?, ?, ?, ?, 'celestrak', 'weather', ?, ?)
                        """,
                        (norad, name, line1, line2, epoch_dt.isoformat(), now, quality),
                    )
                    stats["found"] += 1

                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                    stats["errors"] += 1
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        stats["skipped"] += 1
                    else:
                        logger.warning(f"meteor TLE fetch error for {norad}: {exc}")
                        stats["errors"] += 1
                except httpx.HTTPError as exc:
                    logger.warning(f"meteor TLE fetch error for {norad}: {exc}")
                    stats["errors"] += 1
                except Exception as exc:
                    logger.warning(f"meteor TLE parse error for {norad}: {exc}")
                    stats["errors"] += 1

        self._conn.commit()
        self._log_sync("meteor-tle-check", stats)
        return stats

    async def fetch_provisional_tles(
        self,
        progress_callback: Any = None,
    ) -> dict[str, int]:
        """Fetch TLEs for provisional (NORAD >= 90000) satellites via the SATNOGS TLE API.

        For each visible satellite with a provisional NORAD ID (>= 90000), this method
        queries the SATNOGS TLE API which returns the best available TLE regardless of
        whether norad_follow_id is set publicly.  The TLE is stored under the provisional
        ID so the satellite's position can be shown on the map.

        When the TLE line1 contains a *different* NORAD ID (i.e. SATNOGS internally knows
        the official ID), the migration pipeline is triggered automatically if the official
        satellite record already exists in our DB.

        Returns:
            {"inserted": N, "updated": N, "no_tle": N,
             "hidden_unknown": N, "hidden_expired": N, "errors": N}
        """
        rows = self._conn.execute(
            "SELECT norad_cat_id, name, status, tle_no_result_since FROM satellites"
            " WHERE norad_cat_id >= 90000 AND is_hidden = 0"
        ).fetchall()

        stats: dict[str, int] = {
            "inserted": 0,
            "updated": 0,
            "no_tle": 0,
            "hidden_unknown": 0,
            "hidden_expired": 0,
            "errors": 0,
        }
        now = datetime.now(UTC).isoformat()

        async with httpx.AsyncClient(timeout=15.0) as client:
            for idx, row in enumerate(rows):
                fake_id = int(row["norad_cat_id"])
                sat_name = str(row["name"])
                sat_status = str(row["status"] or "unknown")
                no_result_since: str | None = (
                    str(row["tle_no_result_since"]) if row["tle_no_result_since"] else None
                )

                if progress_callback:
                    progress_callback(idx + 1, len(rows))

                try:
                    r = await client.get(
                        SATNOGS_TLE_URL,
                        params={"norad_cat_id": fake_id, "format": "json"},
                        timeout=10.0,
                    )
                    r.raise_for_status()
                    data = r.json()
                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                    # Silently skip on timeout/connection errors — these are
                    # expected when the app is shutting down or the network
                    # is momentarily unavailable.
                    stats["errors"] += 1
                    continue
                except httpx.HTTPError as exc:
                    ename = type(exc).__name__
                    logger.warning(f"provisional TLE fetch error for {fake_id}: {ename}: {exc}")
                    stats["errors"] += 1
                    continue
                except Exception as exc:
                    ename = type(exc).__name__
                    logger.warning(f"prov TLE unexpected error for {fake_id}: {ename}: {exc}")
                    stats["errors"] += 1
                    continue

                # The SATNOGS TLE API returns a JSON list; take the first element.
                if isinstance(data, list):
                    data = data[0] if data else {}
                if not isinstance(data, dict) or "tle1" not in data:
                    # ---- No TLE available from SATNOGS ----
                    # 'dead' treated same as 'unknown': hide immediately.
                    if sat_status in ("unknown", "dead"):
                        self._conn.execute(
                            "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                            " WHERE norad_cat_id = ?",
                            (now, fake_id),
                        )
                        stats["hidden_unknown"] += 1
                    else:
                        # status='alive': start / check the 30-day grace period.
                        # Use tle_no_result_since as a latch: set once, never reset
                        # unless a TLE is actually found.
                        if no_result_since is None:
                            # First time no TLE → record the start of the grace period
                            self._conn.execute(
                                "UPDATE satellites SET tle_no_result_since = ?, updated_at = ?"
                                " WHERE norad_cat_id = ?",
                                (now, now, fake_id),
                            )
                        else:
                            # Already in grace period → check if 30 days have elapsed
                            since_dt = datetime.fromisoformat(no_result_since)
                            if since_dt.tzinfo is None:
                                since_dt = since_dt.replace(tzinfo=UTC)
                            if datetime.now(UTC) - since_dt > timedelta(days=30):
                                self._conn.execute(
                                    "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                                    " WHERE norad_cat_id = ?",
                                    (now, fake_id),
                                )
                                stats["hidden_expired"] += 1
                            # else: still within grace period → leave visible (yellow in UI)
                    stats["no_tle"] += 1
                    continue

                line1: str = str(data["tle1"])
                line2: str = str(data["tle2"])
                # Prefer the name already stored in our DB over Space-Track object names
                name = sat_name

                try:
                    sat_obj = EarthSatellite(line1, line2, name, self._ts)
                    epoch_dt = sat_obj.epoch.utc_datetime()
                    quality = _calc_quality(epoch_dt)
                except Exception as exc:
                    logger.warning(f"provisional TLE parse error for {fake_id}: {exc}")
                    stats["errors"] += 1
                    continue

                # Check whether the TLE line1 encodes a different (official) NORAD ID
                tle_norad = int(line1[2:7])
                if tle_norad != fake_id:
                    # SATNOGS internally resolved this provisional ID to an official one.
                    # Trigger the migration pipeline if the official satellite is already
                    # present in our DB (e.g. fetched earlier from CelesTrak).
                    official_exists = self._conn.execute(
                        "SELECT norad_cat_id FROM satellites WHERE norad_cat_id = ?",
                        (tle_norad,),
                    ).fetchone()
                    if official_exists:
                        # Import lazily to avoid a circular dependency at module level
                        from data.transmitter_manager import TransmitterManager  # noqa: PLC0415

                        TransmitterManager(self._conn)._run_migration_pipeline(fake_id, tle_norad)

                # TLE found → clear the no-result grace-period latch if it was set
                if no_result_since is not None:
                    self._conn.execute(
                        "UPDATE satellites SET tle_no_result_since = NULL WHERE norad_cat_id = ?",
                        (fake_id,),
                    )

                # Never overwrite a manually entered TLE
                existing = self._conn.execute(
                    "SELECT source FROM tle_data WHERE norad_cat_id = ?",
                    (fake_id,),
                ).fetchone()
                if existing and existing["source"] == "manual":
                    continue

                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO tle_data
                        (norad_cat_id, name, line1, line2, epoch,
                         source, tle_group, fetched_at, quality_score)
                    VALUES (?, ?, ?, ?, ?, 'satnogs', 'amateur', ?, ?)
                    """,
                    (fake_id, name, line1, line2, epoch_dt.isoformat(), now, quality),
                )
                if existing:
                    stats["updated"] += 1
                else:
                    stats["inserted"] += 1

        self._conn.commit()
        self._log_sync("satnogs-provisional", stats)
        return stats

    def _log_sync(self, sync_type: str, stats: dict[str, int]) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO sync_log
                (sync_type, started_at, finished_at, status, records_updated)
            VALUES (?, ?, ?, ?, ?)
        """,
            (sync_type, now, now, "success", stats.get("inserted", 0) + stats.get("updated", 0)),
        )
        self._conn.commit()
