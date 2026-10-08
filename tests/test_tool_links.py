"""Tests for the Tools menu web site list and its Settings tab."""

from __future__ import annotations

import sqlite3

from pytestqt.qtbot import QtBot

from data import tool_links


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return c


def test_defaults_when_unsaved() -> None:
    assert tool_links.load_tool_links(_conn()) == tool_links.DEFAULT_TOOL_LINKS


def test_save_roundtrip_normalizes_and_drops_blanks() -> None:
    c = _conn()
    tool_links.save_tool_links(c, [("A", "example.com"), ("", "x"), ("B", " ")])
    assert tool_links.load_tool_links(c) == [("A", "https://example.com")]


def test_empty_list_stays_empty() -> None:
    c = _conn()
    tool_links.save_tool_links(c, [])
    assert tool_links.load_tool_links(c) == []


def test_corrupt_value_falls_back_to_defaults() -> None:
    c = _conn()
    c.execute("INSERT INTO app_settings (key, value) VALUES ('tool_links', 'nope')")
    assert tool_links.load_tool_links(c) == tool_links.DEFAULT_TOOL_LINKS


def test_settings_tab_shows_defaults_and_saves(qtbot: QtBot) -> None:
    from ui.settings_dialog import SettingsDialog

    c = _conn()
    c.execute("CREATE TABLE custom_groups (id INTEGER, name TEXT, sort_order INTEGER)")
    dlg = SettingsDialog(c)
    qtbot.addWidget(dlg)
    assert dlg._tools_table.rowCount() == 2
    dlg._on_tool_add()
    dlg._tools_table.item(2, 0).setText("Foo")
    dlg._tools_table.item(2, 1).setText("foo.org")
    dlg._save_tools_settings()
    assert tool_links.load_tool_links(c)[-1] == ("Foo", "https://foo.org")
