"""Tests for data.tle_manager.TLEManager (fetch_active_tles(), fetch_provisional_tles()).

Lightweight (no Qt import) so it is safe to run locally, unlike test_main_window.py.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from data.database import SCHEMA_SQL
from data.tle_manager import TLEManager, _ErrorCountBreaker, _run_with_breaker

_LINE1 = "1 68796U 26088E   26192.83685747  .00002724  00000+0  17770-3 0  9997"
_LINE2 = "2 68796  97.5082 341.6669 0015331 359.2893   0.8311 15.08613790 12013"

# A second synthetic element set, keyed to a different NORAD ID.
_LINE1_B = "1 68795U 26088D   26192.91095598  .00012852  00000-0  81506-3 0  9999"
_LINE2_B = "2 68795  97.5066 327.0920 0015527  49.6354 310.6228 15.08945428  9764"


def _bulk_resp() -> MagicMock:
    """An empty Phase 1 bulk-group response (no matches, so Phase 2 runs)."""
    resp = MagicMock()
    resp.text = ""
    resp.raise_for_status.return_value = None
    return resp


def _catnr_found_resp(name: str, line1: str, line2: str) -> MagicMock:
    """A Phase 2a CelesTrak CATNR response that resolves one satellite.

    CATNR only ever accepts a single catalog number per request (confirmed
    against CelesTrak's own documentation and a live query — a
    comma-delimited list is rejected with an "Invalid query" body, 2026-08-09),
    so this always represents exactly one satellite's 3-line TLE block.
    """
    resp = MagicMock()
    resp.text = f"{name}\n{line1}\n{line2}\n"
    resp.raise_for_status.return_value = None
    return resp


def _catnr_not_found_resp() -> MagicMock:
    """A Phase 2a CelesTrak CATNR response for a satellite CelesTrak lacks."""
    resp = MagicMock()
    resp.text = ""
    resp.raise_for_status.return_value = None
    return resp


def _satnogs_resp(tle1: str, tle2: str) -> MagicMock:
    """A Phase 2b SATNOGS TLE API JSON response."""
    resp = MagicMock()
    resp.json.return_value = [{"tle1": tle1, "tle2": tle2}]
    resp.raise_for_status.return_value = None
    return resp


def _error_resp(status_code: int) -> MagicMock:
    """A Phase 2a/2b response representing a real HTTP error status (404,
    403, 429, 500, ...) that raise_for_status() actually raises for --
    unlike _catnr_not_found_resp(), which models a 200 OK with an empty
    body. CelesTrak's own "not found" response can be either, and only the
    real HTTPStatusError path is what the circuit breaker reacts to.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"{status_code} error", request=MagicMock(), response=resp
    )
    return resp


def _probe_ok_resp() -> MagicMock:
    """A successful _probe_reachable() response.

    The probe only awaits the GET and never touches the response body, so a
    bare MagicMock is enough — but it's a distinct helper because every
    Phase 2a/2b test needs one extra response for the probe, inserted right
    before the real per-satellite call.
    """
    return MagicMock()


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


