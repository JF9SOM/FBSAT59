"""The aprs_log table: creation and upgrade."""

from __future__ import annotations

import sqlite3


def ensure_aprs_log_schema(conn: sqlite3.Connection) -> None:
    """Create aprs_log, or add the columns an older database is missing.

    ``freq_hz`` / ``freq_rx_hz`` (the uplink / downlink band in Hz, taken from the
    transponder selected when the packet was logged) were added later, so that an
    ADIF record can carry the right BAND for 70 cm digipeaters as well as 2 m ones.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aprs_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at   DATETIME NOT NULL,
            callsign      TEXT NOT NULL,
            via           TEXT,
            latitude_deg  REAL,
            longitude_deg REAL,
            comment       TEXT,
            raw_frame     TEXT,
            norad_sat     INTEGER,
            freq_hz       INTEGER,
            freq_rx_hz    INTEGER
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(aprs_log)")}
    for name in ("freq_hz", "freq_rx_hz"):
        if name not in columns:
            conn.execute(f"ALTER TABLE aprs_log ADD COLUMN {name} INTEGER")  # noqa: S608 - fixed names
    conn.commit()
