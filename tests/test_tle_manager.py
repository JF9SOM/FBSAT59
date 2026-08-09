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

# A second synthetic element set, keyed to a different NORAD ID, for tests
# that need two distinct satellites in the same CelesTrak CATNR batch.
_LINE1_B = "1 68795U 26088D   26192.91095598  .00012852  00000-0  81506-3 0  9999"
_LINE2_B = "2 68795  97.5066 327.0920 0015527  49.6354 310.6228 15.08945428  9764"


def _bulk_resp() -> MagicMock:
    """An empty Phase 1 bulk-group response (no matches, so Phase 2 runs)."""
    resp = MagicMock()
    resp.text = ""
    resp.raise_for_status.return_value = None
    return resp


def _catnr_resp(text: str) -> MagicMock:
    """A Phase 2a CelesTrak CATNR batch response with the given TLE-format body."""
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


def _satnogs_resp(tle1: str, tle2: str) -> MagicMock:
    """A Phase 2b SATNOGS TLE API JSON response."""
    resp = MagicMock()
    resp.json.return_value = [{"tle1": tle1, "tle2": tle2}]
    resp.raise_for_status.return_value = None
    return resp


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
    These tests force a Phase 2a (CelesTrak CATNR batch) miss, so Phase 2b
    is exercised.
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
            + [_catnr_resp("")]  # Phase 2a: no match
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
            + [_catnr_resp("")]  # Phase 2a: no match
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

    def test_phase2a_celestrak_catnr_batch_resolves_stale_satellite(
        self, db: sqlite3.Connection
    ) -> None:
        """The CelesTrak CATNR batch (Phase 2a) alone must be able to refresh
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

        catnr_batch_text = f"OrigamiSat-2\n{_LINE1_B}\n{_LINE2_B}\n"
        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()] * 5 + [_catnr_resp(catnr_batch_text)]
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        # Resolved entirely in Phase 2a — no Phase 2b (SATNOGS) call was needed.
        assert mock_client.get.await_count == 6
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

    def test_phase2a_batches_multiple_norad_ids_into_one_request(
        self, db: sqlite3.Connection
    ) -> None:
        """Two satellites needing refresh must be resolved by a single
        comma-delimited CATNR request, not one request per satellite.
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

        catnr_batch_text = f"OrigamiSat-2\n{_LINE1_B}\n{_LINE2_B}\nARICA-2\n{_LINE1}\n{_LINE2}\n"
        mgr = TLEManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[_bulk_resp()] * 5 + [_catnr_resp(catnr_batch_text)]
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        # Exactly one Phase 2a request for both satellites, no Phase 2b calls.
        assert mock_client.get.await_count == 6
        catnr_call = mock_client.get.call_args_list[-1]
        queried_ids = set(catnr_call.kwargs["params"]["CATNR"].split(","))
        assert queried_ids == {"68795", "68796"}

        for norad in (68795, 68796):
            row = db.execute(
                "SELECT line1 FROM tle_data WHERE norad_cat_id = ?", (norad,)
            ).fetchone()
            assert row is not None

    def test_phase2b_still_refreshes_what_phase2a_misses(self, db: sqlite3.Connection) -> None:
        """When CelesTrak's CATNR batch doesn't have a satellite, Phase 2b
        (SATNOGS, per-satellite) must still be attempted as a fallback.
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
            + [_catnr_resp("")]  # Phase 2a: CelesTrak doesn't have it
            + [_satnogs_resp(_LINE1_B, _LINE2_B)]  # Phase 2b: SATNOGS does
        )

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.fetch_active_tles())

        assert mock_client.get.await_count == 7
        phase2b_call = mock_client.get.call_args_list[-1]
        assert phase2b_call.kwargs["params"]["norad_cat_id"] == 68795

        row = db.execute(
            "SELECT fetched_at, tle_group, source FROM tle_data WHERE norad_cat_id = 68795"
        ).fetchone()
        assert row["fetched_at"] != "2026-06-27T07:34:09+00:00"
        assert row["tle_group"] == "cubesat"
        assert row["source"] == "satnogs"
        assert stats["updated"] == 1

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

    def test_mixed_success_and_timeout_are_both_counted_correctly(
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

        import httpx

        async def _fake_get(*_args: object, **kwargs: object) -> MagicMock:
            params = kwargs["params"]
            assert isinstance(params, dict)
            if params["norad_cat_id"] == 90001:
                raise httpx.ConnectTimeout("boom")
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
        assert db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 90001").fetchone() is None
        assert (
            db.execute("SELECT 1 FROM tle_data WHERE norad_cat_id = 90002").fetchone() is not None
        )
