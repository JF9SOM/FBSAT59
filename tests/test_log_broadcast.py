"""Unit tests for comms/log_broadcast.py (UDP ADIF log broadcaster).

No LAN access is required — all tests bind a real UDP socket on 127.0.0.1
with an OS-assigned ephemeral port and read back what LogBroadcaster sends.
"""

from __future__ import annotations

import socket
import sqlite3

import pytest

from comms.log_broadcast import (
    LogBroadcaster,
    get_log_broadcaster,
    load_log_broadcast_settings,
    save_log_broadcast_settings,
)
from data.database import SCHEMA_SQL
from ui.adif_utils import adif_field, build_adif_record

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory SQLite DB with the full schema (provides app_settings)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


@pytest.fixture()
def udp_listener() -> tuple[socket.socket, str, int]:
    """A bound UDP socket on 127.0.0.1 with an OS-assigned port, plus its address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.5)
    host, port = sock.getsockname()
    yield sock, host, port
    sock.close()


# ---------------------------------------------------------------------------
# build_adif_record() / adif_field()
# ---------------------------------------------------------------------------


def test_adif_field_formats_length_prefixed_tag() -> None:
    assert adif_field("CALL", "JF9SOM") == "<CALL:6>JF9SOM"


def test_adif_field_omits_blank_value() -> None:
    assert adif_field("COMMENT", "") == ""
    assert adif_field("COMMENT", "   ") == ""


def test_build_adif_record_skips_blank_fields_and_appends_eor() -> None:
    record = build_adif_record({"CALL": "JA1XYZ", "COMMENT": "", "MODE": "FT4"})
    assert record == "<CALL:6>JA1XYZ <MODE:3>FT4 <EOR>\n"


def test_build_adif_record_preserves_insertion_order() -> None:
    record = build_adif_record({"B": "2", "A": "1"})
    assert record.index("<B:1>2") < record.index("<A:1>1")


# ---------------------------------------------------------------------------
# load_log_broadcast_settings() / save_log_broadcast_settings()
# ---------------------------------------------------------------------------


def test_load_settings_defaults_when_unset(db: sqlite3.Connection) -> None:
    settings = load_log_broadcast_settings(db)
    assert settings == {"enabled": False, "host": "127.0.0.1", "port": 2333}


def test_save_then_load_settings_roundtrip(db: sqlite3.Connection) -> None:
    save_log_broadcast_settings(db, {"enabled": True, "host": "192.168.1.50", "port": 2334})
    settings = load_log_broadcast_settings(db)
    assert settings == {"enabled": True, "host": "192.168.1.50", "port": 2334}


def test_load_settings_survives_malformed_json(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('log_broadcast_settings', ?)",
        ("not json",),
    )
    db.commit()
    settings = load_log_broadcast_settings(db)
    assert settings == {"enabled": False, "host": "127.0.0.1", "port": 2333}


# ---------------------------------------------------------------------------
# LogBroadcaster
# ---------------------------------------------------------------------------


def test_send_is_noop_when_disabled(
    db: sqlite3.Connection, udp_listener: tuple[socket.socket, str, int]
) -> None:
    sock, host, port = udp_listener
    save_log_broadcast_settings(db, {"enabled": False, "host": host, "port": port})

    broadcaster = LogBroadcaster()
    broadcaster.reload_settings(db)
    broadcaster.send_adif_record("<CALL:6>JA1XYZ <EOR>\n")

    with pytest.raises(TimeoutError):
        sock.recvfrom(2048)


def test_send_delivers_adif_text_when_enabled(
    db: sqlite3.Connection, udp_listener: tuple[socket.socket, str, int]
) -> None:
    sock, host, port = udp_listener
    save_log_broadcast_settings(db, {"enabled": True, "host": host, "port": port})

    broadcaster = LogBroadcaster()
    broadcaster.reload_settings(db)
    record = build_adif_record({"CALL": "JA1XYZ", "MODE": "FT4"})
    broadcaster.send_adif_record(record)

    data, _addr = sock.recvfrom(2048)
    assert data.decode("utf-8") == record


def test_reload_settings_picks_up_updated_host_port(
    db: sqlite3.Connection, udp_listener: tuple[socket.socket, str, int]
) -> None:
    sock, host, port = udp_listener
    save_log_broadcast_settings(db, {"enabled": True, "host": "127.0.0.1", "port": 1})

    broadcaster = LogBroadcaster()
    broadcaster.reload_settings(db)

    # Change destination and reload again before sending.
    save_log_broadcast_settings(db, {"enabled": True, "host": host, "port": port})
    broadcaster.reload_settings(db)
    broadcaster.send_adif_record("<CALL:3>ABC <EOR>\n")

    data, _addr = sock.recvfrom(2048)
    assert data == b"<CALL:3>ABC <EOR>\n"


def test_get_log_broadcaster_returns_singleton() -> None:
    assert get_log_broadcaster() is get_log_broadcaster()
