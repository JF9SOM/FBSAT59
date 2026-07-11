"""Unit tests for ui/telemetry_tab.py — _populate_afsk_combo() merge logic.

Verifies the combo lists both the hand-written telemetry_formats/*.json
satellites (filtered by satellites.is_hidden) and any other AX.25-capable
satellite from the DB (mode_detection.is_ax25_telemetry_transmitter()),
without needing a real Qt display (conftest.py forces QT_QPA_PLATFORM
offscreen) or a direwolf/gr-satellites installation.
"""

from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

from ui.telemetry_tab import TelemetryTab


class _FakeRadioControl(QWidget):
    """A QWidget (not just QObject) so it satisfies TelemetryTab's
    radio_control: QWidget parameter type."""

    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        """CREATE TABLE transmitters (
            uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, description TEXT,
            mode TEXT, baud INTEGER, alive INTEGER
        )"""
    )
    return c


@pytest.fixture
def app() -> QApplication:
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])


def _combo_items(tab: TelemetryTab) -> list[tuple[int, str]]:
    combo = tab._combo_afsk_sat
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def test_combo_includes_all_json_satellites_by_default(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """With no satellites/transmitters rows at all, every hand-written
    format definition still shows — fail-open, not fail-closed, so a
    fresh app (before any SATNOGS sync) doesn't show an empty dropdown."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        norads = {norad for norad, _name in _combo_items(tab)}
        assert 25544 in norads  # ISS
        assert 47311 in norads  # Maya-2
    finally:
        tab.close()


def test_combo_excludes_hidden_json_satellite(app: QApplication, conn: sqlite3.Connection) -> None:
    """Maya-2 (BIRDS-2 CubeSat, has a format definition) has since decayed
    and been auto-hidden — must drop out of the combo instead of lingering
    as an unreachable entry forever."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (47311, 'Maya-2', 2)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        norads = {norad for norad, _name in _combo_items(tab)}
        assert 47311 not in norads
        assert 25544 in norads  # unrelated JSON satellite still present
    finally:
        tab.close()


def test_combo_includes_extra_ax25_satellite_with_raw_marker(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """ARICA-2 (68796) has no telemetry_formats/*.json definition, but is a
    4800 baud GMSK satellite explicitly marked AX.25 in its SATNOGS
    description — should still surface in the combo, flagged as raw-hex
    only (no field-level decode available)."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (68796, 'ARICA-2', 0)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 68796, 'Mode U - GMSK4k8 - AX.25', 'GMSK', 4800, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        items = dict(_combo_items(tab))
        assert 68796 in items
        assert "[raw]" in items[68796]
    finally:
        tab.close()


def test_combo_excludes_non_ax25_transmitter(app: QApplication, conn: sqlite3.Connection) -> None:
    """A satellite with a transmitter that doesn't match
    is_ax25_telemetry_transmitter() (e.g. a linear SSB transponder) must
    not be pulled into the combo just because it exists in the DB."""
    conn.execute("INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (99999, 'X', 0)")
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 99999, 'Linear transponder', 'USB', NULL, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        norads = {norad for norad, _name in _combo_items(tab)}
        assert 99999 not in norads
    finally:
        tab.close()
