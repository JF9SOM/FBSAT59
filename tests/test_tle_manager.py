"""Tests for data.tle_manager.TLEManager.fetch_active_tles().

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
    ARICA-2 / NORAD 68796, 2026-07-12), so Phase 2 of fetch_active_tles()
    must query by satnogs_source_id when present, not by the real NORAD ID.
    """

    def test_queries_by_satnogs_source_id_when_present(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden, satnogs_source_id)"
            " VALUES (68796, 'ARICA-2', 'alive', 0, 98329)"
        )
        db.commit()

        mgr = TLEManager(db)
        mock_client = AsyncMock()
        resp = MagicMock()
        resp.json.return_value = [{"tle1": _LINE1, "tle2": _LINE2}]
        resp.raise_for_status.return_value = None
        mock_client.get = AsyncMock(return_value=resp)

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            # Bulk group fetch (Phase 1) — return no matches so Phase 2 runs.
            bulk_resp = MagicMock()
            bulk_resp.text = ""
            bulk_resp.raise_for_status.return_value = None
            mock_client.get.side_effect = [bulk_resp] * 5 + [resp]
            asyncio.run(mgr.fetch_active_tles())

        # Phase 2 call must have used the provisional satnogs_source_id (98329),
        # not the real NORAD ID (68796).
        phase2_call = mock_client.get.call_args_list[-1]
        assert phase2_call.kwargs["params"]["norad_cat_id"] == 98329

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
        resp = MagicMock()
        resp.json.return_value = [{"tle1": _LINE1, "tle2": _LINE2}]
        resp.raise_for_status.return_value = None

        bulk_resp = MagicMock()
        bulk_resp.text = ""
        bulk_resp.raise_for_status.return_value = None
        mock_client.get = AsyncMock(side_effect=[bulk_resp] * 5 + [resp])

        with (
            patch("data.tle_manager.httpx.AsyncClient") as mock_cls,
            patch.object(mgr, "_log_sync"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.fetch_active_tles())

        phase2_call = mock_client.get.call_args_list[-1]
        assert phase2_call.kwargs["params"]["norad_cat_id"] == 43803
