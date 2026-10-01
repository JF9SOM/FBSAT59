"""CW Decoder tab: choosing between the AI model and the classical decoder."""

from __future__ import annotations

import sqlite3

import pytest
from pytestqt.qtbot import QtBot

from comms.cw.classic import decode_classic
from ui.cw_tab import _METHOD_AI, _METHOD_CLASSIC, CwTab


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return c


def test_defaults_to_the_ai_model(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = CwTab(conn)
    qtbot.addWidget(tab)
    assert tab._method() == _METHOD_AI


def test_method_is_saved_and_restored(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = CwTab(conn)
    qtbot.addWidget(tab)
    tab._method_combo.setCurrentIndex(tab._method_combo.findData(_METHOD_CLASSIC))
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'cw_decoder_method'").fetchone()
    assert row[0] == _METHOD_CLASSIC
    again = CwTab(conn)
    qtbot.addWidget(again)
    assert again._method() == _METHOD_CLASSIC


def test_classical_method_needs_no_model(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = CwTab(conn)
    qtbot.addWidget(tab)
    tab._method_combo.setCurrentIndex(tab._method_combo.findData(_METHOD_CLASSIC))
    assert tab._decode_function() is decode_classic
    assert tab._start_btn.isEnabled()
    assert not tab._banner.isVisibleTo(tab)
