"""Unit tests for ui/aprs_tab.py.

Covers the "Open in Google Maps" right-click affordance on the received-packet
log: a packet that carries a position stashes its (lat, lon) on the list item
so the context menu can build a maps URL; a packet without a position stashes
nothing. Also checks the URL builder itself.

Uses pytest-qt's qtbot (per CLAUDE.md — new QWidget tests must register the
widget with qtbot.addWidget to avoid the Qt object-lifetime segfault the manual
QApplication + close() pattern has caused).
"""

from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import comms.aprs.engine as engine_mod
from ui.aprs_tab import AprsTab, _google_maps_url


class _FakeRadioControl(QWidget):
    """QWidget stand-in matching AprsTab's radio_control parameter type."""

    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)
    transmitter_changed_ = Signal(object)

    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return c


@pytest.fixture(autouse=True)
def _reset_engine_singleton() -> None:
    """AprsEngine is a process-wide singleton; drop it between tests so each
    AprsTab gets a fresh one wired to its own in-memory DB."""
    engine_mod.AprsEngine._instance = None
    yield
    engine_mod.AprsEngine._instance = None


def _make_tab(qtbot: QtBot, conn: sqlite3.Connection) -> AprsTab:
    tab = AprsTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def test_google_maps_url_has_pin_and_zoom() -> None:
    url = _google_maps_url(35.4817, 139.7210)
    assert url == "https://www.google.com/maps?q=35.481700,139.721000&z=15"


def test_google_maps_url_handles_southern_western_hemisphere() -> None:
    url = _google_maps_url(-33.8688, -151.2093)
    assert "q=-33.868800,-151.209300" in url


def test_positioned_packet_stashes_coords_on_item(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JA4GWS-9",
        via="TCPIP",
        comment="Pos 35.4817,139.7210",
        raw_frame="=3528.90N/13943.26E>",
        lat=35.4817,
        lon=139.7210,
    )
    item = tab._log_list.item(tab._log_list.count() - 1)
    coords = item.data(Qt.ItemDataRole.UserRole)
    assert coords == (35.4817, 139.7210)


def test_positionless_packet_stashes_nothing(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JG1RBF-7",
        via="TCPIP",
        comment="MSG->JF1BQZ-9: Good afternoon",
        raw_frame=":JF1BQZ-9 :Good afternoon{10",
    )
    item = tab._log_list.item(tab._log_list.count() - 1)
    assert item.data(Qt.ItemDataRole.UserRole) is None


def test_open_item_on_map_emits_url_for_positioned_item(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JA4GWS-9",
        via="TCPIP",
        comment="Pos 35.4817,139.7210",
        raw_frame="",
        lat=35.4817,
        lon=139.7210,
    )
    item = tab._log_list.item(tab._log_list.count() - 1)

    with qtbot.waitSignal(tab.open_map_url, timeout=1000) as blocker:
        tab._open_item_on_map(item)

    assert blocker.args == ["https://www.google.com/maps?q=35.481700,139.721000&z=15"]


def test_open_item_on_map_noop_for_positionless_item(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(callsign="JG1RBF-7", via="", comment="hi", raw_frame="")
    item = tab._log_list.item(tab._log_list.count() - 1)

    triggered: list[str] = []
    tab.open_map_url.connect(triggered.append)
    tab._open_item_on_map(item)
    assert triggered == []


# --------------------------------------------------------------------------- #
# Show: Plain / Raw toggle
# --------------------------------------------------------------------------- #


def test_display_toggle_switches_between_plain_and_raw(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    assert tab._display_mode == "plain"  # default

    tab.append_packet(
        callsign="JA4GWS-9",
        via="ARISS",
        comment="Pos 35.48,139.72",
        raw_frame="!3540.00N/13945.00E-shack",
        plain="house · 35.4800°N 139.7200°E",
    )
    item = tab._log_list.item(tab._log_list.count() - 1)
    assert item.text().endswith("house · 35.4800°N 139.7200°E")

    # Switch to Raw — the existing row must re-render to the on-air info field.
    tab._display_combo.setCurrentIndex(tab._display_combo.findData("raw"))
    assert tab._display_mode == "raw"
    assert item.text().endswith("!3540.00N/13945.00E-shack")

    # And back to Plain.
    tab._display_combo.setCurrentIndex(tab._display_combo.findData("plain"))
    assert item.text().endswith("house · 35.4800°N 139.7200°E")


def test_display_mode_persists_to_app_settings(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab._display_combo.setCurrentIndex(tab._display_combo.findData("raw"))

    row = conn.execute("SELECT value FROM app_settings WHERE key = 'aprs_display_mode'").fetchone()
    assert row is not None and row["value"] == "raw"

    # A fresh tab on the same DB restores the saved mode.
    tab2 = AprsTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab2)
    assert tab2._display_mode == "raw"
