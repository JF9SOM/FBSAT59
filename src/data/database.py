"""
Database schema definition and initialization
Migration management with SQLite + alembic
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Satellite basic information
CREATE TABLE IF NOT EXISTS satellites (
    norad_cat_id    INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    alt_names       TEXT DEFAULT '[]',   -- JSON array
    status          TEXT DEFAULT 'unknown'
                    CHECK(status IN ('alive','dead','unknown')),
    is_favorite     INTEGER DEFAULT 0,
    is_hidden       INTEGER DEFAULT 0,
    satnogs_source_id INTEGER DEFAULT NULL,  -- provisional NORAD for SATNOGS transmitter query
    tle_no_result_since DATETIME DEFAULT NULL,  -- set when no TLE found; auto-hide after 30 days
    favorite_group  INTEGER DEFAULT 0,       -- 0=none, 1..N=custom_groups.id
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Transponder/transmitter information (SATNOGS + manual additions)
CREATE TABLE IF NOT EXISTS transmitters (
    uuid            TEXT PRIMARY KEY,
    norad_cat_id    INTEGER NOT NULL REFERENCES satellites(norad_cat_id)
                    ON DELETE CASCADE,
    description     TEXT NOT NULL,
    type            TEXT DEFAULT 'Transponder'
                    CHECK(type IN ('Transmitter','Transponder','Beacon','Transceiver')),
    uplink_low      INTEGER,            -- Hz (None means receive-only)
    uplink_high     INTEGER,            -- Hz (upper bound for band-type transponders)
    downlink_low    INTEGER,            -- Hz
    downlink_high   INTEGER,            -- Hz
    mode            TEXT,               -- 'FM','SSB','CW','DIGITALVOICE',...
    invert          INTEGER DEFAULT 0,  -- Inverting transponder (0/1)
    baud            INTEGER,            -- Baud rate for digital modes
    ctcss_tone      REAL,               -- CTCSS/DCS tone frequency (Hz)
    ctcss_tone_type TEXT DEFAULT NULL
                    CHECK(ctcss_tone_type IN ('CTCSS','DCS',NULL)),
    alive           INTEGER DEFAULT 1,
    source          TEXT DEFAULT 'satnogs'
                    CHECK(source IN ('satnogs','manual','community')),
    manual_override INTEGER DEFAULT 0,  -- If 1, will not be overwritten by SATNOGS sync
    notes           TEXT DEFAULT '',    -- User notes
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Latest TLE data
CREATE TABLE IF NOT EXISTS tle_data (
    norad_cat_id    INTEGER PRIMARY KEY REFERENCES satellites(norad_cat_id)
                    ON DELETE CASCADE,
    name            TEXT,
    line1           TEXT NOT NULL,
    line2           TEXT NOT NULL,
    epoch           DATETIME,
    source          TEXT DEFAULT 'celestrak'
                    CHECK(source IN ('celestrak','space-track','amsat','manual','satnogs')),
    tle_group       TEXT DEFAULT 'amateur',
    fetched_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    quality_score   TEXT DEFAULT 'unknown'
                    CHECK(quality_score IN ('excellent','good','fair','poor','unknown'))
);

-- TLE history (retains past TLEs for quality trend analysis)
CREATE TABLE IF NOT EXISTS tle_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_cat_id    INTEGER NOT NULL,
    name            TEXT,
    line1           TEXT NOT NULL,
    line2           TEXT NOT NULL,
    epoch           DATETIME,
    source          TEXT,
    fetched_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Application settings and state
CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- User-defined favorite groups (Favorite 1, Favorite 2, Favorite 3, …)
-- id 1..N maps to satellites.favorite_group values.
CREATE TABLE IF NOT EXISTS custom_groups (
    id          INTEGER PRIMARY KEY,  -- group number (1-based)
    name        TEXT NOT NULL,        -- display name (e.g. "Favorite 1")
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- Autotrack lists — user-defined ordered lists of (satellite, transponder) pairs
-- for automatic sequential tracking via the Radio Control panel.
CREATE TABLE IF NOT EXISTS autotrack_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- Entries within an Autotrack list (ordered by sort_order).
-- Each entry links a satellite to a specific transponder UUID.
CREATE TABLE IF NOT EXISTS autotrack_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id       INTEGER NOT NULL REFERENCES autotrack_lists(id) ON DELETE CASCADE,
    norad_cat_id  INTEGER NOT NULL,
    xpdr_uuid     TEXT NOT NULL,   -- transmitters.uuid for the chosen transponder
    sort_order    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT DEFAULT ''
);

-- Sync log
CREATE TABLE IF NOT EXISTS sync_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type       TEXT NOT NULL,  -- 'satnogs','celestrak','space-track'
    started_at      DATETIME,
    finished_at     DATETIME,
    status          TEXT,           -- 'success','error','partial'
    records_updated INTEGER DEFAULT 0,
    error_message   TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_transmitters_norad
    ON transmitters(norad_cat_id);
CREATE INDEX IF NOT EXISTS idx_tle_history_norad
    ON tle_history(norad_cat_id);
CREATE INDEX IF NOT EXISTS idx_tle_history_epoch
    ON tle_history(epoch DESC);
"""


