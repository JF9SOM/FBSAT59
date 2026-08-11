"""Tests for data.tle_manager.TLEManager (fetch_active_tles(), fetch_provisional_tles()).

Lightweight (no Qt import) so it is safe to run locally, unlike test_main_window.py.
"""

from __future__ import annotations

import asyncio
import json as _json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from data.database import SCHEMA_SQL
from data.tle_manager import TLEManager, _ErrorCountBreaker

_LINE1 = "1 68796U 26088E   26192.83685747  .00002724  00000+0  17770-3 0  9997"
_LINE2 = "2 68796  97.5082 341.6669 0015331 359.2893   0.8311 15.08613790 12013"

# A second synthetic element set, keyed to a different NORAD ID.
_LINE1_B = "1 68795U 26088D   26192.91095598  .00012852  00000-0  81506-3 0  9999"
_LINE2_B = "2 68795  97.5066 327.0920 0015527  49.6354 310.6228 15.08945428  9764"


def _mk_response(
    *,
    text: str | None = None,
    json_data: Any = None,
    status_code: int = 200,
    has_content_length: bool = True,
) -> MagicMock:
    """A mock httpx.Response as returned by `async with client.stream(...) as r`
    (both Phase 1's GROUP=active fetch and Phase 2's SATNOGS bulk fetch use
    _get_with_progress(), which streams via client.stream() instead of
    client.get() so it can report download percentage). `.aiter_bytes()`
    yields the body in a few chunks so percentage-progress logic has
    something to observe, matching how a real multi-KB/MB response arrives
    over several TCP reads rather than all at once.
    """
    if text is not None:
        body = text.encode()
    elif json_data is not None:
        body = _json.dumps(json_data).encode()
    else:
        body = b""

    resp = MagicMock()
    resp.status_code = status_code
    headers: dict[str, str] = {}
    if has_content_length:
        headers["content-length"] = str(len(body))
    resp.headers = headers

    async def _aiter_bytes(chunk_size: int = 65536) -> Any:
        if not body:
            return
        n = max(1, len(body) // 3)
        for i in range(0, len(body), n):
            yield body[i : i + n]

    resp.aiter_bytes = _aiter_bytes
    if text is not None:
        resp.text = text
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)

    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"{status_code} error", request=MagicMock(), response=resp
            )
        )
    else:
        resp.raise_for_status = MagicMock(return_value=None)
    return resp


def _group_active_resp(text: str = "") -> MagicMock:
    """A successful Phase 1 (CelesTrak GROUP=active) response."""
    return _mk_response(text=text)


def _group_active_cache_stale_403() -> MagicMock:
    """GROUP=active's own ~2h server-side cache 403 -- not an abuse block,
    just "you already downloaded this within the last 2 hours".

    Body captured verbatim from a real response (2026-08-11) rather than a
    hand-written single-line string: it word-wraps with CRLF roughly every
    ~55 characters, including right in the middle of the exact phrase
    _is_active_cache_not_yet_updated() searches for. An earlier version of
    this fixture used a single unbroken line and so never reproduced that
    wrapping -- which meant this exact scenario had "passing" test coverage
    the whole time despite the real check never once matching a real
    response (every actual 403 was misclassified as a genuine block; see
    _is_active_cache_not_yet_updated()'s docstring for the 2026-08-11 fix).
    """
    return _mk_response(
        text=(
            "GP data has not updated since your last successful\r\n"
            "download of GROUP=active at 2026-08-11 10:30:04 UTC.\r\n"
            "Data is updated once every 2 hours."
        ),
        status_code=403,
    )


def _error_resp(status_code: int) -> MagicMock:
    """A response representing a real HTTP error status (403, 429, 500, ...)
    that raise_for_status() actually raises for.
    """
    return _mk_response(text="", status_code=status_code)


def _bulk_record(norad: int, line1: str, line2: str, name: str = "SAT") -> dict[str, Any]:
    """One entry as it appears in SATNOGS's bulk TLE dump
    (GET /api/tle/?format=json with no norad_cat_id filter) -- confirmed by
    live testing 2026-08-11 to include both regular and provisional
    (>=90000) NORAD IDs in one flat, unpaginated list.
    """
    return {
        "tle0": f"0 {name}",
        "tle1": line1,
        "tle2": line2,
        "tle_source": "Space-Track.org",
        "norad_cat_id": norad,
    }


def _satnogs_bulk_resp(records: list[dict[str, Any]]) -> MagicMock:
    """A successful SATNOGS bulk TLE dump response."""
    return _mk_response(json_data=records)


