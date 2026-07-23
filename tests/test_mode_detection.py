"""Unit tests for comms/mode_detection.py.

is_ax25_telemetry_transmitter() is a pure function (no DB). get_norads_for_tab()
needs a satellites/transmitters schema — built directly here since
database.py's full schema setup isn't needed for this narrow join.
"""

from __future__ import annotations

import sqlite3

import pytest

from comms.mode_detection import (
    get_norads_for_tab,
    is_ax25_telemetry_transmitter,
    is_ax100_digi_transmitter,
)

# ---------------------------------------------------------------------------
# is_ax25_telemetry_transmitter()
# ---------------------------------------------------------------------------


def test_afsk_mode_matches_regardless_of_baud_or_description() -> None:
    assert is_ax25_telemetry_transmitter({"mode": "AFSK", "baud": None, "description": ""})
    assert is_ax25_telemetry_transmitter({"mode": "afsk", "baud": 1200, "description": "Beacon"})


def test_4800_with_ax25_in_description_matches() -> None:
    xpdr = {"mode": "GMSK", "baud": 4800, "description": "Mode U - GMSK4k8 - AX.25"}
    assert is_ax25_telemetry_transmitter(xpdr)


def test_9600_with_ax25_no_dot_matches() -> None:
    xpdr = {"mode": "FSK", "baud": 9600, "description": "9k6 AX25 Telemetry"}
    assert is_ax25_telemetry_transmitter(xpdr)


def test_4800_without_ax25_mention_does_not_match() -> None:
    """Baud rate alone isn't a reliable signal — some satellites run
    non-AX.25 protocols at the same rates (unlike 1200, which SATNOGS's
    "AFSK" mode tag reliably identifies as AX.25 on its own)."""
    xpdr = {"mode": "GMSK", "baud": 4800, "description": "Mode U - GMSK Telemetry"}
    assert not is_ax25_telemetry_transmitter(xpdr)


def test_1200_baud_without_afsk_mode_does_not_match() -> None:
    xpdr = {"mode": "FM", "baud": 1200, "description": "Some AX.25 beacon"}
    assert not is_ax25_telemetry_transmitter(xpdr)


def test_unrelated_transmitter_does_not_match() -> None:
    xpdr = {"mode": "USB", "baud": None, "description": "Linear transponder"}
    assert not is_ax25_telemetry_transmitter(xpdr)


# ---------------------------------------------------------------------------
# get_norads_for_tab("telemetry")
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        """CREATE TABLE transmitters (
            uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, description TEXT,
            mode TEXT, baud INTEGER, alive INTEGER
        )"""
    )
    return c


def _add_sat(conn: sqlite3.Connection, norad: int, name: str, hidden: int = 0) -> None:
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (?, ?, ?)",
        (norad, name, hidden),
    )


def _add_xmit(
    conn: sqlite3.Connection,
    uuid: str,
    norad: int,
    *,
    description: str = "",
    mode: str = "",
    baud: int | None = None,
    alive: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uuid, norad, description, mode, baud, alive),
    )


def test_get_norads_includes_afsk_transmitter(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 25544, "ISS")
    _add_xmit(conn, "u1", 25544, mode="AFSK", baud=1200)
    assert get_norads_for_tab(conn, "telemetry") == [25544]


def test_get_norads_includes_gmsk_4800_with_ax25_description(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 68796, "ARICA-2")
    _add_xmit(conn, "u1", 68796, mode="GMSK", baud=4800, description="GMSK4k8 - AX.25")
    assert get_norads_for_tab(conn, "telemetry") == [68796]


def test_get_norads_excludes_hidden_satellite(conn: sqlite3.Connection) -> None:
    """A satellite whose transmitter still says alive=1 but the satellite
    itself was hidden (e.g. decayed, auto-cleaned) must not appear —
    matches the rationale already established for the other tabs."""
    _add_sat(conn, 47311, "Maya-2", hidden=2)
    _add_xmit(conn, "u1", 47311, mode="AFSK", baud=1200)
    assert get_norads_for_tab(conn, "telemetry") == []


def test_get_norads_excludes_dead_transmitter(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 40908, "LilacSat-2")
    _add_xmit(conn, "u1", 40908, mode="AFSK", baud=1200, alive=0)
    assert get_norads_for_tab(conn, "telemetry") == []


def test_get_norads_excludes_non_matching_transmitter(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 99999, "Linear Sat")
    _add_xmit(conn, "u1", 99999, mode="USB", baud=None)
    assert get_norads_for_tab(conn, "telemetry") == []


def test_get_norads_empty_for_unconfigured_tab_key(conn: sqlite3.Connection) -> None:
    assert get_norads_for_tab(conn, "cw") == []
    assert get_norads_for_tab(conn, "no-such-tab") == []


# ---------------------------------------------------------------------------
# is_ax100_digi_transmitter() / get_norads_for_tab("ax100digi")
# ---------------------------------------------------------------------------


def test_ax100_digi_matches_only_marmotsat_norad() -> None:
    assert is_ax100_digi_transmitter({"norad_cat_id": 98272})
    assert not is_ax100_digi_transmitter({"norad_cat_id": 53106})  # GreenCube — excluded
    assert not is_ax100_digi_transmitter({"norad_cat_id": None})


def test_get_norads_ax100_digi_includes_marmotsat(conn: sqlite3.Connection) -> None:
    _add_sat(conn, 98272, "MARMOTSat")
    _add_xmit(
        conn, "u1", 98272, mode="AFSK", baud=1200, description="Mode V - AFSK1k2 - APRS Digipeater"
    )
    assert get_norads_for_tab(conn, "ax100digi") == [98272]


def test_get_norads_ax100_digi_excludes_greencube(conn: sqlite3.Connection) -> None:
    """GreenCube (IO-117) uses the same AX100 framing but is out of service
    and intentionally excluded (2026-07 user request)."""
    _add_sat(conn, 53106, "GreenCube")
    _add_xmit(conn, "u1", 53106, mode="GMSK", baud=1200, description="Digipeater")
    assert get_norads_for_tab(conn, "ax100digi") == []
