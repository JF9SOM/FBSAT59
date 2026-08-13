"""
TLE (Two-Line Element) automatic update manager

Fetches TLEs from multiple sources (CelesTrak, Space-Track, AMSAT),
applies quality scoring, and saves them to SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from skyfield.api import EarthSatellite, load

from data.http_client import DEFAULT_HEADERS

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

# Human-readable labels for TLE_SOURCES["name"] values, shown in Settings'
# enabled-sources checkboxes and in progress messages during the 6-group
# bulk fetch loop (main_window.py's "Fetching group TLEs: X (i/n)..."). A
# single source of truth so both places stay in sync -- this used to be a
# private copy inside settings_dialog.py, which meant the status-bar
# progress message showed the raw internal source name (e.g.
# "celestrak-earth-obs") instead of a name matching what the user actually
# sees elsewhere in the UI (2026-08-13 report: fetch progress messages
# were too vague about what/where).
TLE_SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "celestrak-stations": "Space Stations (CelesTrak)",
    "celestrak-amateur": "Amateur Satellites (CelesTrak)",
    "celestrak-cubesat": "CubeSat (CelesTrak)",
    "celestrak-weather": "Weather Satellites (CelesTrak)",
    "celestrak-earth-obs": "Earth Observation (CelesTrak)",
    "celestrak-science": "Science Satellites (CelesTrak)",
}


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
        async with httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS) as client:
            await client.get(url, params=params, timeout=10.0)
        return True
    except (httpx.ConnectTimeout, httpx.ConnectError):
        return False
    except Exception:
        return True


def _is_active_cache_not_yet_updated(response_text: str) -> bool:
    """True if a 403 from CelesTrak's GROUP=active endpoint is the documented
    "cache hasn't refreshed yet" response, not evidence of an IP-level abuse
    block.

    GROUP=active only updates server-side roughly every 2 hours; a repeat
    request inside that window returns HTTP 403 with an explanatory body
    instead of new data (confirmed against a real response, 2026-08-09):
    "GP data has not updated since your last successful download of
    GROUP=active at ...". fetch_active_tles() itself only runs automatically
    at most once per 24h, but the "Update TLE" button and Settings > OK
    bypass that staleness gate -- pressing either twice within 2h must not
    be misread as an abuse block (which would falsely trip the circuit
    breaker and show an alarming "blocked, retrying in 3h" status for a
    request that was never actually rejected for abuse).

    2026-08-11 bug fix: this check never once matched a real response,
    confirmed via the diagnostic logging added earlier the same day. The
    actual body word-wraps with CRLF line breaks roughly every ~55
    characters -- including right in the middle of the exact phrase this
    function searches for ("...your last successful\r\ndownload of
    GROUP=active..."). A plain substring check against response_text.lower()
    can never match text split across a line break, so every real
    cache-not-yet-updated 403 was silently misclassified as a genuine block:
    the circuit breaker tripped, a needless 3h backoff got scheduled, and
    the status bar showed an alarming "CelesTrak blocked" for a request that
    was never actually rejected for abuse -- precisely the failure mode this
    function was written to prevent (the invented single-line test fixture
    used to cover this never reproduced the wrapping, so it never caught
    it). Fixed by collapsing all whitespace runs (including the embedded
    CRLF) to single spaces before matching.
    """
    normalized = " ".join(response_text.lower().split())
    return "has not updated since your last successful download" in normalized


async def _get_with_progress(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    label: str,
    progress_callback: Any,
) -> httpx.Response:
    """Drop-in replacement for `await client.get(url, params=params)` that
    additionally reports download progress via `progress_callback`, called
    as `progress_callback(f"{label}: downloading TLE data... {pct}%")`
    (throttled to whole-percent changes so a multi-MB download doesn't
    flood the UI with one status-bar update per chunk) as the body streams
    in. Falls back to a single `f"{label}: downloading TLE data..."`
    message with no percentage if the server doesn't send a Content-Length
    header.

    Used for CelesTrak's GROUP=active (Phase 1) and SATNOGS's bulk TLE dump
    (Phase 2 / fetch_provisional_tles(), confirmed ~512KB unpaginated at
    the time of testing, 2026-08-11) -- both single-request bulk downloads
    large enough that "nothing happened for several seconds" would
    otherwise look identical to a hang, the same concern that motivated
    progress_callback in the first place (see fetch_active_tles()'s
    docstring, 2026-08-10).

    The returned Response has its body fully read by the time this
    function returns, so callers use `.raise_for_status()`/`.text`/`.json()`
    on it exactly like a response from `client.get()` -- including in an
    `except httpx.HTTPStatusError as exc:` handler, since `exc.response`
    is this same already-read object.

    2026-08-11 bug fix: an earlier version of this function iterated
    `response.aiter_bytes()` for the per-chunk progress count but discarded
    the chunks, on the mistaken assumption that httpx caches `.content`
    automatically once a streamed body is fully iterated. It does not --
    only `.aread()` populates `response._content`; a bare `aiter_bytes()`
    loop leaves the response looking "unread" to httpx. Confirmed via a
    real run's log the same day: the download itself succeeded (200 OK /
    403 with a body), but every downstream `.json()`/`.text` access on the
    returned Response raised `httpx.ResponseNotRead`, silently discarding
    an otherwise-successful fetch. Fixed by accumulating the chunks
    ourselves and assigning `response._content` directly -- there is no
    public "I already read it via aiter_bytes(), here it is" API; this is
    what `.aread()` does internally.
    """
    async with client.stream("GET", url, params=params) as response:
        total_header = response.headers.get("content-length")
        total_bytes = int(total_header) if total_header and total_header.isdigit() else None
        chunks: list[bytes] = []
        downloaded = 0
        last_pct = -1
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            downloaded += len(chunk)
            if progress_callback and total_bytes:
                pct = int(downloaded * 100 / total_bytes)
                if pct != last_pct:
                    last_pct = pct
                    progress_callback(f"{label}: downloading TLE data... {pct}%")
        if progress_callback and total_bytes is None and downloaded > 0:
            progress_callback(f"{label}: downloading TLE data...")
        response._content = b"".join(chunks)
        return response


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

# Matches CelesTrak's documented 2-hour error-count window, and is used for
# SATNOGS too out of the same caution noted above. See _ErrorCountBreaker's
# docstring for why this needs to be a rolling window, not a plain lifetime
# count.
_ERROR_WINDOW = timedelta(hours=2)

# How long TLEManager._fetch_satnogs_bulk_tles()'s in-instance cache stays
# valid. See that method's docstring.
_SATNOGS_BULK_CACHE_TTL = timedelta(minutes=10)


class _ErrorCountBreaker:
    """Stops a batch of per-satellite provider queries before it can trip
    the provider's own abuse protection.

    Two ways to trip: `error_limit` errors accumulate within a rolling
    `_ERROR_WINDOW` (e.g. repeated 404s, which are individually normal but
    still count against CelesTrak's budget), or a single call reports
    `blocked=True` (e.g. an HTTP 403 -- proof the block has *already*
    started, so there's no point counting further errors before giving up).
    A `blocked=True` report keeps `tripped` True for the rest of the window,
    not just for the remainder of whatever loop reported it.

    Once tripped, callers should stop starting new requests but may let
    already-in-flight ones finish normally (see `_run_with_breaker`).

    Instances are meant to be long-lived and shared across many independent
    calls on the same TLEManager (see TLEManager._celestrak_breaker /
    _satnogs_breaker below), not recreated fresh for every fetch_*() call.
    Before 2026-08-11, Phase 2a (CelesTrak) and Phase 2b (SATNOGS) each
    created their own breaker from scratch on every fetch_active_tles()
    call, and fetch_legacy_tles()/fetch_meteor_tles() had no breaker at
    all -- so a single startup sequence that runs several of these fetch
    methods back-to-back could burn through several independent 20-error
    allowances (up to 60+ actual errors against CelesTrak) before any one
    of them individually noticed, even though the provider counts all of
    them against the same IP-level budget. Sharing one instance per
    provider across the whole TLEManager's lifetime fixes that.
    """

    def __init__(self, error_limit: int, window: timedelta = _ERROR_WINDOW) -> None:
        self._error_limit = error_limit
        self._window = window
        self._error_times: deque[datetime] = deque()
        self._blocked_until: datetime | None = None

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        while self._error_times and self._error_times[0] < cutoff:
            self._error_times.popleft()

    @property
    def tripped(self) -> bool:
        now = datetime.now(UTC)
        if self._blocked_until is not None:
            if now < self._blocked_until:
                return True
            self._blocked_until = None
        self._prune(now)
        return len(self._error_times) >= self._error_limit

    def record_error(self, *, blocked: bool = False) -> None:
        now = datetime.now(UTC)
        if blocked:
            self._blocked_until = now + self._window
            return
        self._prune(now)
        self._error_times.append(now)


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
        # Shared across every fetch_*() method that touches CelesTrak/SATNOGS
        # for this TLEManager's lifetime -- see _ErrorCountBreaker's
        # docstring for why these must NOT be recreated fresh per call.
        self._celestrak_breaker = _ErrorCountBreaker(_CELESTRAK_CATNR_ERROR_LIMIT)
        self._satnogs_breaker = _ErrorCountBreaker(_SATNOGS_TLE_ERROR_LIMIT)
        # See _fetch_satnogs_bulk_tles()'s docstring.
        self._satnogs_bulk_cache: tuple[datetime, dict[int, dict[str, Any]]] | None = None

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

    async def is_celestrak_bulk_group_reachable(self) -> bool:
        """Single fail-fast reachability probe for the CelesTrak bulk
        GROUP=... endpoint all 6 TLE_SOURCES bulk sources share (same host,
        same base URL).

        fetch_and_update() itself has no such probe -- a blocked/unreachable
        host just makes each individual call time out on its own (10-30s),
        so a caller looping it over several sources (the startup group-fetch
        chain, or the Update TLE button) shows a "Fetching..." progress
        message that never resolves into either a success or a visible
        error, indistinguishable from a hang (reported 2026-08-11, by
        analogy: fetch_active_tles()/fetch_provisional_tles() already probe
        once before their own per-satellite loops for exactly this reason).
        Callers should check this once before looping fetch_and_update()
        over multiple sources, and show a clear error + skip the loop
        entirely if it returns False, rather than silently absorbing N
        individual timeouts.
        """
        return await _probe_reachable(
            "https://celestrak.org/NORAD/elements/gp.php", {"GROUP": "STATIONS", "FORMAT": "TLE"}
        )

    async def is_satnogs_reachable(self) -> bool:
        """Single fail-fast reachability probe for db.satnogs.org, the host
        sync_satellite_names()/sync_from_satnogs()/fetch_provisional_tles()/
        fetch_active_tles()'s Phase 2 (_fetch_satnogs_bulk_tles()) all
        depend on.

        Paired with is_celestrak_bulk_group_reachable() so a caller can do
        ONE combined connectivity check up front (2026-08-11 redesign): the
        previous approach let each step discover unreachability on its own,
        one 10-30s timeout at a time, so a clear error message didn't
        appear until several minutes and multiple silently-failed steps in
        -- by then a "Fetched X/Y: Resume in 3h" message from an unrelated
        step had already overwritten it, making the error look like it
        arrived too late or not at all (2026-08-11 report). Checking both
        hosts once before starting the chain lets a caller show the error
        immediately and skip every network-dependent step in one place.
        """
        return await _probe_reachable(SATNOGS_TLE_URL, {"norad_cat_id": 25544, "format": "json"})

    async def _fetch_satnogs_bulk_tles(
        self, progress_callback: Any = None, reachable: bool | None = None
    ) -> dict[int, dict[str, Any]] | None:
        """Fetch every TLE SATNOGS knows about in one unpaginated request,
        returned as {norad_cat_id: record}.

        `progress_callback`, if given, must accept a single string message
        (matching fetch_active_tles()'s progress_callback shape) and is
        forwarded to _get_with_progress() to report download percentage --
        see that function's docstring. fetch_provisional_tles() does NOT
        forward its own progress_callback here, since that one has a
        different (done, total) shape used for a satellite count, not
        download bytes.

        `reachable`, if given (not None), lets a caller that already probed
        SATNOGS connectivity this run (is_satnogs_reachable()) skip this
        attempt entirely instead of making a second, doomed connection --
        see fetch_active_tles()'s matching parameter docstring for the full
        rationale. False is handled exactly like the breaker-tripped case
        below, except it also records a breaker block first (so the
        "_blocked" stat and retry scheduling in the caller still see it).

        Replaces what used to be two separate per-satellite network loops --
        fetch_active_tles()'s old Phase 2a (CelesTrak individual CATNR) +
        Phase 2b (SATNOGS individual per-satellite), and the entirety of
        fetch_provisional_tles() -- with a single request (2026-08-11).
        Confirmed by direct testing that `GET /api/tle/?format=json` with no
        `norad_cat_id` filter returns SATNOGS's complete TLE set unpaginated
        (~1,670 records, ~512KB at the time of testing), covering both
        regular NORAD IDs and provisional (>=90000) ones. Phase 2a existed
        specifically to catch satellites CelesTrak's own curated/active
        listings exclude (e.g. NOAA 18/19) via a direct per-satellite CATNR
        lookup -- but this bulk dump already includes exactly those
        satellites (`tle_source: "Space-Track.org"`), along with
        ORIGAMISAT-2 and ARICA-2's satnogs_source_id-routed entry, the three
        documented cases that motivated Phase 2 in the first place. So
        Phase 2a was removed rather than kept as an extra per-satellite
        fallback on top of this.

        Deliberately does NOT fall back to per-satellite queries if this
        fails -- that would reintroduce the exact "hundreds of individual
        requests" problem this method exists to eliminate. A failed fetch
        here just means this run's Phase 2 resolves nothing; the next
        scheduled run (or once the circuit breaker's window rolls over)
        tries again.

        Unlike CelesTrak's GROUP=active (documented 2h server-side cache) or
        the paginated /api/satellites/ and /api/transmitters/ endpoints this
        app already uses in bulk, SATNOGS doesn't document this unfiltered
        query as an intentional bulk mode -- a request made immediately
        after a successful one returned an HTTP 500 during testing, though
        it was stable on repeated retries a few seconds later. Treated as
        just another transient error (not a block) unless a future report
        shows otherwise.

        Cached in-instance for _SATNOGS_BULK_CACHE_TTL (10 minutes) so
        fetch_active_tles() and fetch_provisional_tles() -- which both call
        this and normally run seconds apart in the same startup sequence --
        don't download the same ~512KB payload twice. Failures are
        deliberately NOT cached: a failed fetch must not be mistaken for "no
        satellite has a TLE".

        Returns None if the request fails outright (unreachable, HTTP
        error, unparseable body) -- callers must treat that as "nothing
        resolved this run", not "every satellite genuinely has no TLE".
        """
        now = datetime.now(UTC)
        if self._satnogs_bulk_cache is not None:
            cached_at, cached_data = self._satnogs_bulk_cache
            if now - cached_at < _SATNOGS_BULK_CACHE_TTL:
                return cached_data

        breaker = self._satnogs_breaker
        if breaker.tripped:
            logger.warning("SATNOGS already blocked this session — skipping bulk TLE fetch")
            return None
        if reachable is False:
            logger.warning(
                "SATNOGS already confirmed unreachable this run — skipping bulk TLE fetch "
                "without a second connection attempt"
            )
            breaker.record_error(blocked=True)
            return None

        try:
            async with httpx.AsyncClient(timeout=60.0, headers=DEFAULT_HEADERS) as client:
                r = await _get_with_progress(
                    client, SATNOGS_TLE_URL, {"format": "json"}, "SATNOGS", progress_callback
                )
                r.raise_for_status()
                data = r.json()
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            # Can't even connect -- treated the same as an explicit 429
            # block (see fetch_active_tles()'s Phase 1 for the same
            # reasoning) so satnogs_blocked gets set and a retry gets
            # scheduled, instead of silently repeating the same fast-fail.
            logger.warning(f"SATNOGS bulk TLE fetch unreachable: {type(exc).__name__}: {exc}")
            breaker.record_error(blocked=True)
            return None
        except (httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            logger.warning(f"SATNOGS bulk TLE fetch error: {type(exc).__name__}: {exc}")
            breaker.record_error()
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning(f"SATNOGS bulk TLE fetch error: {exc}")
            # SATNOGS documents no rate-limit policy, but HTTP 429 ("Too Many
            # Requests") is the standard way a server says so anyway -- treat
            # it the same as CelesTrak's 403.
            breaker.record_error(blocked=exc.response.status_code == 429)
            return None
        except httpx.HTTPError as exc:
            logger.warning(f"SATNOGS bulk TLE fetch error: {exc}")
            breaker.record_error()
            return None
        except Exception as exc:
            logger.warning(f"SATNOGS bulk TLE fetch unexpected error: {type(exc).__name__}: {exc}")
            breaker.record_error()
            return None

        if not isinstance(data, list):
            logger.warning("SATNOGS bulk TLE fetch returned an unexpected payload shape")
            return None

        by_norad: dict[int, dict[str, Any]] = {}
        for rec in data:
            if isinstance(rec, dict) and isinstance(rec.get("norad_cat_id"), int):
                by_norad[rec["norad_cat_id"]] = rec

        self._satnogs_bulk_cache = (now, by_norad)
        return by_norad

    def _apply_no_tle_hide_or_grace(
        self,
        norad: int,
        status: str,
        no_result_since: str | None,
        now: str,
        stats: dict[str, int],
    ) -> None:
        """Shared "no TLE available for this satellite" handling used by both
        fetch_active_tles()'s Phase 2 and fetch_provisional_tles(): hide
        'unknown'/'dead' satellites immediately, otherwise track a 30-day
        grace period (shown yellow in the UI) before hiding. Always
        increments stats["no_tle"].
        """
        if status in ("unknown", "dead"):
            self._conn.execute(
                "UPDATE satellites SET is_hidden = 2, updated_at = ? WHERE norad_cat_id = ?",
                (now, norad),
            )
            stats["hidden_unknown"] += 1
        else:
            if no_result_since is None:
                self._conn.execute(
                    "UPDATE satellites SET tle_no_result_since = ?, updated_at = ?"
                    " WHERE norad_cat_id = ?",
                    (now, now, norad),
                )
            else:
                since_dt = datetime.fromisoformat(no_result_since)
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=UTC)
                if datetime.now(UTC) - since_dt > timedelta(days=30):
                    self._conn.execute(
                        "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                        " WHERE norad_cat_id = ?",
                        (now, norad),
                    )
                    stats["hidden_expired"] += 1
        stats["no_tle"] += 1

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

        if self._celestrak_breaker.tripped:
            logger.warning(
                "CelesTrak already blocked this session — skipping %s fetch", source_name
            )
            stats["errors"] = 1
            self._log_sync(source_name, stats)
            return stats

        try:
            async with httpx.AsyncClient(timeout=30.0, headers=DEFAULT_HEADERS) as client:
                r = await client.get(source["url"], params=source.get("params", {}))
                r.raise_for_status()
                text = r.text
        except httpx.HTTPStatusError as e:
            logger.warning(f"fetch error from {source_name}: {e}")
            self._celestrak_breaker.record_error(blocked=e.response.status_code == 403)
            stats["errors"] = 1
            return stats
        except httpx.HTTPError as e:
            logger.warning(f"fetch error from {source_name}: {e}")
            self._celestrak_breaker.record_error()
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
            async with httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS) as client:
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

    def is_provisional_tle_stale(self, max_age_hours: float = 12.0) -> bool:
        """Return True if the last satnogs-provisional TLE fetch is older than
        max_age_hours (or never ran).

        Mirrors is_active_tle_stale() exactly, for the same reason:
        fetch_provisional_tles() used to be called unconditionally on every
        app startup (as one step of _refresh_satellite_names_sync()), with
        no staleness check at all -- unlike fetch_active_tles(), which
        already had one. CLAUDE.md documents a 12h cadence for provisional
        TLEs, but that only actually held while the app stayed running long
        enough for the APScheduler interval job (provisional_tle_refresh)
        to fire; closing and reopening the app in quick succession made the
        startup call run every single time regardless of how recently it
        last completed (reported 2026-08-11). The default here matches that
        documented 12h interval.
        """
        row = self._conn.execute(
            "SELECT finished_at FROM sync_log WHERE sync_type = 'satnogs-provisional'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return True
        last = datetime.fromisoformat(str(row["finished_at"]))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return datetime.now(UTC) - last > timedelta(hours=max_age_hours)

    def is_source_stale(self, source_name: str) -> bool:
        """Return True if the given TLE source has never been fetched, or the
        last fetch is older than its own documented update_interval_hours
        (TLE_SOURCES).

        This used to only return True when there was no record at all (i.e.
        first run after a fresh install), on the assumption that "the
        APScheduler interval jobs handle subsequent periodic refreshes" --
        but interval jobs don't fire immediately on creation, only after a
        full interval has elapsed *from that session's startup*. A user who
        never keeps the app open that long (1-12h depending on the source)
        would never see the interval job fire even once after the initial
        sync, leaving these 6 CelesTrak groups stuck at whatever they
        looked like on first launch (the same gap already found and fixed
        for fetch_provisional_tles() and sync_from_satnogs(), reported
        2026-08-11). Sources not found in TLE_SOURCES (there shouldn't be
        any, but just in case) fall back to a conservative 24h.
        """
        row = self._conn.execute(
            "SELECT finished_at FROM sync_log WHERE sync_type = ? ORDER BY id DESC LIMIT 1",
            (source_name,),
        ).fetchone()
        if row is None:
            return True
        last = datetime.fromisoformat(str(row["finished_at"]))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        max_age_hours = next(
            (float(s["update_interval_hours"]) for s in TLE_SOURCES if s["name"] == source_name),
            24.0,
        )
        return datetime.now(UTC) - last > timedelta(hours=max_age_hours)

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

    async def fetch_active_tles(
        self,
        progress_callback: Any = None,
        celestrak_reachable: bool | None = None,
        satnogs_reachable: bool | None = None,
    ) -> dict[str, int]:
        """Fill TLE gaps for SATNOGS-registered satellites (NORAD 10000-89999).

        Phase 1 — CelesTrak GROUP=active (single request, ~16,000 satellites):
          Until 2026-08-11 this was 5 separate curated-group requests
          (satnogs/last-30-days/argos/orbcomm/spire), on the reasoning that
          GROUP=active's ~11x larger response wasn't worth downloading when
          this app only needs ~1,482 of those satellites. That trade-off
          prioritized bytes transferred over request count -- but this app's
          actual, repeated real-world problem was getting its IP firewalled
          by CelesTrak/SATNOGS, and what trips that firewall is HTTP *error*
          count in a 2h window (CelesTrak's documented policy), not bytes
          transferred. A single successful GROUP=active request contributes
          ZERO errors and resolves virtually every satellite Phase 2's
          per-satellite fallback loop used to have to chase down one at a
          time (up to 800+ individual requests in one run before this
          change) -- so switching cut this app's own contribution to that
          error budget by roughly two orders of magnitude, at the cost of a
          few extra megabytes on an endpoint this method only calls at most
          once every 24h. See _is_active_cache_not_yet_updated()'s docstring
          for the one caveat this introduces (GROUP=active's own ~2h
          server-side cache).

        Phase 2 — one SATNOGS bulk request (_fetch_satnogs_bulk_tles()) for
        whatever GROUP=active didn't cover. Until 2026-08-11 this was two
        separate per-satellite loops (CelesTrak individual CATNR, then
        SATNOGS individual per-satellite), each capable of hundreds of
        requests in one run. SATNOGS's bulk TLE dump already includes the
        satellites that loop existed to catch (CelesTrak "active"-excluded
        satellites like NOAA 18/19, and anything routed via
        satnogs_source_id) since SATNOGS's own data comes from Space-Track
        for these — see _fetch_satnogs_bulk_tles()'s docstring for the full
        rationale and how that was confirmed. With Phase 1 now resolving
        nearly everything, Phase 2 should typically have very little left
        to do, and unlike before, doesn't grow into hundreds of requests
        even when it does.

        `progress_callback`, if given, is called with a short human-readable
        string at each phase transition so a caller updating a UI status
        label can show that this is still working, not stuck.

        `celestrak_reachable`/`satnogs_reachable`, if given (not None), let a
        caller that has already probed connectivity this run (e.g. via
        is_celestrak_bulk_group_reachable()/is_satnogs_reachable()) tell each
        phase not to bother trying again. Without this, Phase 1/Phase 2 only
        know to skip once their own breaker has tripped from an earlier
        failure *this session* -- so on the very first call of a session,
        each phase would still make its own doomed connection attempt to a
        host a caller already just confirmed is unreachable, surfacing
        confusing "CelesTrak active..."/"SATNOGS: downloading..." progress
        messages moments after a "Cannot connect to X" message (2026-08-13
        report). Passing False here makes that phase behave exactly as if
        its own attempt had failed with a connection error (records a
        breaker block, sets the corresponding "_blocked" stat) without
        actually making a second network round trip. Leave at None (default)
        for callers with no such probe (e.g. the periodic APScheduler job),
        which keeps the existing breaker-only behaviour.

        New satellite records are never created; only existing satellites are updated.
        Manual TLEs are never overwritten.
        Existing tle_group values are preserved on UPDATE.

        Returns:
            {"inserted": N, "updated": N, "revived": N, "no_tle": N, "hidden_unknown": N,
             "hidden_expired": N, "errors": N, "celestrak_blocked": 0|1,
             "satnogs_blocked": 0|1, "phase2_total": N,
             "phase2_unresolved": N}. The two "_blocked" flags are 1 when
            that phase's _ErrorCountBreaker tripped (or the Phase 2 bulk
            fetch failed outright) — callers can use this to schedule a
            later retry instead of assuming every unresolved satellite this
            run genuinely has no TLE anywhere. "phase2_total" is how many
            satellites needed Phase 2 this run (0 if Phase 1 covered
            everything); "phase2_unresolved" is how many of those SATNOGS's
            bulk dump didn't have an entry for — a caller can show
            "Fetched {phase2_total - phase2_unresolved}/{phase2_total}"
            alongside a "_blocked" flag to tell the user how far a paused
            run got. "revived" counts satellites this run un-hid (is_hidden
            2 -> 0) after a TLE resolved again for a satellite this method
            itself had previously auto-hidden via the 30-day grace period —
            see the Phase 2 candidate query's is_hidden IN (0, 2) and
            _store_resolved()'s revive step.
        """
        stats: dict[str, int] = {
            "inserted": 0,
            "updated": 0,
            "revived": 0,
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
        async with httpx.AsyncClient(timeout=60.0, headers=DEFAULT_HEADERS) as client:
            if self._celestrak_breaker.tripped:
                logger.warning(
                    "CelesTrak already blocked this session — skipping Phase 1 (GROUP=active)"
                )
            elif celestrak_reachable is False:
                logger.warning(
                    "CelesTrak already confirmed unreachable this run — skipping Phase 1 "
                    "(GROUP=active) without a second connection attempt"
                )
                self._celestrak_breaker.record_error(blocked=True)
            else:
                if progress_callback:
                    progress_callback("CelesTrak: fetching active TLEs...")
                try:
                    r = await _get_with_progress(
                        client,
                        url_ct,
                        {"GROUP": "active", "FORMAT": "TLE"},
                        "CelesTrak",
                        progress_callback,
                    )
                    r.raise_for_status()
                    _process_tle_text(r.text, "celestrak-active")
                except (httpx.ConnectTimeout, httpx.ConnectError):
                    # Can't even connect -- CelesTrak's own abuse-protection
                    # firewall silently drops connections rather than
                    # returning a 403 (confirmed against a real
                    # firewall-blocked IP, see CLAUDE.md's "SATNOGS・
                    # CelesTrakに接続できない時は..."). Treated the same as
                    # an explicit 403 so celestrak_blocked gets set below and
                    # _schedule_active_tle_retry_if_blocked() (main_window.py)
                    # has something to trigger the 3h backoff on, instead of
                    # silently repeating the same fast-fail every 24h.
                    logger.warning("CelesTrak unreachable — GROUP=active fetch failed to connect")
                    stats["errors"] += 1
                    self._celestrak_breaker.record_error(blocked=True)
                except (httpx.ReadTimeout, httpx.RemoteProtocolError):
                    # Connected fine, just slow/dropped mid-response -- an
                    # individual failure, not evidence of a block.
                    stats["errors"] += 1
                    self._celestrak_breaker.record_error()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 403 and _is_active_cache_not_yet_updated(
                        exc.response.text
                    ):
                        logger.info(
                            "CelesTrak GROUP=active cache hasn't refreshed since our last "
                            "download (< ~2h ago) — nothing new to fetch this run"
                        )
                    else:
                        logger.warning(f"active fetch error (GROUP=active): {exc}")
                        if exc.response.status_code == 403:
                            # Diagnostic only (2026-08-11): a 403 here *should*
                            # usually be the cache-not-yet-updated response
                            # above (GROUP=active only refreshes ~every 2h,
                            # and a single request right after a success is
                            # nowhere near the 50-errors/2h abuse threshold).
                            # When the check above doesn't match, log the raw
                            # body once so a real occurrence gives evidence
                            # for whether CelesTrak changed the wording or
                            # this genuinely is a block, instead of having to
                            # guess after the fact with nothing to go on
                            # (confirmed gap: a real 2026-08-11 run hit this
                            # exact branch and the body was never captured).
                            logger.warning(
                                "GROUP=active 403 body did not match the "
                                "cache-not-yet-updated check: %r",
                                exc.response.text[:500],
                            )
                        stats["errors"] += 1
                        self._celestrak_breaker.record_error(
                            blocked=exc.response.status_code == 403
                        )
                except httpx.HTTPError as exc:
                    logger.warning(f"active fetch error (GROUP=active): {exc}")
                    stats["errors"] += 1
                    self._celestrak_breaker.record_error()

            stats["celestrak_blocked"] = int(self._celestrak_breaker.tripped)

        self._conn.commit()

        # ── Phase 2: fill gaps Phase 1's GROUP=active didn't cover ────────
        # Targets two groups: satellites with no TLE row at all, AND satellites
        # whose current TLE was itself obtained via Phase 2 (source='satnogs').
        # The latter is required — otherwise a satellite Phase 1 doesn't cover
        # gets exactly one TLE ever (whichever run first discovers it) and is
        # then silently excluded from every subsequent run merely for already
        # having a tle_data row, no matter how stale it becomes. Confirmed
        # stuck this way for 44 days for ORIGAMISAT-2 / NORAD 68795
        # (2026-08-09) before this fix, alongside ~700 other satellites
        # across three earlier one-shot Phase 2 runs.
        # source='celestrak' rows are left alone here: they're kept fresh by
        # the periodic group-specific fetches (2h/4h/6h/12h) and by Phase 1
        # above, so re-querying them here would be redundant load.
        # source='manual' rows are never touched by any automated sync.
        #
        # Resolved via one SATNOGS bulk request (_fetch_satnogs_bulk_tles()),
        # not a per-satellite loop — see that method's docstring for why a
        # separate CelesTrak-only fallback stage (formerly "Phase 2a") turned
        # out to be unnecessary. Stored with source='satnogs' regardless of
        # which upstream provider SATNOGS itself sourced the data from — the
        # tle_data.source CHECK constraint has no 'celestrak-catnr' value, and
        # semantically this just means "keep retrying a satellite Phase 1
        # doesn't cover", which is exactly what the 'satnogs' tag already
        # means to this method's own WHERE clause below.
        #
        # Satellites migrated from a provisional (>=90000) ID retain
        # satnogs_source_id pointing at the old ID, and SATNOGS's bulk dump
        # can keep serving the TLE keyed by that old ID for a long time after
        # migration (observed for ARICA-2 / NORAD 68796, 2026-07-12) —
        # queried by satnogs_source_id when present, mirroring the routing
        # already used for transmitter sync in sync_from_satnogs().
        # ORDER BY prioritizes satellites with no TLE at all (t.fetched_at IS
        # NULL, sorts first since SQLite treats NULL < any value and the
        # boolean expression evaluates NULL as 0), then the ones with just a
        # stale source='satnogs' TLE, oldest-fetched-first. This mattered when
        # a slow per-satellite loop might not drain a list of hundreds in one
        # run; with a single bulk fetch resolving everything locally in one
        # pass, the ordering is no longer load-bearing but is kept for the
        # "Fetched X/Y" status message to still make intuitive sense.
        #
        # is_hidden IN (0, 2) — not just 0 — so a satellite this method itself
        # auto-hid via the 30-day no-TLE grace period (_apply_no_tle_hide_or_
        # grace()) gets reconsidered every run instead of being excluded
        # forever the moment it's hidden once. Before this, hiding was a
        # one-way ratchet: ARICA-2 / NORAD 68796 sat hidden from mid-July
        # 2026 onward even though SATNOGS's bulk TLE dump (keyed by its old
        # provisional ID via satnogs_source_id, same routing as above) had a
        # perfectly resolvable, freshly-updated TLE the whole time — nothing
        # ever looked again once is_hidden=0 was required to even be a
        # candidate (2026-08-13 user report; DB comparison against a
        # longer-running dev install showed ~93 satellites stuck the same
        # way). is_hidden=1 (user-hidden) is excluded by construction — it's
        # not in (0, 2). Migration remnants (a norad_cat_id some other row's
        # satnogs_source_id already points at, e.g. 68796's old provisional
        # ID 98329) are excluded explicitly — those must stay hidden forever
        # regardless of TLE availability, see _run_migration_pipeline() step
        # 7. status='dead' is excluded too: a satellite SATNOGS has confirmed
        # non-operational shouldn't reappear just because some catalog still
        # has decay-era orbital elements for it.
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
                WHERE s.is_hidden IN (0, 2)
                  AND s.status != 'dead'
                  AND s.norad_cat_id BETWEEN 10000 AND 89999
                  AND (t.norad_cat_id IS NULL OR t.source = 'satnogs')
                  AND s.norad_cat_id NOT IN (
                      SELECT satnogs_source_id FROM satellites
                      WHERE satnogs_source_id IS NOT NULL
                  )
                ORDER BY t.fetched_at IS NOT NULL, t.fetched_at ASC
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
                # Un-hide a satellite that had been auto-hidden after 30 days
                # of no TLE (see _apply_no_tle_hide_or_grace()) now that a TLE
                # has resolved again. is_hidden=1 (user-hidden) is never a
                # candidate here (see the WHERE clause above), so this only
                # ever reverses this method's own earlier hide.
                revive_cursor = self._conn.execute(
                    "UPDATE satellites SET is_hidden = 0, tle_no_result_since = NULL"
                    " WHERE norad_cat_id = ? AND is_hidden = 2",
                    (norad,),
                )
                if revive_cursor.rowcount > 0:
                    stats["revived"] += 1
                else:
                    self._conn.execute(
                        "UPDATE satellites SET tle_no_result_since = NULL WHERE norad_cat_id = ?",
                        (norad,),
                    )
                if had_tle:
                    stats["updated"] += 1
                else:
                    stats["inserted"] += 1
                return True

            # ── Phase 2: SATNOGS bulk TLE fallback ──────────────────────────
            if progress_callback:
                progress_callback(
                    f"SATNOGS: fetching TLE data for {len(remaining)} satellite(s)..."
                )

            bulk = await self._fetch_satnogs_bulk_tles(
                progress_callback=progress_callback, reachable=satnogs_reachable
            )
            if bulk is None:
                logger.warning(
                    "SATNOGS bulk TLE fetch failed — %d satellite(s) unresolved this run",
                    len(remaining),
                )
                stats["errors"] += len(remaining)
                stats["satnogs_blocked"] = int(self._satnogs_breaker.tripped)
                if progress_callback:
                    progress_callback(
                        f"SATNOGS unavailable — {len(remaining)} satellite(s) deferred"
                    )
            else:
                for norad, (name, status, nrs, source_id, _tle_group, _had_tle) in list(
                    remaining.items()
                ):
                    query_id = source_id if source_id is not None else norad
                    record = bulk.get(query_id)
                    if not record or "tle1" not in record:
                        self._apply_no_tle_hide_or_grace(norad, status, nrs, now, stats)
                        continue

                    line1: str = str(record["tle1"])
                    line2: str = str(record["tle2"])
                    try:
                        sat_obj = EarthSatellite(line1, line2, name, self._ts)
                        epoch_dt = sat_obj.epoch.utc_datetime()
                    except Exception as exc:
                        logger.warning(f"SATNOGS TLE parse error {norad}: {exc}")
                        stats["errors"] += 1
                        continue

                    _store_resolved(norad, name, line1, line2, epoch_dt)
                self._conn.commit()

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

        async with httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS) as client:
            for idx, row in enumerate(rows):
                if self._celestrak_breaker.tripped:
                    logger.warning(
                        "CelesTrak already blocked this session — stopping legacy TLE "
                        "check early (%d satellite(s) not checked)",
                        len(rows) - idx,
                    )
                    stats["errors"] += len(rows) - idx
                    break
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
                    self._celestrak_breaker.record_error()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        # 404 = CelesTrak no longer tracks this satellite → hide it.
                        # Still counts against CelesTrak's own error budget even
                        # though it's an expected, non-exceptional outcome here.
                        self._conn.execute(
                            "UPDATE satellites SET is_hidden = 2, updated_at = ?"
                            " WHERE norad_cat_id = ?",
                            (now, norad),
                        )
                        stats["hidden"] += 1
                        self._celestrak_breaker.record_error()
                    else:
                        logger.warning(f"legacy TLE fetch error for {norad}: {exc}")
                        stats["errors"] += 1
                        self._celestrak_breaker.record_error(
                            blocked=exc.response.status_code == 403
                        )
                except httpx.HTTPError as exc:
                    logger.warning(f"legacy TLE fetch error for {norad}: {exc}")
                    stats["errors"] += 1
                    self._celestrak_breaker.record_error()
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

        async with httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS) as client:
            for idx, norad in enumerate(targets):
                if self._celestrak_breaker.tripped:
                    logger.warning(
                        "CelesTrak already blocked this session — stopping METEOR TLE "
                        "check early (%d satellite(s) not checked)",
                        len(targets) - idx,
                    )
                    stats["errors"] += len(targets) - idx
                    break
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
                    self._celestrak_breaker.record_error()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        stats["skipped"] += 1
                        self._celestrak_breaker.record_error()
                    else:
                        logger.warning(f"meteor TLE fetch error for {norad}: {exc}")
                        stats["errors"] += 1
                        self._celestrak_breaker.record_error(
                            blocked=exc.response.status_code == 403
                        )
                except httpx.HTTPError as exc:
                    logger.warning(f"meteor TLE fetch error for {norad}: {exc}")
                    stats["errors"] += 1
                    self._celestrak_breaker.record_error()
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
        """Fetch TLEs for provisional (NORAD >= 90000) satellites from SATNOGS's bulk TLE dump.

        For each provisional-NORAD (>= 90000) satellite that is either visible
        (is_hidden=0) or auto-hidden by this app's own 30-day no-TLE grace
        period (is_hidden=2; see _apply_no_tle_hide_or_grace()), looks it up
        in _fetch_satnogs_bulk_tles()'s dump, which returns the best
        available TLE regardless of whether norad_follow_id is set publicly.
        The TLE is stored under the provisional ID so the satellite's
        position can be shown on the map. Migration remnants (provisional
        IDs another satellite's satnogs_source_id already points at) and
        satellites with status='dead' are excluded — those stay hidden
        regardless of TLE availability.

        Until 2026-08-11 this queried SATNOGS individually per satellite
        (up to ~140+ requests in one run). Confirmed by testing that
        SATNOGS's bulk TLE dump includes provisional NORAD IDs alongside
        regular ones, so this now shares fetch_active_tles()'s Phase 2 bulk
        fetch (same TLEManager-wide cache — calling both close together, as
        the normal startup sequence does, downloads the ~512KB payload only
        once) instead of looping.

        When the TLE line1 contains a *different* NORAD ID (i.e. SATNOGS internally knows
        the official ID), the migration pipeline is triggered automatically if the official
        satellite record already exists in our DB.

        Returns:
            {"inserted": N, "updated": N, "revived": N, "no_tle": N,
             "hidden_unknown": N, "hidden_expired": N, "errors": N}
        """
        rows = self._conn.execute(
            """
            SELECT norad_cat_id, name, status, tle_no_result_since FROM satellites
            WHERE norad_cat_id >= 90000
              AND is_hidden IN (0, 2)
              AND status != 'dead'
              AND norad_cat_id NOT IN (
                  SELECT satnogs_source_id FROM satellites
                  WHERE satnogs_source_id IS NOT NULL
              )
            """
        ).fetchall()

        stats: dict[str, int] = {
            "inserted": 0,
            "updated": 0,
            "revived": 0,
            "no_tle": 0,
            "hidden_unknown": 0,
            "hidden_expired": 0,
            "errors": 0,
        }
        if not rows:
            return stats

        if progress_callback:
            progress_callback(0, len(rows))

        bulk = await self._fetch_satnogs_bulk_tles()
        if bulk is None:
            logger.warning(
                "SATNOGS bulk TLE fetch failed — skipping %d provisional TLE fetch(es) this run",
                len(rows),
            )
            stats["errors"] = len(rows)
            self._log_sync("satnogs-provisional", stats)
            return stats

        now = datetime.now(UTC).isoformat()

        for row in rows:
            fake_id = int(row["norad_cat_id"])
            sat_name = str(row["name"])
            sat_status = str(row["status"] or "unknown")
            no_result_since: str | None = (
                str(row["tle_no_result_since"]) if row["tle_no_result_since"] else None
            )

            record = bulk.get(fake_id)
            if not record or "tle1" not in record:
                self._apply_no_tle_hide_or_grace(fake_id, sat_status, no_result_since, now, stats)
                continue

            line1: str = str(record["tle1"])
            line2: str = str(record["tle2"])
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
            migrated = False
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
                    migrated = True

            # TLE found → clear the no-result grace-period latch if it was set
            if no_result_since is not None:
                self._conn.execute(
                    "UPDATE satellites SET tle_no_result_since = NULL WHERE norad_cat_id = ?",
                    (fake_id,),
                )

            # Un-hide a satellite that had been auto-hidden after 30 days of no
            # TLE (see _apply_no_tle_hide_or_grace()) now that a TLE resolved
            # again. Skipped when this record just got migrated to an official
            # NORAD ID above — _run_migration_pipeline() re-hides the
            # provisional side (fake_id) as a deliberate remnant, and that
            # decision must not be reversed here.
            if not migrated:
                revive_cursor = self._conn.execute(
                    "UPDATE satellites SET is_hidden = 0 WHERE norad_cat_id = ? AND is_hidden = 2",
                    (fake_id,),
                )
                if revive_cursor.rowcount > 0:
                    stats["revived"] += 1

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

        if progress_callback:
            progress_callback(len(rows), len(rows))

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