def _stream_ctx(response: MagicMock) -> MagicMock:
    """A mock for the async context manager `client.stream(...)` returns."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _stream_items(*items: MagicMock | BaseException) -> list[Any]:
    """Builds a `side_effect` list for `mock_client.stream`, one entry per
    call to `client.stream(...)`. Each item is either a response (wrapped
    into the context manager `client.stream()` really returns) or a raw
    exception instance -- MagicMock raises exception items directly when
    `.stream()` is *called*, which for our purposes is indistinguishable
    from httpx raising the same exception while connecting (inside
    `__aenter__`), since either way it propagates out of the same
    `async with client.stream(...) as r:` statement in
    _get_with_progress().
    """
    return [item if isinstance(item, BaseException) else _stream_ctx(item) for item in items]


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _patched_client(mock_client: AsyncMock) -> Any:
    """Context manager patching data.tle_manager.httpx.AsyncClient so every
    `async with httpx.AsyncClient(...) as x` in the module (regardless of
    how many separate call sites use it) resolves to the same mock client,
    matching how the real code shares connections within a phase but not
    across phases -- from the mock's point of view every AsyncClient(...)
    call returns the same object either way.
    """
    return patch("data.tle_manager.httpx.AsyncClient")


def _wire_mock_client(mock_cls: MagicMock, mock_client: AsyncMock) -> None:
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)


class TestFetchActiveTlesPhase1UsesGroupActive:
    """2026-08-11: Phase 1 switched from 5 separate curated-group requests
    to a single GROUP=active request -- what actually trips CelesTrak's
    firewall is HTTP error count in a 2h window, and a single successful
    request contributes zero errors regardless of how much data it returns.
    """

    def test_phase1_requests_group_active(self, db: sqlite3.Connection) -> None:
        # A satellite with an existing celestrak-sourced TLE never enters
        # refresh_targets, so Phase 2 makes no calls of its own -- this test
        # only cares about Phase 1's own single request.
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (25544, 'ISS', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data"
            " (norad_cat_id, name, line1, line2, epoch, source, tle_group, fetched_at,"
            "  quality_score)"
            " VALUES (25544, 'ISS', ?, ?, '2026-06-26T21:51:46+00:00', 'celestrak',"
            "  'stations', '2026-06-27T07:34:09+00:00', 'poor')",
            (_LINE1, _LINE2),
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=_stream_ctx(_group_active_resp()))

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles())

        assert mock_client.stream.call_count == 1
        call = mock_client.stream.call_args_list[0]
        assert call.kwargs["params"] == {"GROUP": "active", "FORMAT": "TLE"}

    def test_403_with_cache_not_updated_message_is_not_treated_as_blocked(
        self, db: sqlite3.Connection
    ) -> None:
        """GROUP=active's own ~2h server-side cache returns a 403 with a
        specific explanatory body when re-requested too soon -- this must
        NOT trip the circuit breaker or set celestrak_blocked, or pressing
        "Update TLE" twice within 2h would falsely look like an abuse block.
        Phase 2 (SATNOGS, an entirely independent breaker) must still run
        normally afterward.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_cache_stale_403(),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 0
        assert stats["errors"] == 0
        assert stats["updated"] + stats["inserted"] == 1
        assert not mgr._celestrak_breaker.tripped

    def test_403_without_cache_message_is_still_treated_as_blocked(
        self, db: sqlite3.Connection
    ) -> None:
        """A 403 that doesn't carry the specific "cache not updated" wording
        is a real abuse-protection signal and must trip the CelesTrak
        breaker -- but Phase 2's SATNOGS bulk fetch uses an entirely
        separate breaker, so it must still run and can still resolve the
        satellite.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _error_resp(403),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 1
        assert mgr._celestrak_breaker.tripped
        assert stats["updated"] + stats["inserted"] == 1

    def test_connect_timeout_is_treated_as_blocked(self, db: sqlite3.Connection) -> None:
        """A silently-dropped connection (no HTTP response at all) is
        CelesTrak's actual real-world abuse-protection behavior more often
        than an explicit 403 -- must be treated the same way.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (25544, 'ISS', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=httpx.ConnectTimeout("boom"))

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 1
        assert mgr._celestrak_breaker.tripped


