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

    def test_partial_failure_does_not_hide_unprocessed_satellite(
        self, db: sqlite3.Connection
    ) -> None:
        """A satellite this run never got to (e.g. its row was just created by a
        concurrent TLE fetch, still status='unknown' with no transmitters yet) must
        not be treated as a confirmed orphan when the page fetch fails before this
        run has walked the whole catalog — the auto-hide cleanup at the end of
        sync_satellite_names() previously ran unconditionally even after a partial
        fetch, hiding satellites the run simply hadn't reached yet (root cause of
        ISS intermittently vanishing on a fresh macOS install, 2026-08-01).
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (25544, 'ISS', 'unknown', 0)"
        )
        db.commit()

        mgr = TransmitterManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("all connection attempts failed")
        )
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.sync_satellite_names())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 25544").fetchone()
        assert row["is_hidden"] == 0


class TestUpdateTransmitterRxOffset:
    """update_transmitter(uuid, rx_offset_hz=...) -- GitHub Issue #18's
    persistent per-transponder RX offset (Radio Control tab's Offset spinbox)."""

    def test_rx_offset_hz_is_persisted(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        xpdr_uuid = mgr.add_manual_transmitter(
            norad_cat_id=44909,
            description="RS-44 FT4",
            downlink_low=435612000,
            mode="USB-D",
        )

        mgr.update_transmitter(xpdr_uuid, rx_offset_hz=-1240.0)

        row = db.execute(
            "SELECT rx_offset_hz FROM transmitters WHERE uuid = ?", (xpdr_uuid,)
        ).fetchone()
        assert row["rx_offset_hz"] == -1240.0

        # get_transmitters() must surface it too (Radio Control reads the
        # transponder dict returned from here, not raw SQL).
        xpdrs = mgr.get_transmitters(44909)
        assert xpdrs[0]["rx_offset_hz"] == -1240.0

    def test_rx_offset_hz_survives_community_resync(self, db: sqlite3.Connection) -> None:
        """A locally-set rx_offset_hz must not be wiped out by a later
        community/SATNOGS transmitter resync -- update_transmitter() and the
        sync UPDATE statements both use explicit column lists that never
        touch rx_offset_hz."""
        mgr = TransmitterManager(db)
        xpdr_uuid = mgr.add_manual_transmitter(
            norad_cat_id=44909,
            description="RS-44 FT4",
            downlink_low=435612000,
            mode="USB-D",
            manual_override=False,
        )
        mgr.update_transmitter(xpdr_uuid, rx_offset_hz=-1240.0)

        # Simulate what a resync would do to every other column, using the
        # exact explicit-column UPDATE style the sync methods use.
        db.execute(
            "UPDATE transmitters SET description = ?, downlink_low = ? WHERE uuid = ?",
            ("RS-44 FT4 (resynced)", 435612500, xpdr_uuid),
        )
        db.commit()

        row = db.execute(
            "SELECT rx_offset_hz, description FROM transmitters WHERE uuid = ?", (xpdr_uuid,)
        ).fetchone()
        assert row["rx_offset_hz"] == -1240.0
        assert row["description"] == "RS-44 FT4 (resynced)"
