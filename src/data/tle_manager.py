"""
TLE (Two-Line Element) automatic update manager

Fetches TLEs from multiple sources (CelesTrak, Space-Track, AMSAT),
applies quality scoring, and saves them to SQLite.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Callable, Coroutine
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


async def _probe_reachable(url: str, params: dict[str, Any]) -> bool:
    """Try a single GET request to check whether `url` is reachable right now.

    Used as a fail-fast circuit breaker before looping over many individual
    per-satellite fallback requests (against either CelesTrak or SATNOGS):
    if the very first one can't even establish a connection, the host is
    almost certainly down for the whole run, and trying the rest one by one
    would just mean waiting out each one's own ~10s timeout in turn — up to
    20+ minutes for the full provisional-satellite population (confirmed via
    a "restarted the app but nothing changed" report, 2026-08-09, while
    db.satnogs.org was unreachable). Returns False only for a
    connection-level failure (ConnectTimeout / ConnectError); any other
    outcome — including an HTTP error status or a malformed response — still
    proves the host responded, so it's treated as reachable and the caller
    proceeds normally (the real per-satellite loop will surface that
    satellite's own error itself).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.get(url, params=params, timeout=10.0)
        return True
    except (httpx.ConnectTimeout, httpx.ConnectError):
        return False
    except Exception:
        return True


# CelesTrak's documented usage policy: an IP is sent to the firewall after 50
# HTTP errors (301/403/404) within a 2-hour window
# (https://celestrak.org/usage-policy.php, confirmed 2026-08-11). Phase 2a
# below can touch 800+ satellites in one run, and a meaningful fraction
# genuinely have no CelesTrak entry at all (404 is the *expected*, not
# exceptional, response for those) -- so without a cap, a single run can by
# itself burn through the whole 2-hour error budget and get the IP blocked
# (confirmed via a real run's log, 2026-08-10: a clean run of 200/404s up to
# satellite #~400 of 846, then 403 on every single remaining request for the
# rest of the run). Kept well under 50 to leave headroom for whatever else
# shares this IP's 2-hour window (the periodic per-group jobs, other users on
# the same network, etc.).
#
# SATNOGS documents no equivalent limit, but "undocumented" isn't the same as
# "none" -- Phase 2b gets the same treatment out of caution.
_CELESTRAK_CATNR_ERROR_LIMIT = 20
_SATNOGS_TLE_ERROR_LIMIT = 20


class _ErrorCountBreaker:
    """Stops a batch of per-satellite provider queries before it can trip
    the provider's own abuse protection.

    Two ways to trip: `error_limit` errors accumulate (e.g. repeated 404s,
    which are individually normal but still count against CelesTrak's
    budget), or a single call reports `blocked=True` (e.g. an HTTP 403 --
    proof the block has *already* started, so there's no point counting
    further errors before giving up).

    Once tripped, callers should stop starting new requests but may let
    already-in-flight ones finish normally (see `_run_with_breaker`).
    """

    def __init__(self, error_limit: int) -> None:
        self._error_limit = error_limit
        self._error_count = 0
        self.tripped = False

    def record_error(self, *, blocked: bool = False) -> None:
        if blocked:
            self.tripped = True
            return
        self._error_count += 1
        if self._error_count >= self._error_limit:
            self.tripped = True


async def _run_with_breaker(
    tasks: list[Callable[[], Coroutine[Any, Any, None]]],
    breaker: _ErrorCountBreaker,
    concurrency: int = 20,
) -> None:
    """Runs each zero-arg async callable in `tasks` with up to `concurrency`
    running at once (same effective concurrency as the plain
    `asyncio.gather` + `Semaphore` pattern this replaces), but stops pulling
    *new* work the moment `breaker.tripped` becomes True. Requests already
    in flight when that happens are left to finish normally — only the
    queue of not-yet-started work is abandoned, so a provider that's
    already blocking us doesn't get hit with hundreds more requests it's
    already refusing.
    """
    import asyncio  # noqa: PLC0415

    queue: asyncio.Queue[Any] = asyncio.Queue()
    for t in tasks:
        queue.put_nowait(t)

    async def _worker() -> None:
        while not breaker.tripped:
            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await task()

    worker_count = min(concurrency, len(tasks)) or 1
    await asyncio.gather(*[_worker() for _ in range(worker_count)])


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

    # ── Persisted retry marker for a blocked fetch_active_tles() run ──────
    #
    # _log_sync() always records a 'success' sync_log entry once
    # fetch_active_tles() finishes, even when a phase's _ErrorCountBreaker
    # cut it short — so is_active_tle_stale() alone would treat a blocked
    # run as "fresh for the next 24h" and never retry sooner. The 3-hour
    # retry MainWindow schedules for a still-running app (an in-memory
    # APScheduler one-shot job) doesn't survive the app being closed and
    # reopened, either — so without this, closing the app before that
    # retry fires and reopening it later (but still within 24h of the
    # blocked run) would silently skip re-fetching, leaving whatever
    # satellites were left unresolved stuck until the 24h mark (confirmed
    # gap, 2026-08-11). Persisting the retry time to app_settings lets
    # is_active_tle_retry_due() give the startup path an independent
    # reason to fetch again, regardless of how much wall-clock time the
    # app was actually running for.
    def get_active_tle_retry_after(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'active_tle_retry_after'"
        ).fetchone()
        if not row or not row["value"]:
            return None
        try:
            when = datetime.fromisoformat(str(row["value"]))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when

    def set_active_tle_retry_after(self, when: datetime | None) -> None:
        """Persist (or, with `when=None`, clear) the retry-due time."""
        if when is None:
            self._conn.execute("DELETE FROM app_settings WHERE key = 'active_tle_retry_after'")
        else:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES"
                " ('active_tle_retry_after', ?)",
                (when.isoformat(),),
            )
        self._conn.commit()

    def is_active_tle_retry_due(self) -> bool:
        """True if a previously-blocked run's retry time has arrived (or
        passed) — independent of is_active_tle_stale()'s 24h cadence."""
        retry_after = self.get_active_tle_retry_after()
        return retry_after is not None and datetime.now(UTC) >= retry_after

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

    async def fetch_active_tles(self, progress_callback: Any = None) -> dict[str, int]:
        """Fill TLE gaps for SATNOGS-registered satellites (NORAD 10000-89999).

        Phase 1 — CelesTrak bulk groups (single request per group, fast):
          Downloads several CelesTrak groups that collectively cover most
          SATNOGS-registered satellites. GROUP=active would cover far more in
          one request, but is deliberately not used — see CLAUDE.md's
          "fetch_active_tles() の2フェーズ設計" for why (it succeeds only
          once per CelesTrak's 2h update cycle, and downloads ~11x more data
          than this app actually needs).

        Phase 2 — individual per-satellite queries (concurrent, up to 20 at a
        time), for whatever Phase 1 didn't cover. Two stages, CelesTrak then
        SATNOGS — see the inline "Phase 2a"/"Phase 2b" comments below for the
        full rationale (both fail fast via _probe_reachable() if the host is
        unreachable, rather than waiting out every straggler's own timeout).

        `progress_callback`, if given, is called with a short human-readable
        string at each phase/group transition (not per-satellite — Phase 2
        can involve hundreds of concurrent requests) so a caller updating a
        UI status label can show that this is still working, not stuck. This
        matters because this method used to be silent for however long
        Phase 2 took, which looked identical to a hang and led to at least
        one user closing the app before it ever reached the satellite they
        cared about (2026-08-10).

        New satellite records are never created; only existing satellites are updated.
        Manual TLEs are never overwritten.
        Existing tle_group values are preserved on UPDATE.

        Returns:
            {"inserted": N, "updated": N, "no_tle": N, "hidden_unknown": N,
             "hidden_expired": N, "errors": N, "celestrak_blocked": 0|1,
             "satnogs_blocked": 0|1, "phase2_total": N,
             "phase2_unresolved": N}. The two "_blocked" flags are 1 when
            that phase's _ErrorCountBreaker tripped and stopped early —
            callers can use this to schedule a later retry instead of
            assuming every unresolved satellite this run genuinely has no
            TLE anywhere. "phase2_total" is how many satellites needed the
            Phase 2 per-satellite fallback this run (0 if Phase 1 covered
            everything); "phase2_unresolved" is how many of those were
            still unresolved when the run ended (via a tripped breaker, or
            because neither provider has that satellite at all) — a caller
            can show "Fetched {phase2_total - phase2_unresolved}/{phase2_total}"
            alongside a "_blocked" flag to tell the user how far a paused
            run got.
        """
        # CelesTrak groups accessible without authentication that provide good coverage
        # of SATNOGS-registered satellites (GROUP=active deliberately not used — see above).
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
            "celestrak_blocked": 0,
            "satnogs_blocked": 0,
            "phase2_total": 0,
            "phase2_unresolved": 0,
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
                if progress_callback:
                    progress_callback(f"CelesTrak {group}...")
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

        # ── Phase 2: fill gaps Phase 1's bulk groups didn't cover ─────────
        # Targets two groups: satellites with no TLE row at all, AND satellites
        # whose current TLE was itself obtained via one of the two fallbacks
        # below (source='satnogs' — used by both). The latter is required —
        # otherwise a satellite not tracked by any Phase 1 bulk CelesTrak
        # group gets exactly one TLE ever (whichever run first discovers it)
        # and is then silently excluded from every subsequent run merely for
        # already having a tle_data row, no matter how stale it becomes.
        # Confirmed stuck this way for 44 days for ORIGAMISAT-2 / NORAD 68795
        # (2026-08-09) before this fix, alongside ~700 other satellites
        # across three earlier one-shot Phase 2 runs.
        # source='celestrak' rows are left alone here: they're kept fresh by
        # the periodic group-specific fetches (2h/4h/6h/12h) and by Phase 1
        # above, so re-querying them individually here would be redundant
        # load. source='manual' rows are never touched by any automated sync.
        #
        # Two-stage fallback, both concurrent (up to 20 at a time) with a
        # fail-fast reachability probe first (_probe_reachable()):
        #   Phase 2a — CelesTrak, individual CATNR query per satellite.
        #     CelesTrak's gp.php CATNR parameter only accepts a single
        #     catalog number per request — confirmed against CelesTrak's own
        #     documentation and a live query, after an earlier version of
        #     this method wrongly assumed a comma-delimited list was
        #     supported (it returns HTTP 200 with an "Invalid query" body,
        #     which silently resolved zero satellites without ever raising
        #     an exception, 2026-08-09). Looping per-satellite here is the
        #     same shape fetch_legacy_tles()/fetch_meteor_tles() already use,
        #     just applied to a larger, uncapped population — the added
        #     concurrency and circuit breaker keep that from being the
        #     multi-minute serial hammering CelesTrak's usage guidance
        #     discourages.
        #   Phase 2b — SATNOGS TLE API, individual per-satellite, for
        #     whatever Phase 2a didn't resolve. Kept as the last-resort
        #     fallback since SATNOGS aggregates from more sources (including
        #     Space-Track) than CelesTrak's own public catalog. Both stages
        #     write source='satnogs' regardless of which one actually
        #     supplied the data — the tle_data.source CHECK constraint has
        #     no 'celestrak-catnr' value, and semantically both stages exist
        #     for the same reason: keep retrying a satellite Phase 1 doesn't
        #     cover, which is exactly what the 'satnogs' tag already means
        #     to this method's own WHERE clause below.
        #
        # Satellites migrated from a provisional (>=90000) ID retain
        # satnogs_source_id pointing at the old ID. Only Phase 2b uses it —
        # CelesTrak has no notion of that ID, so Phase 2a always queries by
        # the real norad_cat_id. SATNOGS's TLE API can continue to serve the
        # TLE keyed by that old ID for a long time after migration (observed
        # for ARICA-2 / NORAD 68796, 2026-07-12), mirroring the routing
        # already used for transmitter sync in sync_from_satnogs().
        refresh_targets = [
            (
                int(r["norad_cat_id"]),
                str(r["name"]),
                str(r["status"] or "unknown"),
                str(r["tle_no_result_since"]) if r["tle_no_result_since"] else None,
                int(r["satnogs_source_id"]) if r["satnogs_source_id"] else None,
                str(r["tle_group"]) if r["tle_group"] else "amateur",
                bool(r["had_tle"]),
            )
            for r in self._conn.execute(
                """
                SELECT s.norad_cat_id, s.name, s.status, s.tle_no_result_since,
                       s.satnogs_source_id, t.tle_group,
                       (t.norad_cat_id IS NOT NULL) AS had_tle
                FROM satellites s
                LEFT JOIN tle_data t ON s.norad_cat_id = t.norad_cat_id
                WHERE s.is_hidden = 0
                  AND s.norad_cat_id BETWEEN 10000 AND 89999
                  AND (t.norad_cat_id IS NULL OR t.source = 'satnogs')
                """
            ).fetchall()
        ]

        if refresh_targets:
            # norad -> (name, status, no_result_since, source_id, tle_group, had_tle)
            remaining: dict[int, tuple[str, str, str | None, int | None, str, bool]] = {
                norad: (name, status, nrs, source_id, tle_group, had_tle)
                for norad, name, status, nrs, source_id, tle_group, had_tle in refresh_targets
            }
            # Snapshotted once here (not recomputed later) so a "Fetched X/Y"
            # style status message can be built from stats alone -- `remaining`
            # itself shrinks as satellites resolve, so this is the only place
            # the original target count is available.
            phase2_total = len(remaining)

            def _store_resolved(
                norad: int, name_l: str, line1: str, line2: str, epoch_dt: datetime
            ) -> bool:
                """Parse+store a resolved TLE and pop it from `remaining`.

                Returns False (and leaves `remaining` untouched) if the TLE
                fails to parse or `norad` was already resolved by another
                concurrent task — both callers must skip further processing
                in that case.
                """
                if norad not in remaining:
                    return False
                try:
                    EarthSatellite(line1, line2, name_l, self._ts)  # validates the elements
                    quality = _calc_quality(epoch_dt)
                except Exception:
                    stats["errors"] += 1
                    return False
                _, _, _, _, tle_group, had_tle = remaining.pop(norad)
                self._conn.execute(
                    "INSERT OR REPLACE INTO tle_data"
                    " (norad_cat_id, name, line1, line2, epoch,"
                    "  source, tle_group, fetched_at, quality_score)"
                    " VALUES (?, ?, ?, ?, ?, 'satnogs', ?, ?, ?)",
                    (norad, name_l, line1, line2, epoch_dt.isoformat(), tle_group, now, quality),
                )
                self._conn.execute(
                    "UPDATE satellites SET tle_no_result_since = NULL WHERE norad_cat_id = ?",
                    (norad,),
                )
                if had_tle:
                    stats["updated"] += 1
                else:
                    stats["inserted"] += 1
                return True

            # ── Phase 2a: CelesTrak, individual CATNR query per satellite ──
            if progress_callback:
                progress_callback(f"CelesTrak: {len(remaining)} satellite(s)...")
            probe_norad_2a = next(iter(remaining))
            breaker_2a = _ErrorCountBreaker(_CELESTRAK_CATNR_ERROR_LIMIT)
            if await _probe_reachable(url_ct, {"CATNR": str(probe_norad_2a), "FORMAT": "TLE"}):

                async def _fetch_one_celestrak(norad: int) -> None:
                    try:
                        async with httpx.AsyncClient(timeout=15.0) as c:
                            r = await c.get(
                                url_ct,
                                params={"CATNR": str(norad), "FORMAT": "TLE"},
                                timeout=10.0,
                            )
                            r.raise_for_status()
                    except (
                        httpx.ConnectTimeout,
                        httpx.ConnectError,
                        httpx.ReadTimeout,
                        httpx.RemoteProtocolError,
                    ):
                        return
                    except httpx.HTTPStatusError as exc:
                        logger.warning(f"CelesTrak CATNR fetch error for {norad}: {exc}")
                        # 403 is CelesTrak's own signal that this IP is already
                        # on the firewall list for the current 2h window — no
                        # point spending more of the error budget finding out
                        # again. 404 (and any other 4xx/5xx) just counts
                        # toward the budget like the usage policy describes.
                        breaker_2a.record_error(blocked=exc.response.status_code == 403)
                        return
                    except httpx.HTTPError as exc:
                        logger.warning(f"CelesTrak CATNR fetch error for {norad}: {exc}")
                        return

                    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
                    # Not found (empty body) or an "Invalid query" error body —
                    # either way, not a valid 3-line TLE block.
                    if len(lines) < 3 or not (
                        lines[1].startswith("1 ") and lines[2].startswith("2 ")
                    ):
                        return
                    name_l, line1, line2 = lines[0], lines[1], lines[2]
                    try:
                        sat_obj = EarthSatellite(line1, line2, name_l, self._ts)
                        epoch_dt = sat_obj.epoch.utc_datetime()
                    except Exception:
                        stats["errors"] += 1
                        return
                    _store_resolved(norad, name_l, line1, line2, epoch_dt)

                await _run_with_breaker(
                    [functools.partial(_fetch_one_celestrak, n) for n in list(remaining.keys())],
                    breaker_2a,
                )
                self._conn.commit()
                if breaker_2a.tripped:
                    logger.warning(
                        "CelesTrak CATNR circuit breaker tripped — stopped Phase 2a early "
                        "(%d satellite(s) still unresolved) to avoid CelesTrak's own "
                        "abuse-protection firewall; falling back to SATNOGS for the rest",
                        len(remaining),
                    )
                stats["celestrak_blocked"] = int(breaker_2a.tripped)
            else:
                logger.warning(
                    "CelesTrak unreachable — skipping %d individual CATNR fetch(es) this run",
                    len(remaining),
                )
                # Treat a fully-unreachable host the same as an explicit 403
                # block: without this, the caller's stats dict never gets
                # celestrak_blocked=1 and _schedule_active_tle_retry_if_blocked()
                # (main_window.py) has nothing to trigger on, so the status bar
                # is left stuck on the "CelesTrak: N satellite(s)..." message
                # from just above with no indication anything went wrong, and
                # the next attempt happens on the normal 24h/2h schedule with
                # no backoff -- silently repeating the same fast-fail forever
                # instead of pausing and surfacing an error (2026-08-10,
                # confirmed against a real firewall-blocked IP: TCP connect
                # itself times out, never even reaching an HTTP response).
                stats["celestrak_blocked"] = 1
                if progress_callback:
                    progress_callback(
                        f"CelesTrak unreachable — {len(remaining)} satellite(s) deferred"
                    )

            # ── Phase 2b: SATNOGS TLE API fallback for whatever Phase 2a
            # didn't resolve ───────────────────────────────────────────────
            if remaining and progress_callback:
                progress_callback(f"SATNOGS: {len(remaining)} satellite(s)...")
            # Tracked separately rather than emptying `remaining` on an
            # unreachable probe: `remaining` also feeds phase2_unresolved
            # below, and clearing it here used to make an unreachable SATNOGS
            # look like "everything got resolved" in the "Fetched X/Y" status
            # message (2026-08-10 fix, alongside the celestrak_blocked gap
            # noted above).
            satnogs_reachable = True
            if remaining:
                probe_norad = next(iter(remaining))
                probe_source_id = remaining[probe_norad][3]
                probe_query_id = probe_source_id if probe_source_id is not None else probe_norad
                satnogs_reachable = await _probe_reachable(
                    SATNOGS_TLE_URL, {"norad_cat_id": probe_query_id, "format": "json"}
                )
                if not satnogs_reachable:
                    logger.warning(
                        "SATNOGS TLE API unreachable — skipping %d individual "
                        "fallback fetch(es) this run",
                        len(remaining),
                    )
                    stats["errors"] += len(remaining)
                    stats["satnogs_blocked"] = 1
                    if progress_callback:
                        progress_callback(
                            f"SATNOGS unreachable — {len(remaining)} satellite(s) deferred"
                        )

            if remaining and satnogs_reachable:
                breaker_2b = _ErrorCountBreaker(_SATNOGS_TLE_ERROR_LIMIT)

                async def _fetch_one(
                    norad: int,
                    sat_name: str,
                    sat_status: str,
                    no_result_since: str | None,
                    source_id: int | None,
                ) -> None:
                    query_id = source_id if source_id is not None else norad
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as c:
                            resp = await c.get(
                                SATNOGS_TLE_URL,
                                params={"norad_cat_id": query_id, "format": "json"},
                            )
                            resp.raise_for_status()
                            data = resp.json()
                    except (
                        httpx.ConnectTimeout,
                        httpx.ReadTimeout,
                        httpx.RemoteProtocolError,
                    ):
                        stats["errors"] += 1
                        return
                    except httpx.HTTPStatusError as exc:
                        logger.warning(f"SATNOGS TLE fallback error {norad}: {exc}")
                        stats["errors"] += 1
                        # SATNOGS documents no rate-limit policy, but HTTP 429
                        # ("Too Many Requests") is the standard way a server
                        # says so anyway -- treat it the same as CelesTrak's
                        # 403: stop immediately rather than count toward the
                        # generic error budget.
                        breaker_2b.record_error(blocked=exc.response.status_code == 429)
                        return
                    except Exception as exc:
                        logger.warning(f"SATNOGS TLE fallback error {norad}: {exc}")
                        stats["errors"] += 1
                        breaker_2b.record_error()
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
                    except Exception as exc:
                        logger.warning(f"SATNOGS TLE parse error {norad}: {exc}")
                        stats["errors"] += 1
                        return

                    _store_resolved(norad, sat_name, line1, line2, epoch_dt)

                await _run_with_breaker(
                    [
                        functools.partial(_fetch_one, norad, name, status, nrs, source_id)
                        for norad, (
                            name,
                            status,
                            nrs,
                            source_id,
                            _tle_group,
                            _had_tle,
                        ) in remaining.items()
                    ],
                    breaker_2b,
                )
                self._conn.commit()
                if breaker_2b.tripped:
                    logger.warning(
                        "SATNOGS TLE circuit breaker tripped — stopped Phase 2b early "
                        "to avoid overloading db.satnogs.org; remaining satellites will "
                        "be retried on the next scheduled run"
                    )
                stats["satnogs_blocked"] = int(breaker_2b.tripped)

            stats["phase2_total"] = phase2_total
            stats["phase2_unresolved"] = len(remaining)

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

        Before looping, probes SATNOGS reachability with a single request
        (_probe_reachable()). If that fails to even connect, the whole run is
        skipped immediately rather than looping over every satellite — see
        that function's docstring for why. Otherwise, requests run up to 20
        at a time (matching fetch_active_tles()'s Phase 2b) rather than one
        after another, so a run where SATNOGS *is* reachable but individual
        satellites are slow still doesn't serialize unnecessarily.

        When the TLE line1 contains a *different* NORAD ID (i.e. SATNOGS internally knows
        the official ID), the migration pipeline is triggered automatically if the official
        satellite record already exists in our DB.

        Returns:
            {"inserted": N, "updated": N, "no_tle": N,
             "hidden_unknown": N, "hidden_expired": N, "errors": N}
        """
        import asyncio as _asyncio  # noqa: PLC0415

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
        if not rows:
            return stats

        if not await _probe_reachable(
            SATNOGS_TLE_URL, {"norad_cat_id": int(rows[0]["norad_cat_id"]), "format": "json"}
        ):
            logger.warning(
                "SATNOGS TLE API unreachable — skipping %d provisional TLE fetch(es) this run",
                len(rows),
            )
            stats["errors"] = len(rows)
            self._log_sync("satnogs-provisional", stats)
            return stats

        now = datetime.now(UTC).isoformat()
        total = len(rows)
        done = 0

        semaphore = _asyncio.Semaphore(20)  # max 20 concurrent requests

        async def _fetch_one(row: sqlite3.Row) -> None:
            nonlocal done
            fake_id = int(row["norad_cat_id"])
            sat_name = str(row["name"])
            sat_status = str(row["status"] or "unknown")
            no_result_since: str | None = (
                str(row["tle_no_result_since"]) if row["tle_no_result_since"] else None
            )

            async with semaphore:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        r = await client.get(
                            SATNOGS_TLE_URL,
                            params={"norad_cat_id": fake_id, "format": "json"},
                            timeout=10.0,
                        )
                        r.raise_for_status()
                        data = r.json()
                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                    # Expected when the app is shutting down or the network is
                    # momentarily unavailable — logged at debug (not warning)
                    # to avoid spamming, but still records the exception type
                    # since these stringify to "" and give no clue otherwise.
                    ename = type(exc).__name__
                    logger.debug(f"provisional TLE fetch timeout for {fake_id}: {ename}")
                    stats["errors"] += 1
                    return
                except httpx.HTTPError as exc:
                    ename = type(exc).__name__
                    logger.warning(f"provisional TLE fetch error for {fake_id}: {ename}: {exc}")
                    stats["errors"] += 1
                    return
                except Exception as exc:
                    ename = type(exc).__name__
                    logger.warning(f"prov TLE unexpected error for {fake_id}: {ename}: {exc}")
                    stats["errors"] += 1
                    return
                finally:
                    done += 1
                    if progress_callback:
                        progress_callback(done, total)

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
                return

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
                return

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
                return

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

        await _asyncio.gather(*[_fetch_one(row) for row in rows])

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
