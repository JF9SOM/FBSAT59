"""FT4 tab: double-clicking a decoded row calls that station and arms TX."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from data.database import SCHEMA_SQL  # noqa: E402 -- must follow importorskip above
from ui.ft4_tab import _COL_COUNT, _COL_MSG, _COL_UTC, Ft4Tab  # noqa: E402


def _make_tab(qtbot: QtBot) -> Ft4Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    tab = Ft4Tab(conn, MagicMock())
    qtbot.addWidget(tab)
    tab._codec = MagicMock(is_available=True)  # independent of an installed ft8lib
    tab._start_scheduler = MagicMock()  # type: ignore[method-assign]
    tab._my_call = "JF9SOM"
    tab._my_grid = "PM86"
    return tab


def _add_row(tab: Ft4Tab, text: str, even: bool = True) -> int:
    row = tab._table.rowCount()
    tab._table.insertRow(row)
    for c in range(_COL_COUNT):
        tab._table.setItem(row, c, QTableWidgetItem(""))
    tab._table.item(row, _COL_MSG).setText(text)  # type: ignore[union-attr]
    tab._table.item(row, _COL_UTC).setData(Qt.ItemDataRole.UserRole, even)  # type: ignore[union-attr]
    return row


@pytest.mark.parametrize(
    ("text", "call"),
    [
        ("CQ JA9DRP PM86", "JA9DRP"),
        ("JH5EWP JA1VDJ 73", "JA1VDJ"),  # a station signing off is who to call next
        ("JA1NWR JE6TSP PM51", "JE6TSP"),
    ],
)
def test_double_click_fills_the_reply_and_enables_tx(qtbot: QtBot, text: str, call: str) -> None:
    tab = _make_tab(qtbot)
    row = _add_row(tab, text)
    assert not tab._tx_enabled

    tab._on_message_double_clicked(row, _COL_MSG)

    assert tab._tx_edit.text().startswith(f"{call} JF9SOM")
    assert tab._tx_enabled
    assert tab._tx_enable_btn.isChecked()


def test_double_click_uses_the_slot_the_message_was_heard_in(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    row = _add_row(tab, "CQ JA9DRP PM86", even=False)
    tab._on_message_double_clicked(row, _COL_MSG)
    tab._start_scheduler.assert_called_once_with(tx_even=False)


def test_double_click_on_our_own_row_does_not_enable_tx(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    row = _add_row(tab, "JF9SOM JF9SOM PM86")
    tab._on_message_double_clicked(row, _COL_MSG)
    assert not tab._tx_enabled


def test_double_click_without_my_call_does_not_enable_tx(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    tab._my_call = ""
    row = _add_row(tab, "CQ JA9DRP PM86")
    tab._on_message_double_clicked(row, _COL_MSG)
    assert not tab._tx_enabled
