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


def test_tx_turn_waits_for_the_running_decode(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, [])
    tab._transmit_now = MagicMock()  # type: ignore[method-assign]
    tab._tx_enabled = True
    tab._decode_busy = True

    tab._on_period_changed(True)
    tab._transmit_now.assert_not_called()

    tab._on_decode_done([], MagicMock())
    tab._transmit_now.assert_called_once()

    # The timer that was armed for the same turn must not transmit again.
    tab._flush_deferred_tx()
    tab._transmit_now.assert_called_once()


def test_deferred_tx_starts_on_timeout_when_the_decode_is_slow(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, [])
    tab._transmit_now = MagicMock()  # type: ignore[method-assign]
    tab._tx_enabled = True
    tab._decode_busy = True

    tab._on_period_changed(True)
    qtbot.waitUntil(lambda: tab._transmit_now.call_count == 1, timeout=2000)


def test_tx_turn_starts_at_once_when_no_decode_is_running(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, [])
    tab._transmit_now = MagicMock()  # type: ignore[method-assign]
    tab._tx_enabled = True

    tab._on_period_changed(True)
    tab._transmit_now.assert_called_once()
