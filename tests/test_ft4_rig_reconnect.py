"""FT4 tab: reconnect the TX rig if its connection drops while TX Enable is on.

RS-44 pass testing (2026-09-29) found the rig connection dropping repeatedly
mid-pass with nothing reconnecting it -- Doppler tracking and TX silently
stopped until the operator noticed and clicked Connect by hand.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from data.database import SCHEMA_SQL  # noqa: E402 -- must follow importorskip above
from ui.ft4_tab import Ft4Tab  # noqa: E402


def _make_tab(qtbot: QtBot) -> Ft4Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    tab = Ft4Tab(conn, MagicMock())
    qtbot.addWidget(tab)
    return tab


def _rig(connected: bool) -> MagicMock:
    return MagicMock(is_connected=connected)


def test_watch_timer_starts_and_stops_with_tx_enable(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    assert not tab._rig_watch_timer.isActive()
    tab._codec = MagicMock(is_available=True)
    tab._my_call = "JF9SOM"

    tab._on_tx_enable_toggled(True)
    assert tab._rig_watch_timer.isActive()

    tab._on_tx_enable_toggled(False)
    assert not tab._rig_watch_timer.isActive()


def test_reconnects_rig1_on_connected_to_disconnected_transition(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    rig1 = _rig(True)
    tab._radio_control = MagicMock(
        _rig1=rig1,
        _rig2=None,
        _connect_rig1_btn=MagicMock(**{"isEnabled.return_value": True}),
    )
    tab._tx_rig = lambda: rig1  # type: ignore[method-assign]

    tab._check_tx_rig_connection()  # baseline: connected
    tab._radio_control._on_connect_rig1.assert_not_called()

    rig1.is_connected = False
    tab._check_tx_rig_connection()  # transition: connected -> disconnected

    tab._radio_control._on_connect_rig1.assert_called_once()


def test_reconnects_rig2_when_it_is_the_tx_rig(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    rig2 = _rig(True)
    tab._radio_control = MagicMock(
        _rig1=MagicMock(is_connected=True),
        _rig2=rig2,
        _connect_rig2_btn=MagicMock(**{"isEnabled.return_value": True}),
    )
    tab._tx_rig = lambda: rig2  # type: ignore[method-assign]

    tab._check_tx_rig_connection()
    rig2.is_connected = False
    tab._check_tx_rig_connection()

    tab._radio_control._on_connect_rig2.assert_called_once()
    tab._radio_control._on_connect_rig1.assert_not_called()


def test_does_not_reconnect_while_a_burst_is_in_flight(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    rig1 = _rig(True)
    tab._radio_control = MagicMock(_rig1=rig1, _rig2=None)
    tab._tx_rig = lambda: rig1  # type: ignore[method-assign]
    tab._check_tx_rig_connection()

    tab._tx_in_progress = True
    rig1.is_connected = False
    tab._check_tx_rig_connection()

    tab._radio_control._on_connect_rig1.assert_not_called()


def test_does_not_reconnect_when_never_seen_connected(qtbot: QtBot) -> None:
    """Started disconnected (e.g. operator hasn't pressed Connect yet) -- not our job."""
    tab = _make_tab(qtbot)
    rig1 = _rig(False)
    tab._radio_control = MagicMock(_rig1=rig1, _rig2=None)
    tab._tx_rig = lambda: rig1  # type: ignore[method-assign]

    tab._check_tx_rig_connection()
    tab._check_tx_rig_connection()

    tab._radio_control._on_connect_rig1.assert_not_called()


def test_does_not_double_reconnect_while_a_connect_is_already_in_flight(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    rig1 = _rig(True)
    tab._radio_control = MagicMock(
        _rig1=rig1,
        _rig2=None,
        _connect_rig1_btn=MagicMock(**{"isEnabled.return_value": False}),
    )
    tab._tx_rig = lambda: rig1  # type: ignore[method-assign]
    tab._check_tx_rig_connection()

    rig1.is_connected = False
    tab._check_tx_rig_connection()

    tab._radio_control._on_connect_rig1.assert_not_called()


def test_reconnect_on_close_event_stops_the_timer(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    tab._codec = MagicMock(is_available=True)
    tab._my_call = "JF9SOM"
    tab._on_tx_enable_toggled(True)
    assert tab._rig_watch_timer.isActive()

    tab.close()

    assert not tab._rig_watch_timer.isActive()
