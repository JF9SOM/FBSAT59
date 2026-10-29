"""FT4 tab: decoded rows are kept in time order next to our own TX rows."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QTableWidgetItem
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from data.database import SCHEMA_SQL  # noqa: E402 -- must follow importorskip above
from ui.ft4_tab import _COL_COUNT, _COL_UTC, Ft4Tab  # noqa: E402


def _make_tab(qtbot: QtBot, stamps: list[str]) -> Ft4Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    tab = Ft4Tab(conn, MagicMock())
    qtbot.addWidget(tab)
    for stamp in stamps:
        row = tab._table.rowCount()
        tab._table.insertRow(row)
        for c in range(_COL_COUNT):
            tab._table.setItem(row, c, QTableWidgetItem(stamp if c == _COL_UTC else ""))
    return tab


def test_rx_row_goes_above_the_tx_row_of_the_next_period(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, ["035615", "035630", "035645"])
    assert tab._time_ordered_row("035637") == 2
    assert tab._time_ordered_row("035645") == 3  # equal stamps keep arrival order
    assert tab._time_ordered_row("035700") == 3


def test_midnight_wrap_is_not_reordered(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, ["235952", "235959"])
    assert tab._time_ordered_row("000007") == 2