class TestFetchActiveTlesSatnogsSourceIdRouting:
    """A satellite migrated from a provisional (>=90000) NORAD ID retains
    satnogs_source_id pointing at the old ID. SATNOGS's TLE API can keep
    serving the TLE keyed by that old ID long after migration (observed for
    ARICA-2 / NORAD 68796, 2026-07-12), so Phase 2b of fetch_active_tles()
    must query by satnogs_source_id when present, not by the real NORAD ID.
    These tests force a Phase 2a (CelesTrak) miss, so Phase 2b is exercised.
    Sequence per test: 1 bulk (GROUP=active) + Phase 2a probe + Phase 2a
    miss + Phase 2b probe + Phase 2b match.
    """

    def test_queries_by_satnogs_source_id_when_present(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden, satnogs_source_id)"
            " VALUES (68796, 'ARICA-2', 'alive', 0, 98329)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_catnr_not_found_resp()]  # Phase 2a: CelesTrak doesn't have it
            + [_probe_ok_resp()]  # Phase 2b: reachability probe
            + [_satnogs_resp(_LINE1, _LINE2)]  # Phase 2b: match
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        # Phase 2b call must have used the provisional satnogs_source_id (98329),
        # not the real NORAD ID (68796).
        phase2b_call = mock_client.get.call_args_list[-1]
        assert phase2b_call.kwargs["params"]["norad_cat_id"] == 98329

        # The TLE result must be stored under the real NORAD ID.
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
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_catnr_not_found_resp()]  # Phase 2a: CelesTrak doesn't have it
            + [_probe_ok_resp()]  # Phase 2b: reachability probe
            + [_satnogs_resp(_LINE1, _LINE2)]  # Phase 2b: match
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        phase2b_call = mock_client.get.call_args_list[-1]
        assert phase2b_call.kwargs["params"]["norad_cat_id"] == 43803


