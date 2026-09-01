"""
データベース初期化・基本CRUD動作確認テスト
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data.database import SCHEMA_SQL, init_database


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """インメモリDBを使う一時接続"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    """ファイルベースの一時DB接続"""
    return init_database(tmp_path / "test.db")


class TestSchemaInit:
    def test_all_tables_created(self, db_conn: sqlite3.Connection) -> None:
        expected = {
            "satellites",
            "transmitters",
            "tle_data",
            "tle_history",
            "app_settings",
            "sync_log",
        }
        rows = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        created = {r["name"] for r in rows}
        assert expected <= created

    def test_indexes_created(self, db_conn: sqlite3.Connection) -> None:
        rows = db_conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        names = {r["name"] for r in rows}
        assert "idx_transmitters_norad" in names
        assert "idx_tle_history_norad" in names
        assert "idx_tle_history_epoch" in names

    def test_init_is_idempotent(self, tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
        """同じDBに2回 init_database を呼んでもエラーにならない"""
        tmp_db.close()
        conn2 = init_database(tmp_path / "test.db")
        rows = conn2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(rows) >= 6
        conn2.close()


class TestSatelliteCRUD:
    def test_insert_and_select(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status) VALUES (?, ?, ?)",
            (25544, "ISS (ZARYA)", "alive"),
        )
        db_conn.commit()
        row = db_conn.execute("SELECT * FROM satellites WHERE norad_cat_id = 25544").fetchone()
        assert row is not None
        assert row["name"] == "ISS (ZARYA)"
        assert row["status"] == "alive"

    def test_status_constraint(self, db_conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO satellites (norad_cat_id, name, status) VALUES (?, ?, ?)",
                (99999, "BadSat", "invalid_status"),
            )
            db_conn.commit()


class TestTransmitterCRUD:
    def test_insert_transmitter(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (25544, "ISS"),
        )
        db_conn.execute(
            """INSERT INTO transmitters
               (uuid, norad_cat_id, description, downlink_low, mode, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("test-uuid-001", 25544, "ISS VHF FM", 145800000, "FM", "manual"),
        )
        db_conn.commit()
        row = db_conn.execute("SELECT * FROM transmitters WHERE uuid = 'test-uuid-001'").fetchone()
        assert row["downlink_low"] == 145800000
        assert row["mode"] == "FM"

    def test_rx_offset_hz_defaults_to_zero(self, db_conn: sqlite3.Connection) -> None:
        """GitHub Issue #18: new transponder rows start with no persistent
        RX offset until the operator explicitly sets one."""
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (44909, "RS-44"),
        )
        db_conn.execute(
            """INSERT INTO transmitters
               (uuid, norad_cat_id, description, downlink_low, mode, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("rx-offset-default", 44909, "RS-44 FT4", 435612000, "USB-D", "manual"),
        )
        db_conn.commit()
        row = db_conn.execute(
            "SELECT rx_offset_hz FROM transmitters WHERE uuid = 'rx-offset-default'"
        ).fetchone()
        assert row["rx_offset_hz"] == 0

    def test_rx_offset_hz_update_persists(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (44909, "RS-44"),
        )
        db_conn.execute(
            """INSERT INTO transmitters
               (uuid, norad_cat_id, description, downlink_low, mode, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("rx-offset-update", 44909, "RS-44 FT4", 435612000, "USB-D", "manual"),
        )
        db_conn.execute(
            "UPDATE transmitters SET rx_offset_hz = ? WHERE uuid = ?",
            (-1240.0, "rx-offset-update"),
        )
        db_conn.commit()
        row = db_conn.execute(
            "SELECT rx_offset_hz FROM transmitters WHERE uuid = 'rx-offset-update'"
        ).fetchone()
        assert row["rx_offset_hz"] == -1240.0

    def test_cascade_delete(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (12345, "TestSat"),
        )
        db_conn.execute(
            """INSERT INTO transmitters
               (uuid, norad_cat_id, description, source)
               VALUES (?, ?, ?, ?)""",
            ("del-uuid", 12345, "Test TX", "satnogs"),
        )
        db_conn.commit()
        db_conn.execute("DELETE FROM satellites WHERE norad_cat_id = 12345")
        db_conn.commit()
        row = db_conn.execute("SELECT * FROM transmitters WHERE uuid = 'del-uuid'").fetchone()
        assert row is None, "CASCADE DELETE が機能していない"


class TestTleData:
    _LINE1 = "1 25544U 98067A   24001.50000000  .00016717  00000+0  10270-3 0  9994"
    _LINE2 = "2 25544  51.6400 208.9163 0006828  86.9922 273.1770 15.49212693420559"

    def test_insert_tle(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (25544, "ISS"),
        )
        db_conn.execute(
            """INSERT INTO tle_data
               (norad_cat_id, name, line1, line2, source, quality_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (25544, "ISS (ZARYA)", self._LINE1, self._LINE2, "celestrak", "excellent"),
        )
        db_conn.commit()
        row = db_conn.execute("SELECT * FROM tle_data WHERE norad_cat_id = 25544").fetchone()
        assert row["line1"] == self._LINE1
        assert row["quality_score"] == "excellent"

    def test_quality_score_constraint(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (99001, "TestSat2"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """INSERT INTO tle_data
                   (norad_cat_id, name, line1, line2, quality_score)
                   VALUES (?, ?, ?, ?, ?)""",
                (99001, "T", self._LINE1, self._LINE2, "super"),
            )
            db_conn.commit()


class TestAppSettings:
    def test_upsert_setting(self, db_conn: sqlite3.Connection) -> None:
        db_conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            ("last_sync", "2024-01-01T00:00:00"),
        )
        db_conn.commit()
        row = db_conn.execute("SELECT value FROM app_settings WHERE key = 'last_sync'").fetchone()
        assert row["value"] == "2024-01-01T00:00:00"


class TestSatnogsSourceIdRepair:
    """_apply_migrations() self-heal for satnogs_source_id / placeholder names.

    Regression coverage for the ISS (25544) / Coconut (98292) incident
    (2026-08-01): a first-launch race let a stray satnogs_source_id get set
    on ISS pointing at an unrelated, independently-visible satellite. The old
    SQL-only name-repair migration then unconditionally overwrote ISS's real
    name with that satellite's name on every startup, since it only checked
    for a name *difference*, not whether ISS's own name actually needed
    fixing.
    """

    def _reinit(self, tmp_path: Path) -> sqlite3.Connection:
        """Re-run init_database() (and therefore _apply_migrations()) against
        whatever rows were seeded into the DB file beforehand."""
        return init_database(tmp_path / "test.db")

    def test_real_name_is_not_clobbered_by_stray_source_id(self, tmp_path: Path) -> None:
        """A satellite with a real (non-placeholder) name must never be
        overwritten just because satnogs_source_id points at a differently
        named, independently visible satellite (the ISS/Coconut bug)."""
        conn = init_database(tmp_path / "test.db")
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden, satnogs_source_id)"
            " VALUES (25544, 'ISS', 'alive', 0, 98292)"
        )
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98292, 'Coconut', 'alive', 0)"
        )
        conn.commit()
        conn.close()

        conn2 = self._reinit(tmp_path)
        row = conn2.execute(
            "SELECT name, satnogs_source_id FROM satellites WHERE norad_cat_id = 25544"
        ).fetchone()
        assert row["name"] == "ISS"
        assert row["satnogs_source_id"] is None  # stray link cleared
        conn2.close()

    def test_placeholder_name_is_still_repaired_from_hidden_source(self, tmp_path: Path) -> None:
        """The original, legitimate use case must keep working: a satellite
        stuck with a placeholder name gets the real name copied over from
        its (properly hidden) provisional-NORAD counterpart."""
        conn = init_database(tmp_path / "test.db")
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden, satnogs_source_id)"
            " VALUES (68795, '#68795', 'alive', 0, 98325)"
        )
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (98325, 'ORIGAMISAT-2', 'alive', 2)"
        )
        conn.commit()
        conn.close()

        conn2 = self._reinit(tmp_path)
        row = conn2.execute(
            "SELECT name, satnogs_source_id FROM satellites WHERE norad_cat_id = 68795"
        ).fetchone()
        assert row["name"] == "ORIGAMISAT-2"
        assert row["satnogs_source_id"] == 98325  # legitimate link preserved
        conn2.close()

    def test_dangling_source_id_is_cleared(self, tmp_path: Path) -> None:
        """satnogs_source_id pointing at a satellite that no longer exists
        should be cleared rather than left dangling forever."""
        conn = init_database(tmp_path / "test.db")
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden, satnogs_source_id)"
            " VALUES (12345, 'SomeSat', 'alive', 0, 99999)"
        )
        conn.commit()
        conn.close()

        conn2 = self._reinit(tmp_path)
        row = conn2.execute(
            "SELECT satnogs_source_id FROM satellites WHERE norad_cat_id = 12345"
        ).fetchone()
        assert row["satnogs_source_id"] is None
        conn2.close()


class TestSatnogsInOrbitStatusRepair:
    """_apply_migrations() one-time repair for the 2026-09 SatNOGS 'status'
    vocabulary change ('alive' -> 'in orbit'), which flipped ~1700 orbiting
    satellites to a grey 'unknown' on the first sync afterwards.
    """

    def _seed(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "test.db"
        conn = init_database(db_path)
        # init_database() already ran the repair once (no-op on an empty DB) and
        # stamped the marker. Clear it so the corruption seeded below stands in
        # for a DB upgraded from a pre-repair build that never self-healed.
        conn.execute("DELETE FROM app_settings WHERE key = 'db_repair_satnogs_in_orbit_v1'")
        # Corrupted: real catalogue NORAD, has a TLE, non-placeholder name,
        # wrongly auto-hidden.
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (57166, 'METEOR M2-3', 'unknown', 2)"
        )
        conn.execute("INSERT INTO tle_data (norad_cat_id, line1, line2) VALUES (57166, 'l1', 'l2')")
        # Genuinely unknown: no TLE on file -> must be left alone.
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (12345, 'FutureSat', 'unknown', 0)"
        )
        # Provisional placeholder NORAD -> must be left alone even with a TLE.
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (90001, '#90001', 'unknown', 0)"
        )
        conn.execute("INSERT INTO tle_data (norad_cat_id, line1, line2) VALUES (90001, 'l1', 'l2')")
        # User-hidden (is_hidden=1) corrupted row -> status repaired, stays hidden.
        conn.execute(
            "INSERT INTO satellites (norad_cat_id, name, status, is_hidden)"
            " VALUES (33591, 'NOAA 19', 'unknown', 1)"
        )
        conn.execute("INSERT INTO tle_data (norad_cat_id, line1, line2) VALUES (33591, 'l1', 'l2')")
        conn.execute("INSERT INTO sync_log (sync_type, status) VALUES ('satnogs_names', 'success')")
        conn.execute("INSERT INTO sync_log (sync_type, status) VALUES ('celestrak', 'success')")
        conn.commit()
        conn.close()
        return db_path

    def test_repair_restores_status_and_unhides(self, tmp_path: Path) -> None:
        db_path = self._seed(tmp_path)
        conn = init_database(db_path)

        rows = {
            r["norad_cat_id"]: r
            for r in conn.execute("SELECT norad_cat_id, status, is_hidden FROM satellites")
        }
        assert rows[57166]["status"] == "alive"
        assert rows[57166]["is_hidden"] == 0  # auto-hide (2) cleared
        assert rows[33591]["status"] == "alive"
        assert rows[33591]["is_hidden"] == 1  # user-hide (1) preserved
        assert rows[12345]["status"] == "unknown"  # no TLE -> untouched
        assert rows[90001]["status"] == "unknown"  # provisional -> untouched

        # Forces one authoritative re-sync; unrelated sync_log rows kept.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE sync_type = 'satnogs_names'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM sync_log WHERE sync_type = 'celestrak'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT value FROM app_settings WHERE key = 'db_repair_satnogs_in_orbit_v1'"
            ).fetchone()["value"]
            == "2"
        )
        conn.close()

    def test_repair_runs_only_once(self, tmp_path: Path) -> None:
        db_path = self._seed(tmp_path)
        conn = init_database(db_path)
        # Simulate a later legitimate sync marking 57166 unknown again.
        conn.execute("UPDATE satellites SET status = 'unknown' WHERE norad_cat_id = 57166")
        conn.execute("INSERT INTO sync_log (sync_type, status) VALUES ('satnogs_names', 'success')")
        conn.commit()
        conn.close()

        conn2 = init_database(db_path)
        # Marker already set -> repair does not run again, does not re-flip.
        assert (
            conn2.execute("SELECT status FROM satellites WHERE norad_cat_id = 57166").fetchone()[
                "status"
            ]
            == "unknown"
        )
        assert (
            conn2.execute(
                "SELECT COUNT(*) FROM sync_log WHERE sync_type = 'satnogs_names'"
            ).fetchone()[0]
            == 1
        )
        conn2.close()
