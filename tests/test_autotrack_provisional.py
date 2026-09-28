"""Autotrack entries follow a satellite from its provisional NORAD ID to the official one."""

from __future__ import annotations

import sqlite3

import pytest

from data.database import SCHEMA_SQL, _apply_migrations
from data.transmitter_manager import TransmitterManager

FAKE_ID = 98292
REAL_ID = 69000


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    for norad, name in ((25544, "ISS"), (FAKE_ID, "Coconut"), (REAL_ID, "Official")):
        c.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)", (norad, name))
    c.execute("INSERT INTO autotrack_lists (id, name) VALUES (1, 'L')")
    c.commit()
    return c


def _add_entry(c: sqlite3.Connection, norad: int, uuid: str) -> None:
    c.execute(
        "INSERT INTO autotrack_entries (list_id, norad_cat_id, xpdr_uuid) VALUES (1, ?, ?)",
        (norad, uuid),
    )
    c.commit()


def _entry_norads(c: sqlite3.Connection) -> dict[str, int]:
    return {
        str(r["xpdr_uuid"]): int(r["norad_cat_id"])
        for r in c.execute("SELECT xpdr_uuid, norad_cat_id FROM autotrack_entries")
    }


def test_pipeline_repoints_entries_and_leaves_others(conn: sqlite3.Connection) -> None:
    _add_entry(conn, FAKE_ID, "tx-fake")
    _add_entry(conn, 25544, "tx-iss")
    TransmitterManager(conn)._run_migration_pipeline(FAKE_ID, REAL_ID)
    assert _entry_norads(conn) == {"tx-fake": REAL_ID, "tx-iss": 25544}


def test_pipeline_rerun_is_harmless(conn: sqlite3.Connection) -> None:
    _add_entry(conn, FAKE_ID, "tx-fake")
    tm = TransmitterManager(conn)
    tm._run_migration_pipeline(FAKE_ID, REAL_ID)
    tm._run_migration_pipeline(FAKE_ID, REAL_ID)
    assert _entry_norads(conn) == {"tx-fake": REAL_ID}


def test_startup_repair_fixes_entries_stranded_by_old_pipeline(conn: sqlite3.Connection) -> None:
    # State left by the old pipeline: official row linked, provisional row hidden,
    # Autotrack entry still on the provisional ID.
    conn.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = ?", (FAKE_ID,))
    conn.execute(
        "UPDATE satellites SET satnogs_source_id = ? WHERE norad_cat_id = ?", (FAKE_ID, REAL_ID)
    )
    _add_entry(conn, FAKE_ID, "tx-fake")
    _apply_migrations(conn)
    assert _entry_norads(conn) == {"tx-fake": REAL_ID}


def test_startup_repair_ignores_unlinked_provisional(conn: sqlite3.Connection) -> None:
    # A visible provisional satellite with no official counterpart must stay put.
    _add_entry(conn, FAKE_ID, "tx-fake")
    _apply_migrations(conn)
    assert _entry_norads(conn) == {"tx-fake": FAKE_ID}
