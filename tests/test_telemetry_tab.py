"""Unit tests for ui/telemetry_tab.py — _populate_afsk_combo() merge logic.

Verifies the combo lists both the hand-written telemetry_formats/*.json
satellites (filtered by satellites.is_hidden) and any other AX.25-capable
satellite from the DB (mode_detection.is_ax25_telemetry_transmitter()),
without needing a real Qt display (conftest.py forces QT_QPA_PLATFORM
offscreen) or a direwolf/gr-satellites installation.
"""

from __future__ import annotations

import sqlite3
import types

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

import ui.telemetry_tab as telemetry_tab_mod
from comms.telemetry.satnogs_uploader import (
    load_satnogs_upload_settings,
    save_satnogs_upload_settings,
)
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


def _gr_combo_items(tab: TelemetryTab) -> list[tuple[int, str]]:
    combo = tab._combo_gr_sat
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def test_gr_combo_excludes_hidden_satellite(app: QApplication, conn: sqlite3.Connection) -> None:
    """gr-satellites' own YAML catalog is independent of our satellites
    table, so a NORAD id it lists (e.g. a decayed CubeSat we've since
    auto-hidden) must still be dropped from the combo — the app's own
    tracking is the authority on whether a satellite is still up there,
    not gr-satellites' static list."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (47311, 'Maya-2', 2)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._gr_sat_list = [(47311, "Maya-2"), (25544, "ISS")]
        tab._populate_gr_combo()
        norads = {norad for norad, _name in _gr_combo_items(tab)}
        assert 47311 not in norads
        assert 25544 in norads
    finally:
        tab.close()


def test_gr_combo_includes_satellite_absent_from_db(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """A satellite gr-satellites knows about but our own satellites table
    has never seen (no row at all — never synced from SATNOGS) must not be
    excluded: absence isn't evidence it decayed, only an explicit
    is_hidden != 0 row is."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._gr_sat_list = [(99000, "Some Unsynced Sat")]
        tab._populate_gr_combo()
        norads = {norad for norad, _name in _gr_combo_items(tab)}
        assert 99000 in norads
    finally:
        tab.close()


# ---------------------------------------------------------------------------
# SatNOGS DB upload footer controls
# ---------------------------------------------------------------------------


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, conn, raw, norad, received_at) -> bool:  # noqa: ANN001
        self.calls.append((conn, raw, norad, received_at))
        return True


def test_satnogs_toggle_persists_to_app_settings(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        assert tab._btn_satnogs_toggle.text() == "SatNOGS Upload: OFF"
        tab._btn_satnogs_toggle.setChecked(True)
        assert load_satnogs_upload_settings(conn)["enabled"] is True
        assert tab._btn_satnogs_toggle.text() == "SatNOGS Upload: ON"
        tab._btn_satnogs_toggle.setChecked(False)
        assert load_satnogs_upload_settings(conn)["enabled"] is False
    finally:
        tab.close()


def test_satnogs_toggle_reflects_saved_state_on_open(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    save_satnogs_upload_settings(conn, {"enabled": True, "api_key": "k"})
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        assert tab._btn_satnogs_toggle.isChecked() is True
        assert tab._btn_satnogs_toggle.text() == "SatNOGS Upload: ON"
    finally:
        tab.close()


def test_satnogs_controls_hidden_in_gr_satellites_mode(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """The SatNOGS-upload cluster only covers the AFSK / Direwolf path, so
    it disappears entirely when Mode is switched to gr-satellites."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        widgets = (
            tab._btn_satnogs_toggle,
            tab._btn_satnogs_api,
            tab._btn_satnogs_link,
        )
        assert all(not w.isHidden() for w in widgets)  # AFSK mode by default

        tab._combo_mode.setCurrentText("gr-satellites")
        assert all(w.isHidden() for w in widgets)

        tab._combo_mode.setCurrentText("Direwolf (AX.25)")
        assert all(not w.isHidden() for w in widgets)
    finally:
        tab.close()


def test_satnogs_link_disabled_without_any_target(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """With both mode combos empty and nothing selected in the main list,
    the link has nothing to point at and is disabled."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._combo_afsk_sat.clear()
        tab._combo_gr_sat.clear()
        tab._selected_norad = None
        tab._update_satnogs_link_enabled()
        assert tab._btn_satnogs_link.isEnabled() is False
        tab.set_satellite(25544, "ISS")
        assert tab._btn_satnogs_link.isEnabled() is True
    finally:
        tab.close()


def test_satnogs_link_emits_open_request_for_active_satellite(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        got: list[tuple[int, str]] = []
        tab.open_satnogs_requested.connect(lambda n, name: got.append((n, name)))
        tab.set_satellite(43803, "JO-97")  # 43803 has a format definition
        assert tab._btn_satnogs_link.isEnabled() is True
        tab._btn_satnogs_link.click()
        assert got == [(43803, "JO-97")]
    finally:
        tab.close()


def test_ax25_frame_forwards_raw_to_satnogs_uploader(
    app: QApplication, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    monkeypatch.setattr(
        telemetry_tab_mod,
        "decode_ax25",
        lambda raw: types.SimpleNamespace(src="JY1SAT", payload=b"\x00\x11\x22"),
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        monkeypatch.setattr(tab, "_callsign_to_norad", lambda src: 43803)
        raw = bytes.fromhex("9c86aa8ea662e0a08a82a49886e103f000112233")
        tab._on_ax25_frame(raw)
        assert len(rec.calls) == 1
        c_conn, c_raw, c_norad, _c_ts = rec.calls[0]
        assert c_conn is conn
        assert c_raw == raw  # full AX.25 frame, not just the payload
        assert c_norad == 43803
    finally:
        tab.close()
