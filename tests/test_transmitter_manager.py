"""Tests for data.transmitter_manager.TransmitterManager.sync_satellite_names().

Lightweight (no Qt import) so it is safe to run locally, unlike test_main_window.py.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from data.database import SCHEMA_SQL
from data.transmitter_manager import TransmitterManager


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _mock_satellites_response(satellites: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"results": satellites, "next": None}
    resp.raise_for_status.return_value = None
    return resp


def _run_sync(db: sqlite3.Connection, satellites: list[dict]) -> dict[str, int]:
    mgr = TransmitterManager(db)
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mock_satellites_response(satellites))
    with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        return asyncio.run(mgr.sync_satellite_names())


class TestSyncSatelliteNamesUnhide:
    """A satellite auto-hidden (is_hidden=2) while status looked dead/unknown
    should reappear once SATNOGS reports it alive again — see the 2026-07-09
    "Marina" (NORAD 98293) investigation: fetch_provisional_tles() hid it
    immediately right around launch, and nothing ever un-hid it afterward.
    """

    def test_system_hidden_satellite_is_unhidden_when_alive(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98293, 'Marina', 'unknown', 2)"
        )
        db.commit()

        _run_sync(
            db,
            [
                {
                    "norad_cat_id": 98293,
                    "name": "Marina",
                    "names": "OM9MAR",
                    "status": "alive",
                    "norad_follow_id": None,
                }
            ],
        )

        row = db.execute(
            "SELECT status, is_hidden FROM satellites WHERE norad_cat_id = 98293"
        ).fetchone()
        assert row["status"] == "alive"
        assert row["is_hidden"] == 0

    def test_system_hidden_satellite_stays_hidden_when_still_not_alive(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98293, 'Marina', 'unknown', 2)"
        )
        db.commit()

        _run_sync(
            db,
            [
                {
                    "norad_cat_id": 98293,
                    "name": "Marina",
                    "names": "",
                    "status": "unknown",
                    "norad_follow_id": None,
                }
            ],
        )

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 98293").fetchone()
        assert row["is_hidden"] == 2

    def test_user_hidden_satellite_is_not_unhidden_when_alive(self, db: sqlite3.Connection) -> None:
        """is_hidden=1 (user chose to hide it) must never be overridden automatically."""
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98293, 'Marina', 'unknown', 1)"
        )
        db.commit()

        _run_sync(
            db,
            [
                {
                    "norad_cat_id": 98293,
                    "name": "Marina",
                    "names": "",
                    "status": "alive",
                    "norad_follow_id": None,
                }
            ],
        )

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 98293").fetchone()
        assert row["is_hidden"] == 1

    def test_already_visible_satellite_unaffected(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98293, 'Marina', 'alive', 0)"
        )
        db.commit()

        _run_sync(
            db,
            [
                {
                    "norad_cat_id": 98293,
                    "name": "Marina",
                    "names": "",
                    "status": "alive",
                    "norad_follow_id": None,
                }
            ],
        )

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 98293").fetchone()
        assert row["is_hidden"] == 0


class TestSyncSatelliteNamesPageFailure:
    """A page-fetch failure partway through pagination (e.g. SATNOGS becomes
    unreachable, as diagnosed on Windows 2026-07-23 — ConnectError/ConnectTimeout)
    must not discard satellites already processed from earlier pages, and must not
    raise out of sync_satellite_names() (the caller only logs a warning either way,
    but earlier behavior silently lost all progress made on prior pages too).
    """

    def test_first_page_failure_returns_without_raising(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("all connection attempts failed")
        )
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.sync_satellite_names())

        assert stats == {"updated": 0, "skipped": 0}

    def test_later_page_failure_keeps_earlier_pages_committed(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        page1 = MagicMock()
        page1.raise_for_status.return_value = None
        page1.json.return_value = {
            "results": [
                {
                    "norad_cat_id": 25544,
                    "name": "ISS",
                    "names": "ZARYA, RS0ISS",
                    "status": "alive",
                    "norad_follow_id": None,
                }
            ],
            "next": "https://db.satnogs.org/api/satellites/?page=2",
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[page1, httpx.ConnectTimeout("timed out")],
        )
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            stats = asyncio.run(mgr.sync_satellite_names())

        assert stats == {"updated": 1, "skipped": 0}
        row = db.execute(
            "SELECT name, is_hidden FROM satellites WHERE norad_cat_id = 25544"
        ).fetchone()
        assert row is not None
        assert row["name"] == "ISS"
        assert row["is_hidden"] == 0