class TestFetchActiveTlesPhase2RefreshesStaleSatnogsTles:
    """Regression coverage for the "permanent staleness ratchet" bug: a
    satellite not tracked by any Phase 1 bulk CelesTrak group used to get
    exactly one TLE ever (whichever run first discovered it, via
    source='satnogs'), then be silently excluded from every later run
    merely for already having a tle_data row — no matter how stale
    (confirmed for ORIGAMISAT-2 / NORAD 68795, stuck 44 days, 2026-08-09).
    """

    def test_phase2a_celestrak_individual_query_resolves_stale_satellite(
        self, db: sqlite3.Connection
    ) -> None:
        """CelesTrak (Phase 2a), queried per satellite (CATNR takes only a
        single catalog number — see _probe_reachable()'s docstring for how
        an earlier version of this got that wrong), must be able to refresh
        a satellite Phase 1's bulk groups don't cover, without ever falling
        through to the SATNOGS fallback (Phase 2b) — this is the path that
        fixed ORIGAMISAT-2 when SATNOGS itself was unreachable but CelesTrak
        was not (2026-08-09).
        """
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
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_catnr_found_resp("OrigamiSat-2", _LINE1_B, _LINE2_B)]  # Phase 2a: match
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        # Resolved entirely in Phase 2a — no Phase 2b (SATNOGS) call was needed.
        assert mock_client.get.await_count == 3
        catnr_call = mock_client.get.call_args_list[-1]
        assert catnr_call.kwargs["params"]["CATNR"] == "68795"

        row = db.execute(
            "SELECT fetched_at, tle_group, source FROM tle_data WHERE norad_cat_id = 68795"
        ).fetchone()
        assert row["fetched_at"] != "2026-06-27T07:34:09+00:00"
        assert row["tle_group"] == "cubesat"  # preserved, not reset to 'amateur'
        assert row["source"] == "satnogs"  # tag kept even though CelesTrak supplied it
        assert stats["updated"] == 1
        assert stats["inserted"] == 0

    def test_phase2a_resolves_multiple_satellites_via_separate_individual_queries(
        self, db: sqlite3.Connection
    ) -> None:
        """Two satellites needing refresh are each resolved by their own
        CATNR request (concurrently, up to 20 at a time) — not one combined
        request, since CelesTrak's CATNR only accepts a single value.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68796, 'ARICA-2', 'alive', 0)"
        )
        db.commit()

        targets = {68795: ("OrigamiSat-2", _LINE1_B, _LINE2_B), 68796: ("ARICA-2", _LINE1, _LINE2)}

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if "GROUP" in params:
                return _bulk_resp()
            norad = int(params["CATNR"])
            name, line1, line2 = targets[norad]
            return _catnr_found_resp(name, line1, line2)

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        # 1 bulk (GROUP=active) + 1 Phase 2a probe + 2 individual CATNR fetches.
        # No Phase 2b calls.
        assert mock_client.get.await_count == 4
        assert stats["updated"] + stats["inserted"] == 2

        for norad in (68795, 68796):
            row = db.execute(
                "SELECT line1 FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            assert row is not None

    def test_phase2b_still_refreshes_what_phase2a_misses(self, db: sqlite3.Connection) -> None:
        """When CelesTrak doesn't have a satellite, Phase 2b (SATNOGS,
        per-satellite) must still be attempted as a fallback.
        """
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
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_catnr_not_found_resp()]  # Phase 2a: CelesTrak doesn't have it
            + [_probe_ok_resp()]  # Phase 2b: reachability probe
            + [_satnogs_resp(_LINE1_B, _LINE2_B)]  # Phase 2b: SATNOGS does
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert mock_client.get.await_count == 5
        phase2b_call = mock_client.get.call_args_list[-1]
        assert phase2b_call.kwargs["params"]["norad_cat_id"] == 68795

        row = db.execute(
            "SELECT fetched_at, tle_group, source FROM tle_data WHERE norad_cat_id = 68795"
        ).fetchone()
        assert row["fetched_at"] != "2026-06-27T07:34:09+00:00"
        assert row["tle_group"] == "cubesat"
        assert row["source"] == "satnogs"
        assert stats["updated"] == 1

    def test_skips_all_remaining_when_both_celestrak_and_satnogs_probes_fail(
        self, db: sqlite3.Connection
    ) -> None:
        """If neither host can even be connected to, both phases should bail
        out on their first probe rather than trying each straggler
        individually against a dead host (twice over).
        """
        for norad in (68795, 68796):
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, 'Sat', 'alive', 0)",
                (norad,),
            )
        db.commit()

        import httpx

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [httpx.ConnectTimeout("boom")]  # Phase 2a: probe fails to connect
            + [httpx.ConnectTimeout("boom")]  # Phase 2b: probe fails to connect
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        # 1 bulk (GROUP=active) + 1 Phase 2a probe + 1 Phase 2b probe — no
        # per-satellite calls at all.
        assert mock_client.get.await_count == 3
        assert stats["errors"] == 2
        assert db.execute("SELECT COUNT(*) c FROM tle_data").fetchone()["c"] == 0

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
        mock_client.get = AsyncMock(side_effect=[_bulk_resp()])

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        # Only the 1 Phase 1 GROUP=active request — no Phase 2 (2a/2b) call at
        # all, since this satellite never enters refresh_targets.
        assert mock_client.get.await_count == 1

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
        mock_client.get = AsyncMock(side_effect=[_bulk_resp()])

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        assert mock_client.get.await_count == 1


class TestFetchProvisionalTlesConcurrency:
    """fetch_provisional_tles() used to query SATNOGS one satellite at a time
    with zero concurrency: ~140+ visible provisional satellites at up to 10s
    each could take over 20 minutes when SATNOGS is unreachable, and since
    this method runs as one step in the startup chain, that also delayed
    every later step. Now runs up to 20 requests concurrently, matching the
    pattern already used by fetch_active_tles()'s SATNOGS fallback. These
    tests verify that concurrent execution still routes each response to the
    correct satellite and keeps stats correct — not just that it's fast.
    """

    def test_resolves_multiple_satellites_concurrently_without_cross_contamination(
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

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            norad = params["norad_cat_id"]
            _, line1, line2 = targets[norad]
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = [{"tle1": line1, "tle2": line2}]
            return resp

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        assert stats["inserted"] == 2
        for norad, (_, line1, line2) in targets.items():
            row = db.execute(
                "SELECT line1, line2 FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            assert row is not None
            assert row["line1"] == line1
            assert row["line2"] == line2

    def test_one_satellites_read_timeout_does_not_abort_the_whole_run(
        self, db: sqlite3.Connection
    ) -> None:
        """A ReadTimeout (connected fine, just slow to respond) on one
        satellite is an individual failure, not evidence the whole host is
        down — it must not trip the reachability probe (which only reacts to
        ConnectTimeout/ConnectError) or stop the rest of the batch.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, 'Sat A', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90002, 'Sat B', 'alive', 0)"
        )
        db.commit()

        import httpx

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if params["norad_cat_id"] == 90002:
                raise httpx.ReadTimeout("boom")
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = [{"tle1": _LINE1, "tle2": _LINE2}]
            return resp

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        assert stats["errors"] == 1
        assert stats["inserted"] == 1
        assert (
            db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 90001").fetchone() is not None
        )
        assert db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 90002").fetchone() is None


