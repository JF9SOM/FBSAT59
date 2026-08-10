"""Tests for data.tle_manager.TLEManager (fetch_active_tles(), fetch_provisional_tles()).

Lightweight (no Qt import) so it is safe to run locally, unlike test_main_window.py.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.database import SCHEMA_SQL
from data.tle_manager import TLEManager

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
    Sequence per test: 5 bulk + Phase 2a probe + Phase 2a miss + Phase 2b
    probe + Phase 2b match.
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
            side_effect=[_bulk_resp()] * 5
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
            side_effect=[_bulk_resp()] * 5
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
            side_effect=[_bulk_resp()] * 5
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
        assert mock_client.get.await_count == 7
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

        # 5 bulk + 1 Phase 2a probe + 2 individual CATNR fetches. No Phase 2b calls.
        assert mock_client.get.await_count == 8
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
            side_effect=[_bulk_resp()] * 5
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

        assert mock_client.get.await_count == 9
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
            side_effect=[_bulk_resp()] * 5
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

        # 5 bulk + 1 Phase 2a probe + 1 Phase 2b probe — no per-satellite calls at all.
        assert mock_client.get.await_count == 7
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
        mock_client.get = AsyncMock(side_effect=[_bulk_resp()] * 5)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        # Only the 5 Phase 1 bulk-group requests — no Phase 2 (2a/2b) call at all,
        # since this satellite never enters refresh_targets.
        assert mock_client.get.await_count == 5

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
        mock_client.get = AsyncMock(side_effect=[_bulk_resp()] * 5)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        assert mock_client.get.await_count == 5


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
            side_effect=[_bulk_resp()] * 5
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

        # One message per Phase 1 bulk group, plus one for Phase 2a starting.
        # No Phase 2b message, since Phase 2a resolved the only target satellite.
        assert len(messages) == 6
        assert messages[-1] == "CelesTrak: 1 satellite(s)..."
        assert all("SATNOGS" not in m for m in messages)