class TestFetchActiveTlesSatnogsSourceIdRouting:
    """A satellite migrated from a provisional (>=90000) NORAD ID retains
    satnogs_source_id pointing at the old ID. SATNOGS's bulk TLE dump can
    keep serving the TLE keyed by that old ID long after migration (observed
    for ARICA-2 / NORAD 68796, 2026-07-12), so Phase 2 must look it up by
    satnogs_source_id when present, not by the real NORAD ID.
    """

    def test_looks_up_by_satnogs_source_id_when_present(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden, satnogs_source_id)"
            " VALUES (68796, 'ARICA-2', 'alive', 0, 98329)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(98329, _LINE1, _LINE2, "ARICA-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles())

        # The TLE result must be stored under the real NORAD ID even though
        # it was looked up under the provisional one.
        row = db.execute("SELECT line1, line2 FROM tle_data WHERE norad_cat_id = 68796").fetchone()
        assert row is not None
        assert row["line1"] == _LINE1
        assert row["line2"] == _LINE2

    def test_falls_back_to_own_norad_id_when_no_source_id(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (43803, 'JO-97', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(43803, _LINE1, _LINE2, "JO-97")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles())

        row = db.execute("SELECT line1 FROM tle_data WHERE norad_cat_id = 43803").fetchone()
        assert row is not None
        assert row["line1"] == _LINE1


class TestFetchActiveTlesPhase2RefreshesStaleSatnogsTles:
    """Regression coverage for the "permanent staleness ratchet" bug: a
    satellite not tracked by CelesTrak's GROUP=active used to get exactly
    one TLE ever (whichever run first discovered it, via source='satnogs'),
    then be silently excluded from every later run merely for already
    having a tle_data row — no matter how stale (confirmed for
    ORIGAMISAT-2 / NORAD 68795, stuck 44 days, 2026-08-09).
    """

    def test_resolves_a_stale_satnogs_sourced_satellite(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data"
            " (norad_cat_id, name, line1, line2, epoch, source, tle_group, fetched_at,"
            "  quality_score)"
            " VALUES (68795, 'OrigamiSat-2', ?, ?, '2026-06-26T21:51:46+00:00', 'satnogs',"
            "  'cubesat', '2026-06-27T07:34:09+00:00', 'poor')",
            (_LINE1_B, _LINE2_B),
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert mock_client.stream.call_count == 2
        row = db.execute(
            "SELECT fetched_at, tle_group, source FROM tle_data WHERE norad_cat_id = 68795"
        ).fetchone()
        assert row["fetched_at"] != "2026-06-27T07:34:09+00:00"
        assert row["tle_group"] == "cubesat"  # preserved, not reset to 'amateur'
        assert row["source"] == "satnogs"  # tag kept even though CelesTrak may have supplied it
        assert stats["updated"] == 1
        assert stats["inserted"] == 0

    def test_resolves_multiple_satellites_from_one_bulk_response(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68796, 'ARICA-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp(
                    [
                        _bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2"),
                        _bulk_record(68796, _LINE1, _LINE2, "ARICA-2"),
                    ]
                ),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        # One bulk request resolves both -- not one request per satellite.
        assert mock_client.stream.call_count == 2
        assert stats["updated"] + stats["inserted"] == 2
        for norad in (68795, 68796):
            row = db.execute(
                "SELECT line1 FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            assert row is not None

    def test_satellite_missing_from_bulk_dump_gets_grace_period(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(_group_active_resp(), _satnogs_bulk_resp([]))
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["no_tle"] == 1
        row = db.execute(
            "SELECT tle_no_result_since FROM satellites WHERE norad_cat_id = 68795"
        ).fetchone()
        assert row["tle_no_result_since"] is not None

    def test_celestrak_sourced_tle_is_not_requeried_by_phase2(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (25544, 'ISS', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data"
            " (norad_cat_id, name, line1, line2, epoch, source, tle_group, fetched_at,"
            "  quality_score)"
            " VALUES (25544, 'ISS', ?, ?, '2026-06-26T21:51:46+00:00', 'celestrak',"
            "  'stations', '2026-06-27T07:34:09+00:00', 'poor')",
            (_LINE1, _LINE2),
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=_stream_items(_group_active_resp()))

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles())

        # Only the Phase 1 GROUP=active request -- no Phase 2 call at all,
        # since this satellite never enters refresh_targets.
        assert mock_client.stream.call_count == 1

    def test_manual_tle_is_not_requeried_by_phase2(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data"
            " (norad_cat_id, name, line1, line2, epoch, source, tle_group, fetched_at,"
            "  quality_score)"
            " VALUES (68795, 'OrigamiSat-2', ?, ?, '2026-06-26T21:51:46+00:00', 'manual',"
            "  'amateur', '2026-06-27T07:34:09+00:00', 'poor')",
            (_LINE1, _LINE2),
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=_stream_items(_group_active_resp()))

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles())

        assert mock_client.stream.call_count == 1


class TestFetchActiveTlesPhase2TargetSelection:
    """Phase 2 must target both satellites with no TLE at all and satellites
    whose only TLE is a stale source='satnogs' one. Before 2026-08-11 this
    also controlled per-satellite fetch *order* (mattered when Phase 2 was a
    per-satellite loop that might not drain a long queue in one run); with a
    single bulk fetch resolving everything locally in one pass, order is no
    longer behaviorally significant -- this only checks both kinds of
    targets get resolved, and that celestrak-sourced rows are left alone.
    """

    def test_no_tle_and_stale_satnogs_tle_both_get_resolved(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (10001, 'Sat A (no TLE)', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (10002, 'Sat B (old TLE)', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data (norad_cat_id, name, line1, line2, source, fetched_at)"
            " VALUES (10002, 'Sat B', ?, ?, 'satnogs', '2020-01-01T00:00:00+00:00')",
            (_LINE1, _LINE2),
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (10003, 'Sat C (celestrak TLE)', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data (norad_cat_id, name, line1, line2, source, fetched_at)"
            " VALUES (10003, 'Sat C', ?, ?, 'celestrak', '2026-08-01T00:00:00+00:00')",
            (_LINE1_B, _LINE2_B),
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp(
                    [
                        _bulk_record(10001, _LINE1_B, _LINE2_B, "Sat A"),
                        _bulk_record(10002, _LINE1_B, _LINE2_B, "Sat B"),
                    ]
                ),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["inserted"] == 1  # 10001
        assert stats["updated"] == 1  # 10002
        row_a = db.execute("SELECT line1 FROM tle_data WHERE norad_cat_id = 10001").fetchone()
        assert row_a["line1"] == _LINE1_B
        row_b = db.execute(
            "SELECT line1, fetched_at FROM tle_data WHERE norad_cat_id = 10002"
        ).fetchone()
        assert row_b["line1"] == _LINE1_B
        assert row_b["fetched_at"] != "2020-01-01T00:00:00+00:00"
        # 10003 (celestrak-sourced) was never in refresh_targets -- untouched.
        row_c = db.execute("SELECT line1 FROM tle_data WHERE norad_cat_id = 10003").fetchone()
        assert row_c["line1"] == _LINE1_B


class TestFetchActiveTlesProgressCallback:
    """The optional progress_callback must fire at each phase transition so
    a caller can show that work is still happening (2026-08-10: this method
    used to run silently for however long its old per-satellite Phase 2
    loop took, which looked identical to a hang).
    """

    def test_progress_callback_fires_for_each_phase(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        messages: list[str] = []

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles(progress_callback=messages.append))

        assert messages == [
            "CelesTrak active...",
            "SATNOGS: 1 satellite(s)...",
            "SATNOGS: downloading... 33%",
            "SATNOGS: downloading... 66%",
            "SATNOGS: downloading... 100%",
        ]

    def test_no_phase2_message_when_phase1_covers_everything(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (25544, 'ISS', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data"
            " (norad_cat_id, name, line1, line2, epoch, source, tle_group, fetched_at,"
            "  quality_score)"
            " VALUES (25544, 'ISS', ?, ?, '2026-06-26T21:51:46+00:00', 'celestrak',"
            "  'stations', '2026-06-27T07:34:09+00:00', 'poor')",
            (_LINE1, _LINE2),
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=_stream_ctx(_group_active_resp()))

        messages: list[str] = []

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles(progress_callback=messages.append))

        assert messages == ["CelesTrak active..."]


class TestGetWithProgressDownloadPercentage:
    """_get_with_progress() streams the response body (instead of a plain
    client.get()) so it can report download percentage as a bulk fetch
    (Phase 1's GROUP=active, Phase 2's SATNOGS bulk dump) arrives, rather
    than showing nothing until the whole multi-KB/MB body has landed.
    """

    def test_reports_whole_percent_steps_from_content_length(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        messages: list[str] = []

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles(progress_callback=messages.append))

        download_messages = [m for m in messages if "downloading..." in m and "%" in m]
        assert download_messages == [
            "SATNOGS: downloading... 33%",
            "SATNOGS: downloading... 66%",
            "SATNOGS: downloading... 100%",
        ]

    def test_no_content_length_falls_back_to_a_single_message(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        satnogs_resp = _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")])
        satnogs_resp.headers = {}  # no content-length header at all
        mock_client.stream = MagicMock(
            side_effect=_stream_items(_group_active_resp(), satnogs_resp)
        )

        messages: list[str] = []

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_active_tles(progress_callback=messages.append))

        download_messages = [m for m in messages if "downloading" in m]
        # Exactly one fallback message, no percentage -- not one per chunk.
        assert download_messages == ["SATNOGS: downloading..."]

    def test_no_progress_callback_does_not_crash(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())  # no progress_callback

        assert stats["updated"] + stats["inserted"] == 1


class TestGetWithProgressResponseIsReadable:
    """Regression test for a real bug (2026-08-11): _get_with_progress()
    iterated response.aiter_bytes() to count downloaded bytes but discarded
    the chunks, on the mistaken assumption that httpx caches .content once a
    streamed body is fully iterated -- it doesn't; only .aread() does that.
    The other tests in this file mock httpx.AsyncClient with MagicMock,
    whose .text/.json() are plain attributes that never raise, so they could
    not have caught this: production hit it live (GROUP=active's 403 body
    and the SATNOGS bulk dump's 200 OK body both raised
    httpx.ResponseNotRead downstream, silently discarding two otherwise-
    successful fetches). These tests instead round-trip through a real
    httpx.AsyncClient backed by httpx.MockTransport -- a real Response
    object with real stream-consumption semantics, still fully offline (no
    socket is ever opened).

    Important: the handler must give httpx.Response() its body as an
    async-generator `content=`, not a plain `bytes` `content=`. Passing raw
    bytes makes httpx populate `_content` immediately at construction time
    (there is nothing to stream), which means the response looks "already
    read" from the start regardless of whether _get_with_progress() sets
    `_content` itself -- exactly the false negative that let a first draft
    of this test pass against the unfixed function. An async-generator body
    is what actually reproduces "the caller must consume the stream before
    .text/.json() work", matching how a real HTTP response arrives.
    """

    def test_success_response_text_and_json_readable_after_return(self) -> None:
        from data.tle_manager import _get_with_progress

        body = b'{"ok": true}'

        async def body_stream() -> Any:
            yield body[:4]
            yield body[4:]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=body_stream(), headers={"content-length": str(len(body))}
            )

        async def run() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await _get_with_progress(client, "https://example.invalid/x", {}, "T", None)

        response = asyncio.run(run())

        # Both would raise httpx.ResponseNotRead if _content were never set.
        assert response.text == '{"ok": true}'
        assert response.json() == {"ok": True}

    def test_error_response_text_readable_via_raise_for_status_handler(self) -> None:
        """Mirrors the exact production failure: a 403 with a body that
        callers need to inspect (e.g. _is_active_cache_not_yet_updated())
        inside an `except httpx.HTTPStatusError as exc:` handler.
        """
        from data.tle_manager import _get_with_progress

        body = b"quota exceeded, try again later"

        async def body_stream() -> Any:
            yield body[:10]
            yield body[10:]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, content=body_stream(), headers={"content-length": str(len(body))}
            )

        async def run() -> str:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                response = await _get_with_progress(
                    client, "https://example.invalid/x", {}, "T", None
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    return exc.response.text  # raised ResponseNotRead before the fix
                raise AssertionError("expected raise_for_status() to raise")

        assert asyncio.run(run()) == "quota exceeded, try again later"


class TestIsActiveCacheNotYetUpdated:
    """Regression coverage for a real bug (2026-08-11): the check never
    matched a real GROUP=active 403 body, because that body word-wraps with
    CRLF roughly every ~55 characters -- including right in the middle of
    the exact phrase being searched for ("...your last successful\r\ndownload
    of GROUP=active..."). A plain substring check can't match text split
    across a line break, so every real cache-not-yet-updated 403 was
    silently misclassified as a genuine block. Confirmed live: a real run's
    diagnostic log captured the actual body and showed the mismatch
    directly. Fixed by collapsing whitespace runs before matching.
    """

    def test_matches_the_real_word_wrapped_response_body(self) -> None:
        from data.tle_manager import _is_active_cache_not_yet_updated

        # Captured verbatim from a real 403 response, 2026-08-11.
        body = (
            "GP data has not updated since your last successful\r\n"
            "download of GROUP=active at 2026-08-11 10:30:04 UTC.\r\n"
            "Data is updated once every 2 hours."
        )
        assert _is_active_cache_not_yet_updated(body) is True

    def test_matches_a_single_line_variant_too(self) -> None:
        from data.tle_manager import _is_active_cache_not_yet_updated

        body = "GP data has not updated since your last successful download of GROUP=active at ..."
        assert _is_active_cache_not_yet_updated(body) is True

    def test_matches_regardless_of_case(self) -> None:
        from data.tle_manager import _is_active_cache_not_yet_updated

        body = "gp data HAS NOT UPDATED since your last successful\r\ndownload of group=active..."
        assert _is_active_cache_not_yet_updated(body) is True

    def test_does_not_match_an_unrelated_403_body(self) -> None:
        from data.tle_manager import _is_active_cache_not_yet_updated

        assert _is_active_cache_not_yet_updated("Access denied.") is False
        assert _is_active_cache_not_yet_updated("") is False


class TestErrorCountBreaker:
    """Unit coverage for the low-level breaker shared across every fetch_*()
    method that touches CelesTrak/SATNOGS on a given TLEManager instance --
    see CLAUDE.md's "fetch_active_tles() の2フェーズ設計" for the incident
    (a real run's log showed a clean mix of 200/404 responses for ~400
    satellites, then 403 on every single remaining request for the rest
    of an 846-satellite run) that motivated this.
    """

    def test_not_tripped_before_reaching_the_limit(self) -> None:
        breaker = _ErrorCountBreaker(error_limit=3)
        breaker.record_error()
        breaker.record_error()
        assert breaker.tripped is False

    def test_trips_once_the_limit_is_reached(self) -> None:
        breaker = _ErrorCountBreaker(error_limit=3)
        breaker.record_error()
        breaker.record_error()
        breaker.record_error()
        assert breaker.tripped is True

    def test_blocked_trips_immediately_regardless_of_count(self) -> None:
        """A single HTTP 403 (CelesTrak) / 429 (SATNOGS) is proof the block
        has already begun -- no reason to spend more of the error budget
        confirming it, even with a high numeric limit still far off."""
        breaker = _ErrorCountBreaker(error_limit=50)
        breaker.record_error(blocked=True)
        assert breaker.tripped is True

    def test_blocked_stays_tripped_for_the_rest_of_the_window(self) -> None:
        """Unlike a plain error count (which can be reasoned about purely
        via record_error() calls), a `blocked=True` report must keep
        `tripped` True even if checked again without any further errors --
        it represents "the provider told us to stop", not just a count.
        """
        breaker = _ErrorCountBreaker(error_limit=50)
        breaker.record_error(blocked=True)
        assert breaker.tripped is True
        assert breaker.tripped is True  # still true on a later check

    def test_errors_outside_the_window_do_not_count(self) -> None:
        """A rolling window, not a plain lifetime count: errors old enough
        to have aged out of the window must not contribute to tripping."""
        breaker = _ErrorCountBreaker(error_limit=2, window=timedelta(seconds=0))
        breaker.record_error()
        breaker.record_error()
        # window=0 means every error is immediately "outside" the window by
        # the time `tripped` is next evaluated.
        assert breaker.tripped is False


class TestFetchActiveTlesCircuitBreaker:
    """fetch_active_tles() must not hammer CelesTrak/SATNOGS after either
    reports being blocked -- see TestErrorCountBreaker's docstring for the
    incident that motivated this. Phase 1 (CelesTrak) and Phase 2 (SATNOGS)
    each have their own independent breaker/request, so a block on one
    provider must not stop the other from still being attempted.
    """

    def test_phase1_403_reports_celestrak_blocked_but_phase2_still_runs(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _error_resp(403),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 1
        assert stats["satnogs_blocked"] == 0
        assert stats["updated"] + stats["inserted"] == 1

    def test_phase2_429_reports_satnogs_blocked(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(_group_active_resp(), _error_resp(429))
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 0
        assert stats["satnogs_blocked"] == 1
        assert mgr._satnogs_breaker.tripped
        assert stats["phase2_unresolved"] == 1

    def test_no_breaker_trips_on_a_clean_run(self, db: sqlite3.Connection) -> None:
        """Regression guard: a normal, fully-successful run must not report
        either provider as blocked."""
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")]),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 0
        assert stats["satnogs_blocked"] == 0

    def test_phase2_connect_timeout_is_transient_not_a_block(self, db: sqlite3.Connection) -> None:
        """An outright inability to connect IS treated as a block (see
        TestFetchProvisionalTles' equivalent case) -- but a ReadTimeout
        (connected fine, just slow) must not be, since it's an individual
        failure rather than proof the provider is rejecting this IP.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(_group_active_resp(), httpx.ReadTimeout("boom"))
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["satnogs_blocked"] == 0
        assert stats["phase2_unresolved"] == 1
        assert stats["errors"] >= 1

    def test_phase2_connect_error_is_treated_as_blocked(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(_group_active_resp(), httpx.ConnectTimeout("boom"))
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["satnogs_blocked"] == 1
        assert mgr._satnogs_breaker.tripped

    def test_already_tripped_satnogs_breaker_skips_phase2_bulk_fetch(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mgr._satnogs_breaker.record_error(blocked=True)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=_stream_ctx(_group_active_resp()))

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        # Only Phase 1's request -- Phase 2 never touched the network.
        assert mock_client.stream.call_count == 1
        assert stats["satnogs_blocked"] == 1
        assert stats["phase2_unresolved"] == 1

    def test_already_tripped_celestrak_breaker_skips_phase1_but_phase2_still_runs(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mgr._celestrak_breaker.record_error(blocked=True)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            return_value=_stream_ctx(
                _satnogs_bulk_resp([_bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2")])
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_active_tles())

        # Only Phase 2's bulk request -- Phase 1 was skipped entirely.
        assert mock_client.stream.call_count == 1
        assert stats["celestrak_blocked"] == 1
        assert stats["updated"] + stats["inserted"] == 1


class TestFetchProvisionalTles:
    """fetch_provisional_tles() used to query SATNOGS one satellite at a
    time (~140+ requests in one run before 2026-08-11). Confirmed by direct
    testing that SATNOGS's bulk TLE dump includes provisional (>=90000)
    NORAD IDs alongside regular ones, so this now shares
    fetch_active_tles()'s Phase 2 bulk fetch (_fetch_satnogs_bulk_tles())
    instead of looping.
    """

    def test_resolves_multiple_satellites_from_one_bulk_response(
        self, db: sqlite3.Connection
    ) -> None:
        targets = {
            90001: ("Sat A", _LINE1, _LINE2),
            90002: ("Sat B", _LINE1_B, _LINE2_B),
        }
        for norad, (name, _, _) in targets.items():
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, ?, 'alive', 0)",
                (norad, name),
            )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            return_value=_stream_ctx(
                _satnogs_bulk_resp(
                    [
                        _bulk_record(norad, line1, line2, name)
                        for norad, (name, line1, line2) in targets.items()
                    ]
                )
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        assert mock_client.stream.call_count == 1
        assert stats["inserted"] == 2
        for norad, (_, line1, line2) in targets.items():
            row = db.execute(
                "SELECT line1, line2 FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            assert row is not None
            assert row["line1"] == line1
            assert row["line2"] == line2

    def test_satellite_missing_from_bulk_dump_does_not_abort_the_rest(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, 'Sat A', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90002, 'Sat B', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            return_value=_stream_ctx(
                _satnogs_bulk_resp([_bulk_record(90001, _LINE1, _LINE2, "Sat A")])
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        assert stats["inserted"] == 1
        assert stats["no_tle"] == 1
        assert (
            db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 90001").fetchone() is not None
        )
        assert db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 90002").fetchone() is None

    def test_no_rows_returns_immediately_without_a_network_call(
        self, db: sqlite3.Connection
    ) -> None:
        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock()

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        mock_client.stream.assert_not_called()
        assert stats == {
            "inserted": 0,
            "updated": 0,
            "no_tle": 0,
            "hidden_unknown": 0,
            "hidden_expired": 0,
            "errors": 0,
        }


class TestFetchProvisionalTlesBulkFetchFailure:
    """A failed bulk fetch must not be mistaken for "every satellite
    genuinely has no TLE" -- and must not fall back to a per-satellite loop,
    which would reintroduce the request-count problem this design avoids.
    """

    def test_unreachable_bulk_fetch_marks_everything_as_an_error(
        self, db: sqlite3.Connection
    ) -> None:
        for norad in (90001, 90002, 90003):
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, 'Sat', 'alive', 0)",
                (norad,),
            )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=httpx.ConnectTimeout("boom"))

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        # Exactly one attempt -- no per-satellite fallback loop.
        assert mock_client.stream.call_count == 1
        assert stats["errors"] == 3
        assert db.execute("SELECT COUNT(*) c FROM tle_data").fetchone()["c"] == 0

    def test_already_tripped_breaker_skips_the_fetch_entirely(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, 'Sat A', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mgr._satnogs_breaker.record_error(blocked=True)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock()

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        mock_client.stream.assert_not_called()
        assert stats["errors"] == 1


class TestSatnogsBulkTleCache:
    """_fetch_satnogs_bulk_tles() caches its result in-instance so
    fetch_active_tles() and fetch_provisional_tles() -- which both call it
    and normally run seconds apart in the same startup sequence -- don't
    download the same bulk payload twice.
    """

    def test_second_call_within_ttl_reuses_the_cached_result(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, 'Provisional Sat', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            side_effect=_stream_items(
                _group_active_resp(),
                _satnogs_bulk_resp(
                    [
                        _bulk_record(68795, _LINE1_B, _LINE2_B, "OrigamiSat-2"),
                        _bulk_record(90001, _LINE1, _LINE2, "Provisional Sat"),
                    ]
                ),
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            active_stats = asyncio.run(mgr.fetch_active_tles())
            prov_stats = asyncio.run(mgr.fetch_provisional_tles())

        # 1 (Phase 1 GROUP=active) + 1 (bulk SATNOGS, shared) = 2 total --
        # NOT 3, which is what a second independent bulk fetch would cost.
        assert mock_client.stream.call_count == 2
        assert active_stats["updated"] + active_stats["inserted"] == 1
        assert prov_stats["inserted"] == 1

    def test_expired_cache_triggers_a_fresh_fetch(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, 'Sat A', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mgr._satnogs_bulk_cache = (
            datetime.now(UTC) - timedelta(minutes=11),
            {90001: _bulk_record(90001, _LINE1, _LINE2, "Sat A (stale cache)")},
        )
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(
            return_value=_stream_ctx(
                _satnogs_bulk_resp([_bulk_record(90001, _LINE1_B, _LINE2_B, "Sat A")])
            )
        )

        with _patched_client(mock_client) as mock_cls, patch.object(mgr, "_log_sync"):
            _wire_mock_client(mock_cls, mock_client)
            asyncio.run(mgr.fetch_provisional_tles())

        mock_client.stream.assert_called_once()
        row = db.execute("SELECT line1 FROM tle_data WHERE norad_cat_id = 90001").fetchone()
        assert row["line1"] == _LINE1_B


class TestIsSourceStale:
    """is_source_stale() (gating the 6 CelesTrak bulk group sources --
    stations/amateur/cubesat/weather/earth-obs/science) used to only return
    True when a source had literally never been fetched, on the assumption
    that "the APScheduler interval jobs handle subsequent periodic
    refreshes" -- the same flawed assumption already found and fixed for
    fetch_provisional_tles() and sync_from_satnogs() (interval jobs don't
    fire immediately; a user who never keeps the app open for a full
    interval never sees them fire even once after the initial sync).
    is_source_stale() now also checks the source's own
    TLE_SOURCES[...]["update_interval_hours"].
    """

    def test_true_when_never_fetched(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        assert mgr.is_source_stale("celestrak-amateur") is True

    def test_false_when_within_its_own_interval(self, db: sqlite3.Connection) -> None:
        """celestrak-amateur's update_interval_hours is 2 -- a 1h-old fetch
        must still count as fresh."""
        mgr = TLEManager(db)
        one_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('celestrak-amateur', ?, ?, 'success', 0)",
            (one_hour_ago, one_hour_ago),
        )
        db.commit()
        assert mgr.is_source_stale("celestrak-amateur") is False

    def test_true_once_older_than_its_own_interval(self, db: sqlite3.Connection) -> None:
        """celestrak-stations's update_interval_hours is 1 -- a 2h-old fetch
        must count as stale, even though that's fresher than most other
        sources' own intervals (each source is judged against its own
        documented cadence, not a single shared threshold)."""
        mgr = TLEManager(db)
        two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('celestrak-stations', ?, ?, 'success', 0)",
            (two_hours_ago, two_hours_ago),
        )
        db.commit()
        assert mgr.is_source_stale("celestrak-stations") is True
        # The same age against a source with a longer interval (cubesat=4h)
        # must still count as fresh -- confirming each source is judged
        # against its own interval, not a shared one.
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('celestrak-cubesat', ?, ?, 'success', 0)",
            (two_hours_ago, two_hours_ago),
        )
        db.commit()
        assert mgr.is_source_stale("celestrak-cubesat") is False

    def test_unknown_source_name_falls_back_to_24h(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('not-a-real-source', ?, ?, 'success', 0)",
            (old, old),
        )
        db.commit()
        assert mgr.is_source_stale("not-a-real-source") is True

        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        db.execute(
            "UPDATE sync_log SET started_at = ?, finished_at = ?"
            " WHERE sync_type = 'not-a-real-source'",
            (recent, recent),
        )
        db.commit()
        assert mgr.is_source_stale("not-a-real-source") is False


class TestIsProvisionalTleStale:
    """fetch_provisional_tles() used to be called unconditionally on every
    app startup with no staleness check at all, unlike fetch_active_tles()
    (is_active_tle_stale()) -- so closing and reopening the app in quick
    succession re-ran it every single time regardless of CLAUDE.md's
    documented 12h cadence (reported 2026-08-11). is_provisional_tle_stale()
    mirrors is_active_tle_stale() exactly, keyed on the 'satnogs-provisional'
    sync_log entries fetch_provisional_tles() already writes.
    """

    def test_true_when_never_fetched(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        assert mgr.is_provisional_tle_stale() is True

    def test_false_when_recently_fetched(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        mgr._log_sync("satnogs-provisional", {"inserted": 1, "updated": 0})
        assert mgr.is_provisional_tle_stale() is False

    def test_true_once_older_than_the_default_12h(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        old = (datetime.now(UTC) - timedelta(hours=13)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs-provisional', ?, ?, 'success', 0)",
            (old, old),
        )
        db.commit()
        assert mgr.is_provisional_tle_stale() is True

    def test_respects_a_custom_max_age_hours(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs-provisional', ?, ?, 'success', 0)",
            (two_hours_ago, two_hours_ago),
        )
        db.commit()
        assert mgr.is_provisional_tle_stale(max_age_hours=1.0) is True
        assert mgr.is_provisional_tle_stale(max_age_hours=4.0) is False

    def test_unrelated_sync_types_are_ignored(self, db: sqlite3.Connection) -> None:
        """A fresh celestrak-active entry must not make provisional TLEs
        look fresh too -- each sync_type's staleness is independent."""
        mgr = TLEManager(db)
        mgr._log_sync("celestrak-active", {"inserted": 1, "updated": 0})
        assert mgr.is_provisional_tle_stale() is True


class TestActiveTleRetryAfterPersistence:
    """The retry-due marker (app_settings key 'active_tle_retry_after')
    must survive being read back by a *different* TLEManager instance on
    the same DB connection -- standing in for the app being closed and a
    fresh MainWindow/TLEManager being constructed on the next launch. An
    in-memory-only marker (e.g. a plain instance attribute) would silently
    lose a pending retry across a restart, which is exactly the gap this
    closes (see MainWindow._schedule_active_tle_retry_if_blocked()'s
    docstring, 2026-08-11).
    """

    def test_no_marker_by_default(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        assert mgr.get_active_tle_retry_after() is None
        assert mgr.is_active_tle_retry_due() is False

    def test_future_marker_is_not_yet_due(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        mgr.set_active_tle_retry_after(datetime.now(UTC) + timedelta(hours=3))
        assert mgr.is_active_tle_retry_due() is False

    def test_past_marker_is_due(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        mgr.set_active_tle_retry_after(datetime.now(UTC) - timedelta(minutes=1))
        assert mgr.is_active_tle_retry_due() is True

    def test_clearing_with_none_removes_the_marker(self, db: sqlite3.Connection) -> None:
        mgr = TLEManager(db)
        mgr.set_active_tle_retry_after(datetime.now(UTC) - timedelta(minutes=1))
        assert mgr.is_active_tle_retry_due() is True
        mgr.set_active_tle_retry_after(None)
        assert mgr.get_active_tle_retry_after() is None
        assert mgr.is_active_tle_retry_due() is False

    def test_marker_survives_a_fresh_tle_manager_instance_on_the_same_db(
        self, db: sqlite3.Connection
    ) -> None:
        """Simulates an app restart: a new TLEManager is constructed on the
        same (persisted) DB connection and must still see the marker set by
        the previous instance."""
        due_at = datetime.now(UTC) - timedelta(minutes=1)
        TLEManager(db).set_active_tle_retry_after(due_at)

        restarted_mgr = TLEManager(db)
        assert restarted_mgr.is_active_tle_retry_due() is True
        retrieved = restarted_mgr.get_active_tle_retry_after()
        assert retrieved is not None
        assert abs((retrieved - due_at).total_seconds()) < 1