class TestFetchProvisionalTlesCircuitBreaker:
    """If SATNOGS can't even accept a connection for the first request, the
    whole run should bail out immediately rather than waiting out every
    remaining satellite's own timeout in turn (the actual cause of a
    "restarted the app but nothing changed" report, 2026-08-09 — the app was
    still working through ~140 sequential-ish timeouts, not stuck or broken).
    """

    def test_skips_entire_run_when_first_probe_cannot_connect(self, db: sqlite3.Connection) -> None:
        for norad in (90001, 90002, 90003):
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, 'Sat', 'alive', 0)",
                (norad,),
            )
        db.commit()

        import httpx

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("boom"))

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        # Only the probe request was attempted — not one request per satellite.
        assert mock_client.get.await_count == 1
        assert stats["errors"] == 3
        assert db.execute("SELECT COUNT(*) c FROM tle_data").fetchone()["c"] == 0

    def test_proceeds_normally_when_probe_succeeds(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, 'Sat A', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"tle1": _LINE1, "tle2": _LINE2}]
        mock_client.get = AsyncMock(return_value=resp)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_provisional_tles())

        # Probe + the real per-satellite request.
        assert mock_client.get.await_count == 2
        assert stats["inserted"] == 1


class TestFetchActiveTlesPhase1UsesGroupActive:
    """2026-08-11: Phase 1 switched from 5 separate curated-group requests
    to a single GROUP=active request -- what actually trips CelesTrak's
    firewall is HTTP error count in a 2h window, and a single successful
    request contributes zero errors regardless of how much data it returns
    (unlike the old 5-group loop, which was still only 5 requests but grew
    to hundreds more via Phase 2's per-satellite fallback whenever a
    satellite fell outside all 5 curated groups -- something GROUP=active
    itself now avoids for the vast majority of satellites).
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
        mock_client.get = AsyncMock(return_value=_bulk_resp())

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        assert mock_client.get.await_count == 1
        call = mock_client.get.call_args_list[0]
        assert call.kwargs["params"] == {"GROUP": "active", "FORMAT": "TLE"}

    def test_403_with_cache_not_updated_message_is_not_treated_as_blocked(
        self, db: sqlite3.Connection
    ) -> None:
        """GROUP=active's own ~2h server-side cache returns a 403 with a
        specific explanatory body when re-requested too soon -- this must
        NOT trip the circuit breaker or set celestrak_blocked, or pressing
        "Update TLE" twice within 2h would falsely look like an abuse block.
        Phase 2 must still run normally afterward (nothing was actually
        wrong with the connection).
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        cache_stale_resp = MagicMock()
        cache_stale_resp.status_code = 403
        cache_stale_resp.text = (
            "GP data has not updated since your last successful download "
            "of GROUP=active at 2026-08-11T12:00:00Z"
        )
        cache_stale_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 error", request=MagicMock(), response=cache_stale_resp
        )

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[cache_stale_resp]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_catnr_found_resp("OrigamiSat-2", _LINE1_B, _LINE2_B)]  # Phase 2a: match
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 0
        assert stats["errors"] == 0
        assert stats["updated"] + stats["inserted"] == 1
        assert not mgr._celestrak_breaker.tripped

    def test_403_without_cache_message_is_still_treated_as_blocked(
        self, db: sqlite3.Connection
    ) -> None:
        """A 403 that doesn't carry the specific "cache not updated" wording
        is a real abuse-protection signal and must still trip the breaker,
        exactly like Phase 2a's own 403 handling.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        blocked_resp = MagicMock()
        blocked_resp.status_code = 403
        blocked_resp.text = "Forbidden"
        blocked_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 error", request=MagicMock(), response=blocked_resp
        )

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[blocked_resp]
            + [_probe_ok_resp()]  # Phase 2b: reachability probe (2a skipped, breaker tripped)
            + [_satnogs_resp(_LINE1_B, _LINE2_B)]  # Phase 2b: resolves it
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 1
        assert mgr._celestrak_breaker.tripped


class TestFetchActiveTlesProgressCallback:
    """fetch_active_tles() used to run silently for however long Phase 2 took,
    which looked identical to a hang and led to a user closing the app before
    it ever reached the satellite they cared about (2026-08-10). The
    optional progress_callback must fire at each phase/group transition so a
    caller can show that work is still happening.
    """

    def test_progress_callback_fires_for_each_phase(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_catnr_found_resp("OrigamiSat-2", _LINE1_B, _LINE2_B)]  # Phase 2a: match
        )

        messages: list[str] = []

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles(progress_callback=messages.append))

        # One message for Phase 1's single GROUP=active request, one for
        # Phase 2a starting, and one per-item progress update (batch of 1, so
        # it fires once and is both the first and last completion). No Phase
        # 2b message, since Phase 2a resolved the only target satellite.
        assert len(messages) == 3
        assert messages[-2] == "CelesTrak: 1 satellite(s)..."
        assert messages[-1] == "CelesTrak: 1/1 checked..."
        assert all("SATNOGS" not in m for m in messages)

    def test_progress_updates_periodically_for_a_larger_batch(self, db: sqlite3.Connection) -> None:
        """A batch bigger than the every-10 throttle must produce more than
        just the phase's starting message -- one "Fetching..." message with
        no further updates for a slow batch of hundreds is exactly what
        looked like a hang (2026-08-10).
        """
        for i in range(25):
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, ?, 'alive', 0)",
                (10001 + i, f"Sat {i}"),
            )
        db.commit()

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if "GROUP" in params:
                return _bulk_resp()
            return _catnr_not_found_resp()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        messages: list[str] = []

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles(progress_callback=messages.append))

        celestrak_progress = [m for m in messages if m.startswith("CelesTrak:") and "checked" in m]
        assert "CelesTrak: 10/25 checked..." in celestrak_progress
        assert "CelesTrak: 20/25 checked..." in celestrak_progress
        assert celestrak_progress[-1] == "CelesTrak: 25/25 checked..."


class TestFetchActiveTlesPhase2Prioritization:
    """Phase 2 must query satellites with no TLE at all first, then
    satellites with only a stale source='satnogs' TLE ordered
    oldest-fetched-first -- otherwise a slow provider handling hundreds of
    targets per run can perpetually never reach whichever satellite happens
    to sort last in arbitrary DB row order (confirmed for ORIGAMISAT-2 /
    NORAD 68795, 2026-08-10: it had an old TLE that never got refreshed
    across a week of runs that each stopped partway through Phase 2).
    """

    def test_no_tle_first_then_oldest_fetched(self, db: sqlite3.Connection) -> None:
        # Inserted in the opposite order from the expected priority, so a
        # passing assertion can't be an accident of row/insertion order.
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (10003, 'Sat C (newer TLE)', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO tle_data (norad_cat_id, name, line1, line2, source, fetched_at)"
            " VALUES (10003, 'Sat C', ?, ?, 'satnogs', '2026-08-01T00:00:00+00:00')",
            (_LINE1_B, _LINE2_B),
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
            " VALUES (10001, 'Sat A (no TLE)', 'alive', 0)"
        )
        db.commit()

        catnr_order: list[int] = []

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if "GROUP" in params:
                return _bulk_resp()
            if "CATNR" in params:
                catnr_order.append(int(params["CATNR"]))
                return _catnr_not_found_resp()
            # SATNOGS probe/fetch (norad_cat_id key) -- not the focus of
            # this test, just needs to resolve without raising.
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {}
            return resp

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        # First call is the reachability probe (uses whichever satellite
        # sorts first), then the real per-satellite loop queries all three
        # again in that same priority order: no-TLE first, then
        # oldest-fetched-first among the rest.
        assert catnr_order[0] == 10001
        assert catnr_order[1:] == [10001, 10002, 10003]


class TestErrorCountBreaker:
    """Unit coverage for the low-level breaker used by both Phase 2a
    (CelesTrak) and Phase 2b (SATNOGS) to stop a per-satellite fetch batch
    before it can trip a provider's own abuse protection -- see
    CLAUDE.md's "fetch_active_tles() の2フェーズ設計" for the incident
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


