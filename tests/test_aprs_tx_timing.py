"""User-adjustable APRS transmit timing: persistence, audio length, dialog."""

from __future__ import annotations

import sqlite3

from comms.aprs.ax25_tx import SAMPLE_RATE, build_ax25_audio
from comms.aprs.tx_timing import MAX_MS, TxTiming, load_tx_timing, save_tx_timing


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME)"
    )
    return conn


def test_defaults_when_nothing_saved() -> None:
    assert load_tx_timing(_conn()) == TxTiming(200, 300, 100, 200)


def test_save_and_load_round_trip_and_clamp() -> None:
    conn = _conn()
    save_tx_timing(conn, TxTiming(10, 250, 10, 5000))
    assert load_tx_timing(conn) == TxTiming(10, 250, 10, MAX_MS)


def test_bad_json_falls_back_to_defaults() -> None:
    conn = _conn()
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('aprs_tx_timing', 'x')")
    assert load_tx_timing(conn) == TxTiming()


def test_txdelay_and_txtail_change_audio_length() -> None:
    frame = b"\x82" * 14 + b"\x03\xf0" + b"hi"
    short = build_ax25_audio(frame, "1200", txdelay_ms=100, txtail_ms=0)
    long = build_ax25_audio(frame, "1200", txdelay_ms=500, txtail_ms=300)
    assert (len(long) - len(short)) / SAMPLE_RATE > 0.55
