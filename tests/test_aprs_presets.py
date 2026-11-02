"""Canned messages for the APRS tab's Send form."""

from __future__ import annotations

import sqlite3

from ui.aprs_presets import (
    DEFAULT_PRESETS,
    MAX_MESSAGE_LEN,
    clean_presets,
    expand_preset,
    load_presets,
    save_presets,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME)"
    )
    return conn


def test_defaults_when_nothing_saved() -> None:
    assert load_presets(_conn()) == list(DEFAULT_PRESETS)


def test_saved_list_replaces_defaults_and_can_be_empty() -> None:
    conn = _conn()
    save_presets(conn, ["599 JCC1026", "RR73"])
    assert load_presets(conn) == ["599 JCC1026", "RR73"]
    save_presets(conn, [])
    assert load_presets(conn) == []


def test_broken_row_falls_back_to_defaults() -> None:
    conn = _conn()
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('aprs_message_presets', 'x{')")
    assert load_presets(conn) == list(DEFAULT_PRESETS)


def test_clean_drops_blanks_and_duplicates() -> None:
    assert clean_presets([" 73 ", "", "73", "RR73"]) == ["73", "RR73"]


def test_expand_fills_the_grid_and_trims() -> None:
    assert expand_preset("599 {grid}", "PM95") == "599 PM95"
    assert expand_preset("{grid}", "") == ""
    assert len(expand_preset("x" * 100, "PM95")) == MAX_MESSAGE_LEN