class TestRunWithBreaker:
    """Unit coverage for the bounded worker-pool runner both Phase 2a and
    Phase 2b use in place of the old plain `asyncio.gather` + `Semaphore`
    pattern, which had no way to stop issuing new requests once a provider
    started rejecting them.
    """

    def test_runs_every_task_when_breaker_never_trips(self) -> None:
        breaker = _ErrorCountBreaker(error_limit=100)
        completed: list[int] = []

        async def _make_task(n: int) -> None:
            completed.append(n)

        async def _run() -> None:
            import functools

            await _run_with_breaker(
                [functools.partial(_make_task, n) for n in range(10)],
                breaker,
                concurrency=4,
            )

        asyncio.run(_run())
        assert sorted(completed) == list(range(10))

    def test_stops_pulling_new_work_once_breaker_trips(self) -> None:
        """20 tasks, concurrency capped at 5: the first 5 all start (and
        error out, tripping the breaker) before any worker loops back for a
        6th -- so at most 5 of the 20 should ever run.
        """
        breaker = _ErrorCountBreaker(error_limit=1)
        completed: list[int] = []

        async def _make_task(n: int) -> None:
            completed.append(n)
            breaker.record_error()

        async def _run() -> None:
            import functools

            await _run_with_breaker(
                [functools.partial(_make_task, n) for n in range(20)],
                breaker,
                concurrency=5,
            )

        asyncio.run(_run())
        assert len(completed) <= 5
        assert len(completed) >= 1


