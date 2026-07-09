"""Tests for data.transmitter_manager.TransmitterManager.sync_satellite_names().

Lightweight (no Qt import) so it is safe to run locally, unlike test_main_window.py.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

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