def get_db_path() -> Path:
    """Return the platform-specific database file path"""
    from platformdirs import user_data_dir

    data_dir = Path(user_data_dir("fbsat59", "fbsat59"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "fbsat59.db"


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply migrations to add missing columns to an existing DB."""
    migrations = [
        "ALTER TABLE satellites ADD COLUMN is_favorite INTEGER DEFAULT 0",
        "ALTER TABLE satellites ADD COLUMN is_hidden INTEGER DEFAULT 0",
        "ALTER TABLE tle_data ADD COLUMN tle_group TEXT DEFAULT 'amateur'",
        "ALTER TABLE satellites ADD COLUMN satnogs_uuid TEXT DEFAULT NULL",
        "ALTER TABLE satellites ADD COLUMN satnogs_source_id INTEGER DEFAULT NULL",
        "ALTER TABLE satellites ADD COLUMN tle_no_result_since DATETIME DEFAULT NULL",
        # favorite_group: 0 = not in any favorite group, 1..N = group id
        "ALTER TABLE satellites ADD COLUMN favorite_group INTEGER DEFAULT 0",
    ]
    for stmt in migrations:
        with contextlib.suppress(Exception):
            conn.execute(stmt)
    conn.commit()

    # Seed default custom groups if the table is empty.
    # Existing is_favorite=1 satellites are migrated to favorite_group=1.
    if conn.execute("SELECT COUNT(*) FROM custom_groups").fetchone()[0] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO custom_groups (id, name, sort_order) VALUES (?, ?, ?)",
            [(1, "Favorite 1", 1), (2, "Favorite 2", 2), (3, "Favorite 3", 3)],
        )
        # Migrate legacy is_favorite=1 → favorite_group=1
        conn.execute(
            "UPDATE satellites SET favorite_group = 1 WHERE is_favorite = 1 AND favorite_group = 0"
        )
        conn.commit()

    # Add 'satnogs' to the tle_data.source CHECK constraint (requires table recreation).
    # Also handles recovery when a previous migration was interrupted mid-flight
    # (backup table still exists but the INSERT was not committed).
    _TLE_DATA_COLS = (
        "norad_cat_id, name, line1, line2, epoch, source, tle_group, fetched_at, quality_score"
    )
    tle_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tle_data'"
    ).fetchone()
    backup_exists = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_tle_data_backup'"
        ).fetchone()
        is not None
    )
    needs_migration = tle_row and "'satnogs'" not in tle_row[0]
    if needs_migration or backup_exists:
        if needs_migration:
            conn.execute("DROP TABLE IF EXISTS _tle_data_backup")
            conn.execute("ALTER TABLE tle_data RENAME TO _tle_data_backup")
            conn.execute("""
                CREATE TABLE tle_data (
                    norad_cat_id    INTEGER PRIMARY KEY REFERENCES satellites(norad_cat_id)
                                    ON DELETE CASCADE,
                    name            TEXT,
                    line1           TEXT NOT NULL,
                    line2           TEXT NOT NULL,
                    epoch           DATETIME,
                    source          TEXT DEFAULT 'celestrak'
                                    CHECK(source IN (
                                        'celestrak','space-track','amsat','manual','satnogs'
                                    )),
                    tle_group       TEXT DEFAULT 'amateur',
                    fetched_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                    quality_score   TEXT DEFAULT 'unknown'
                                    CHECK(quality_score IN (
                                        'excellent','good','fair','poor','unknown'
                                    ))
                )
            """)
        # Use explicit column list to avoid column-order mismatch between the
        # old schema (tle_group appended last via ALTER TABLE) and the new schema
        # (tle_group in its canonical position).  INSERT OR IGNORE skips any row
        # that already exists in the target (safe for re-runs after a crash).
        conn.execute(
            f"INSERT OR IGNORE INTO tle_data ({_TLE_DATA_COLS})"
            f" SELECT {_TLE_DATA_COLS} FROM _tle_data_backup"
        )
        conn.execute("DROP TABLE _tle_data_backup")
        conn.commit()

    # Add 'Transceiver' and 'community' to the transmitters CHECK constraints
    # (requires table recreation when either value is missing).
    tx_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='transmitters'"
    ).fetchone()
    needs_tx_migration = tx_row and (
        "'Transceiver'" not in tx_row[0] or "'community'" not in tx_row[0]
    )
    if needs_tx_migration:
        _TX_COLS = (
            "uuid, norad_cat_id, description, type,"
            " uplink_low, uplink_high, downlink_low, downlink_high,"
            " mode, invert, baud, ctcss_tone, ctcss_tone_type,"
            " alive, source, manual_override, notes, updated_at"
        )
        conn.execute("DROP TABLE IF EXISTS _transmitters_backup")
        conn.execute("ALTER TABLE transmitters RENAME TO _transmitters_backup")
        conn.execute("""
            CREATE TABLE transmitters (
                uuid            TEXT PRIMARY KEY,
                norad_cat_id    INTEGER NOT NULL REFERENCES satellites(norad_cat_id)
                                ON DELETE CASCADE,
                description     TEXT NOT NULL,
                type            TEXT DEFAULT 'Transponder'
                                CHECK(type IN ('Transmitter','Transponder','Beacon','Transceiver')),
                uplink_low      INTEGER,
                uplink_high     INTEGER,
                downlink_low    INTEGER,
                downlink_high   INTEGER,
                mode            TEXT,
                invert          INTEGER DEFAULT 0,
                baud            INTEGER,
                ctcss_tone      REAL,
                ctcss_tone_type TEXT DEFAULT NULL
                                CHECK(ctcss_tone_type IN ('CTCSS','DCS',NULL)),
                alive           INTEGER DEFAULT 1,
                source          TEXT DEFAULT 'satnogs'
                                CHECK(source IN ('satnogs','manual','community')),
                manual_override INTEGER DEFAULT 0,
                notes           TEXT DEFAULT '',
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            f"INSERT OR IGNORE INTO transmitters ({_TX_COLS})"
            f" SELECT {_TX_COLS} FROM _transmitters_backup"
        )
        conn.execute("DROP TABLE _transmitters_backup")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transmitters_norad ON transmitters(norad_cat_id)"
        )
        conn.commit()

    # Data repair: TransmitterManager.sync_from_satnogs() used to seed a new
    # satellite's placeholder name from the transmitter's own description
    # (e.g. "Mode U - CW") instead of a recognizable "#NNNNN" placeholder.
    # Since only names that look like a placeholder get corrected once the
    # real name is known, satellites created this way were stuck with a
    # garbage name forever even after the correct name became known on their
    # (now-hidden) provisional-NORAD counterpart. Repair by copying the name
    # from satnogs_source_id's satellite row whenever they differ. Safe to
    # run on every startup: a no-op once the names already match.
    conn.execute(
        """
        UPDATE satellites
        SET name = (
                SELECT p.name FROM satellites p
                WHERE p.norad_cat_id = satellites.satnogs_source_id
            ),
            updated_at = CURRENT_TIMESTAMP
        WHERE satnogs_source_id IS NOT NULL
          AND EXISTS (
                SELECT 1 FROM satellites p WHERE p.norad_cat_id = satellites.satnogs_source_id
            )
          AND name != (
                SELECT p.name FROM satellites p
                WHERE p.norad_cat_id = satellites.satnogs_source_id
            )
        """
    )
    conn.commit()


def init_database(db_path: Path | None = None) -> sqlite3.Connection:
    """
    Initialize the database and return the connection.
    For an existing DB, validates the schema and creates only missing tables.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _apply_migrations(conn)
    conn.commit()
    return conn