class TestFetchActiveTlesCircuitBreaker:
    """fetch_active_tles() must stop a Phase 2a/2b batch early rather than
    hammer CelesTrak/SATNOGS into blocking this IP -- see
    TestErrorCountBreaker's docstring for the incident that motivated this.
    """

    def test_phase2a_breaker_trips_on_repeated_404s_and_falls_back_to_phase2b(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both target satellites 404 at CelesTrak (a routine, expected
        outcome for satellites CelesTrak simply doesn't carry) -- with the
        error limit patched down to 1, the second 404 alone must be enough
        to report celestrak_blocked, and Phase 2b must still be given every
        satellite Phase 2a didn't resolve.
        """
        monkeypatch.setattr("data.tle_manager._CELESTRAK_CATNR_ERROR_LIMIT", 1)

        for norad, name in ((68795, "OrigamiSat-2"), (68796, "ARICA-2")):
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, ?, 'alive', 0)",
                (norad, name),
            )
        db.commit()

        targets = {68795: (_LINE1_B, _LINE2_B), 68796: (_LINE1, _LINE2)}

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if "GROUP" in params:
                return _bulk_resp()
            if "CATNR" in params:
                return _error_resp(404)
            # Phase 2b (SATNOGS): resolve via the fallback.
            norad = int(params["norad_cat_id"])
            line1, line2 = targets[norad]
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = [{"tle1": line1, "tle2": line2}]
            return resp

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 1
        assert stats["satnogs_blocked"] == 0
        # Both satellites still got resolved, via Phase 2b.
        assert stats["updated"] + stats["inserted"] == 2
        for norad in (68795, 68796):
            row = db.execute(
                "SELECT line1 FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            assert row is not None

    def test_phase2a_403_reports_blocked_even_under_the_default_error_limit(
        self, db: sqlite3.Connection
    ) -> None:
        """A single 403 is CelesTrak's own signal that the block has already
        started -- celestrak_blocked must be set even though the default
        error limit (20) is nowhere near reached.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a: reachability probe
            + [_error_resp(403)]  # Phase 2a: already blocked
            + [_probe_ok_resp()]  # Phase 2b: reachability probe
            + [_error_resp(500)]  # Phase 2b: give up on this one
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 1

    def test_phase2b_breaker_trips_on_repeated_errors(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CelesTrak doesn't have either satellite (Phase 2a "not found",
        the normal 200-OK-with-empty-body case), so both fall to Phase 2b —
        which then fails for both. With the SATNOGS error limit patched
        down to 1, satnogs_blocked must be set.
        """
        monkeypatch.setattr("data.tle_manager._SATNOGS_TLE_ERROR_LIMIT", 1)

        for norad, name in ((68795, "OrigamiSat-2"), (68796, "ARICA-2")):
            db.execute(
                "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
                " VALUES (?, ?, 'alive', 0)",
                (norad, name),
            )
        db.commit()

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if "GROUP" in params:
                return _bulk_resp()
            if "CATNR" in params:
                return _catnr_not_found_resp()
            return _error_resp(500)

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_fake_get)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 0
        assert stats["satnogs_blocked"] == 1

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
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]
            + [_catnr_found_resp("OrigamiSat-2", _LINE1_B, _LINE2_B)]
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert stats["celestrak_blocked"] == 0
        assert stats["satnogs_blocked"] == 0

    def test_phase2a_unreachable_probe_reports_blocked_and_falls_back_to_phase2b(
        self, db: sqlite3.Connection
    ) -> None:
        """A fully-unreachable CelesTrak (TCP connect itself times out --
        confirmed 2026-08-10 against a real firewall-blocked IP) must still
        set celestrak_blocked=1, not just log a warning. Without it,
        _schedule_active_tle_retry_if_blocked() (main_window.py) never
        triggers the 3h backoff, and the status bar is left stuck on the
        "CelesTrak: N satellite(s)..." message with no error shown and no
        indication anything will retry later.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [httpx.ConnectTimeout("connect timed out")]  # Phase 2a probe
            + [_probe_ok_resp()]  # Phase 2b probe
            + [_satnogs_resp(_LINE1_B, _LINE2_B)]  # Phase 2b resolves it
        )

        progress_messages: list[str] = []

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles(progress_callback=progress_messages.append))

        assert stats["celestrak_blocked"] == 1
        assert stats["satnogs_blocked"] == 0
        assert stats["updated"] + stats["inserted"] == 1
        assert any("unreachable" in m for m in progress_messages)

    def test_phase2b_unreachable_probe_reports_blocked_and_preserves_unresolved_count(
        self, db: sqlite3.Connection
    ) -> None:
        """Mirrors the Phase 2a case above for SATNOGS, and also guards
        against a related bug: the unreachable branch used to reset
        `remaining` to an empty dict to skip the fetch loop, which corrupted
        phase2_unresolved to 0 -- making the "Fetched X/Y" status message
        falsely claim every satellite resolved when in fact none did.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68795, 'OrigamiSat-2', 'alive', 0)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()]
            + [_probe_ok_resp()]  # Phase 2a probe
            + [_catnr_not_found_resp()]  # Phase 2a: CelesTrak doesn't have it
            + [httpx.ConnectTimeout("connect timed out")]  # Phase 2b probe
        )

        progress_messages: list[str] = []

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles(progress_callback=progress_messages.append))

        assert stats["celestrak_blocked"] == 0
        assert stats["satnogs_blocked"] == 1
        assert stats["phase2_total"] == 1
        assert stats["phase2_unresolved"] == 1
        assert stats["updated"] + stats["inserted"] == 0
        assert any("unreachable" in m for m in progress_messages)


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
