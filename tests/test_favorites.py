"""Tests for many-to-many favorite groups and the provisional-ID favorite hand-over."""

from __future__ import annotations

import sqlite3

import pytest

from data import favorites as fav
from data.database import SCHEMA_SQL, _apply_migrations
from data.transmitter_manager import TransmitterManager


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    for norad, name in ((25544, "ISS"), (43017, "AO-92"), (98292, "Coconut"), (69000, "Official")):
        c.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)", (norad, name))
    c.commit()
    return c


def _legacy(c: sqlite3.Connection, norad: int) -> tuple[int, int]:
    row = c.execute(
        "SELECT is_favorite, favorite_group FROM satellites WHERE norad_cat_id = ?", (norad,)
    ).fetchone()
    return int(row[0]), int(row[1])


class TestMembership:
    def test_set_and_get_multiple_groups(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 25544, [3, 1, 3])
        assert fav.get_groups(conn, 25544) == [1, 3]

    def test_toggle_adds_then_removes_independently(self, conn: sqlite3.Connection) -> None:
        fav.toggle_group(conn, 25544, 1)
        fav.toggle_group(conn, 25544, 2)
        assert fav.get_groups(conn, 25544) == [1, 2]
        fav.toggle_group(conn, 25544, 1)
        assert fav.get_groups(conn, 25544) == [2]

    def test_set_empty_clears_all(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 25544, [1, 2])
        fav.set_groups(conn, 25544, [])
        assert fav.get_groups(conn, 25544) == []

    def test_group_id_zero_is_ignored(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 25544, [0, 2])
        assert fav.get_groups(conn, 25544) == [2]

    def test_load_all_memberships(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 25544, [1, 2])
        fav.set_groups(conn, 43017, [2])
        assert fav.load_all_memberships(conn) == {25544: [1, 2], 43017: [2]}

    def test_remove_group_only_drops_that_group(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 25544, [1, 2])
        fav.set_groups(conn, 43017, [2])
        fav.remove_group(conn, 2)
        assert fav.get_groups(conn, 25544) == [1]
        assert fav.get_groups(conn, 43017) == []
        assert _legacy(conn, 43017) == (0, 0)

    def test_legacy_columns_follow_membership(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 25544, [3, 2])
        assert _legacy(conn, 25544) == (1, 2)
        fav.set_groups(conn, 25544, [])
        assert _legacy(conn, 25544) == (0, 0)


class TestMoveGroups:
    def test_move_unions_and_leaves_nothing_behind(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 98292, [1, 2])
        fav.set_groups(conn, 69000, [3])
        fav.move_groups(conn, 98292, 69000)
        assert fav.get_groups(conn, 69000) == [1, 2, 3]
        assert fav.get_groups(conn, 98292) == []

    def test_second_move_does_not_resurrect_removed_group(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 98292, [1, 2])
        fav.move_groups(conn, 98292, 69000)
        fav.toggle_group(conn, 69000, 2)  # user removes group 2 from the official sat
        fav.move_groups(conn, 98292, 69000)  # pipeline runs again
        assert fav.get_groups(conn, 69000) == [1]


class TestMigrationPipeline:
    def test_pipeline_hands_groups_to_official_satellite(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 98292, [1, 3])
        conn.commit()
        TransmitterManager(conn)._run_migration_pipeline(98292, 69000)
        assert fav.get_groups(conn, 69000) == [1, 3]
        assert fav.get_groups(conn, 98292) == []
        assert _legacy(conn, 69000) == (1, 1)

    def test_pipeline_is_idempotent_for_favorites(self, conn: sqlite3.Connection) -> None:
        fav.set_groups(conn, 98292, [1, 2])
        conn.commit()
        tm = TransmitterManager(conn)
        tm._run_migration_pipeline(98292, 69000)
        fav.toggle_group(conn, 69000, 2)
        conn.commit()
        tm._run_migration_pipeline(98292, 69000)
        assert fav.get_groups(conn, 69000) == [1]


class TestDatabaseMigration:
    def test_legacy_single_group_is_moved_once(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE satellites SET favorite_group = 2 WHERE norad_cat_id = 25544")
        conn.commit()
        _apply_migrations(conn)
        assert fav.get_groups(conn, 25544) == [2]
        # A later deliberate removal must survive a restart (marker prevents re-import).
        fav.set_groups(conn, 25544, [])
        conn.execute("UPDATE satellites SET favorite_group = 2 WHERE norad_cat_id = 25544")
        conn.commit()
        _apply_migrations(conn)
        assert fav.get_groups(conn, 25544) == []

    def test_repairs_favorites_lost_by_old_provisional_migration(
        self, conn: sqlite3.Connection
    ) -> None:
        # State left behind by the old pipeline: hidden provisional row keeps the group,
        # the official row is linked but in no group.
        conn.execute(
            "UPDATE satellites SET favorite_group = 2, is_hidden = 2 WHERE norad_cat_id = 98292"
        )
        conn.execute("UPDATE satellites SET satnogs_source_id = 98292 WHERE norad_cat_id = 69000")
        conn.commit()
        _apply_migrations(conn)
        assert fav.get_groups(conn, 69000) == [2]
        assert fav.get_groups(conn, 98292) == []
