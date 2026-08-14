"""Tests for data.transmitter_manager.TransmitterManager.sync_satellite_names().

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


class TestMigrationPipelinePlaceholderGuard:
    """_run_migration_pipeline()'s "official satellite already has
    transmitters" guard used to refuse to run at all in that case. But
    TransmitterManager.sync_from_satnogs() runs before sync_satellite_names()
    in the normal startup sequence and can itself legitimately create the
    official satellite's row (still under a "#NNNNN" placeholder name) with
    its real transmitters already attached, routed there via the transmitter
    payload's own norad_follow_id -- before this pipeline ever gets a chance
    to run for that same pair. The old guard treated that exactly like the
    Coconut/ISS hijack case it was meant to prevent, permanently blocking the
    rename/satnogs_source_id/hide steps. Confirmed for ARICA-2 / NORAD 68796
    <- 98329 on a fresh Windows install (2026-08-13): 68796 sat forever under
    the name "#68796" with its 3 transmitters correctly attached, while 98329
    (holding the real name "ARICA-2") stayed is_hidden=2 forever.

    The guard now also checks whether the official satellite's name is still
    a placeholder -- only a *confirmed* (non-placeholder) name means it's a
    genuinely unrelated, already-established satellite.
    """

    def test_migrates_when_official_side_has_transmitters_under_placeholder_name(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98329, 'ARICA-2', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (68796, '#68796', 'alive', 0)"
        )
        for i in range(3):
            db.execute(
                "INSERT INTO transmitters (uuid, norad_cat_id, description, source)"
                " VALUES (?, 68796, 'Xpdr', 'satnogs')",
                (f"uuid-{i}",),
            )
        db.commit()

        _run_sync(
            db,
            [
                {
                    "norad_cat_id": 98329,
                    "name": "ARICA-2",
                    "names": "JS1YSD",
                    "status": "alive",
                    "norad_follow_id": 68796,
                }
            ],
        )

        official = db.execute(
            "SELECT name, satnogs_source_id FROM satellites WHERE norad_cat_id = 68796"
        ).fetchone()
        assert official["name"] == "ARICA-2"
        assert official["satnogs_source_id"] == 98329
        remnant = db.execute(
            "SELECT is_hidden FROM satellites WHERE norad_cat_id = 98329"
        ).fetchone()
        assert remnant["is_hidden"] == 2
        # The 3 already-correctly-routed transmitters must not be duplicated.
        count = db.execute(
            "SELECT COUNT(*) FROM transmitters WHERE norad_cat_id = 68796"
        ).fetchone()[0]
        assert count == 3

    def test_does_not_migrate_when_official_side_has_a_confirmed_name(
        self, db: sqlite3.Connection
    ) -> None:
        """Regression guard for the Coconut/ISS scenario the original check
        existed for: an established satellite's own real name must still
        block an unrelated provisional ID from being linked to it.
        """
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (25544, 'ISS', 'alive', 0)"
        )
        db.execute(
            "INSERT INTO transmitters (uuid, norad_cat_id, description, source)"
            " VALUES ('iss-uuid', 25544, 'ISS Xpdr', 'satnogs')"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98292, 'Coconut', 'alive', 0)"
        )
        db.commit()

        _run_sync(
            db,
            [
                {
                    "norad_cat_id": 98292,
                    "name": "Coconut",
                    "names": "",
                    "status": "alive",
                    "norad_follow_id": 25544,
                }
            ],
        )

        official = db.execute(
            "SELECT name, satnogs_source_id FROM satellites WHERE norad_cat_id = 25544"
        ).fetchone()
        assert official["name"] == "ISS"
        assert official["satnogs_source_id"] is None
        count = db.execute(
            "SELECT COUNT(*) FROM transmitters WHERE norad_cat_id = 25544"
        ).fetchone()[0]
        assert count == 1


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


class TestIsSatnogsTransmittersStale:
    """The startup path only auto-syncs SATNOGS transmitters when the DB has
    zero source='satnogs' rows (a genuine first launch), otherwise relying
    entirely on the 168h (7-day) APScheduler interval job -- which only
    fires after 7 continuous days of uptime, so a user who never leaves the
    app open that long would never see the transmitter DB refresh again
    after the initial sync (reported 2026-08-11).
    is_satnogs_transmitters_stale() mirrors TLEManager.is_active_tle_stale()
    exactly, keyed on the 'satnogs' sync_log entries sync_from_satnogs()
    already writes via _log_sync().
    """

    def test_true_when_never_synced(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        assert mgr.is_satnogs_transmitters_stale() is True

    def test_false_when_recently_synced(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        mgr._log_sync("satnogs", {"inserted": 1, "updated": 0})
        assert mgr.is_satnogs_transmitters_stale() is False

    def test_true_once_older_than_the_default_168h(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        old = (datetime.now(UTC) - timedelta(hours=169)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs', ?, ?, 'success', 0)",
            (old, old),
        )
        db.commit()
        assert mgr.is_satnogs_transmitters_stale() is True

    def test_false_just_under_the_default_168h(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        recent = (datetime.now(UTC) - timedelta(hours=167)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs', ?, ?, 'success', 0)",
            (recent, recent),
        )
        db.commit()
        assert mgr.is_satnogs_transmitters_stale() is False

    def test_respects_a_custom_max_age_hours(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs', ?, ?, 'success', 0)",
            (two_hours_ago, two_hours_ago),
        )
        db.commit()
        assert mgr.is_satnogs_transmitters_stale(max_age_hours=1.0) is True
        assert mgr.is_satnogs_transmitters_stale(max_age_hours=4.0) is False

    def test_unrelated_sync_types_are_ignored(self, db: sqlite3.Connection) -> None:
        """A fresh satnogs_names entry (sync_satellite_names(), a different
        sync) must not make the transmitter sync look fresh too."""
        mgr = TransmitterManager(db)
        mgr._log_sync("satnogs_names", {"updated": 1, "skipped": 0})
        assert mgr.is_satnogs_transmitters_stale() is True


class TestIsSatelliteNamesStale:
    """The startup path re-ran the full ~2700-satellite paginated
    sync_satellite_names() on every single restart with no staleness gate
    at all -- unlike every other step in the same startup chain, which
    already had one (reported 2026-08-13). is_satellite_names_stale()
    mirrors is_satnogs_transmitters_stale() exactly, keyed on the
    'satnogs_names' sync_log entries sync_satellite_names() already writes
    via _log_sync().
    """

    def test_true_when_never_synced(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        assert mgr.is_satellite_names_stale() is True

    def test_false_when_recently_synced(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        mgr._log_sync("satnogs_names", {"updated": 1, "skipped": 0})
        assert mgr.is_satellite_names_stale() is False

    def test_true_once_older_than_the_default_24h(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs_names', ?, ?, 'success', 0)",
            (old, old),
        )
        db.commit()
        assert mgr.is_satellite_names_stale() is True

    def test_false_just_under_the_default_24h(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        recent = (datetime.now(UTC) - timedelta(hours=23)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs_names', ?, ?, 'success', 0)",
            (recent, recent),
        )
        db.commit()
        assert mgr.is_satellite_names_stale() is False

    def test_respects_a_custom_max_age_hours(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        db.execute(
            "INSERT INTO sync_log (sync_type, started_at, finished_at, status,"
            " records_updated) VALUES ('satnogs_names', ?, ?, 'success', 0)",
            (two_hours_ago, two_hours_ago),
        )
        db.commit()
        assert mgr.is_satellite_names_stale(max_age_hours=1.0) is True
        assert mgr.is_satellite_names_stale(max_age_hours=4.0) is False

    def test_unrelated_sync_types_are_ignored(self, db: sqlite3.Connection) -> None:
        """A fresh satnogs (transmitter) entry must not make the satellite
        names sync look fresh too."""
        mgr = TransmitterManager(db)
        mgr._log_sync("satnogs", {"inserted": 1, "updated": 0})
        assert mgr.is_satellite_names_stale() is True


def _mock_uuid_response(results: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = results
    resp.raise_for_status.return_value = None
    return resp


class TestFetchSatnogsTransmitter:
    """fetch_satnogs_transmitter() -- single-UUID counterpart to
    sync_from_satnogs(), used by TransmitterDialog's "Reset to SatNOGS
    Official Value" button (GitHub Issue #20 follow-up: undoing a manual
    edit that turned out wrong, e.g. an incompatible Mode, without being
    blocked by manual_override protection)."""

    def test_maps_fields_same_as_bulk_sync(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=_mock_uuid_response(
                [
                    {
                        "uuid": "kjb3TFADq77qj2AFSzxHCV",
                        "description": "Mode U/V Linear",
                        "type": "Transponder",
                        "uplink_low": 435130000,
                        "uplink_high": 435150000,
                        "downlink_low": 145950000,
                        "downlink_high": 145970000,
                        "mode": "USB",
                        "invert": True,
                        "ctcss_tone": None,
                    }
                ]
            )
        )
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            rec = asyncio.run(mgr.fetch_satnogs_transmitter("kjb3TFADq77qj2AFSzxHCV"))

        assert rec == {
            "description": "Mode U/V Linear",
            "type": "Transponder",
            "uplink_low": 435130000,
            "uplink_high": 435150000,
            "downlink_low": 145950000,
            "downlink_high": 145970000,
            "mode": "USB",
            "invert": 1,
            "ctcss_tone": None,
            "ctcss_tone_type": None,
        }
        # Passed uuid as a query param, not a path segment.
        mock_client.get.assert_awaited_once()
        _, kwargs = mock_client.get.call_args
        assert kwargs["params"]["uuid"] == "kjb3TFADq77qj2AFSzxHCV"

    def test_ctcss_extracted_from_description_when_api_field_missing(
        self, db: sqlite3.Connection
    ) -> None:
        mgr = TransmitterManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=_mock_uuid_response(
                [
                    {
                        "uuid": "abc",
                        "description": "FM repeater, CTCSS 88.5Hz",
                        "downlink_low": 145900000,
                        "mode": "FM",
                    }
                ]
            )
        )
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            rec = asyncio.run(mgr.fetch_satnogs_transmitter("abc"))

        assert rec is not None
        assert rec["ctcss_tone"] == 88.5

    def test_uuid_no_longer_in_satnogs_returns_none(self, db: sqlite3.Connection) -> None:
        mgr = TransmitterManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_uuid_response([]))
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            rec = asyncio.run(mgr.fetch_satnogs_transmitter("gone-uuid"))

        assert rec is None

    def test_connection_failure_propagates(self, db: sqlite3.Connection) -> None:
        """Unlike the "not found" case (returns None), a connectivity
        failure must raise so the caller can show the same "cannot connect
        to SatNOGS" messaging used elsewhere in the app."""
        mgr = TransmitterManager(db)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("all connection attempts failed")
        )
        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            with pytest.raises(httpx.ConnectError):
                asyncio.run(mgr.fetch_satnogs_transmitter("abc"))
