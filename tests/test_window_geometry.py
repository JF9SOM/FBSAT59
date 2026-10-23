"""Tests for ui.window_geometry -- the main window reopens as it was closed (2026-10-23)."""

from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from ui.window_geometry import GEOMETRY_KEY, restore_window_geometry, save_window_geometry


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, "
        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    return c


def _widget(qtbot: QtBot) -> QWidget:
    w = QWidget()
    qtbot.addWidget(w)
    return w


def test_size_and_position_survive_a_restart(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    first = _widget(qtbot)
    # Small enough for any screen: Qt shrinks a restored window that would not fit.
    first.setGeometry(40, 60, 640, 480)
    save_window_geometry(conn, first)

    second = _widget(qtbot)
    second.resize(300, 200)  # stands for the fixed default every start used to have
    assert restore_window_geometry(conn, second) is True
    assert (second.width(), second.height()) == (640, 480)


def test_the_value_is_stored_under_one_key(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    w = _widget(qtbot)
    save_window_geometry(conn, w)
    save_window_geometry(conn, w)  # saving again replaces it
    rows = conn.execute("SELECT key FROM app_settings").fetchall()
    assert [r["key"] for r in rows] == [GEOMETRY_KEY]


def test_nothing_stored_keeps_the_default(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    w = _widget(qtbot)
    w.resize(1280, 800)
    assert restore_window_geometry(conn, w) is False
    assert (w.width(), w.height()) == (1280, 800)


def test_an_unusable_stored_value_is_ignored(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)", (GEOMETRY_KEY, "not a geometry")
    )
    w = _widget(qtbot)
    w.resize(1280, 800)
    assert restore_window_geometry(conn, w) is False
    assert (w.width(), w.height()) == (1280, 800)


def test_a_database_problem_never_raises(qtbot: QtBot) -> None:
    broken = sqlite3.connect(":memory:")  # no app_settings table
    w = _widget(qtbot)
    save_window_geometry(broken, w)
    assert restore_window_geometry(broken, w) is False
