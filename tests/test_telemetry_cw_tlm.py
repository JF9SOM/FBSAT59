"""Telemetry tab, "CW TLM" mode: Morse-coded hexadecimal housekeeping frames.

The CW Decoder tab decodes the Morse and announces finished text blocks; the
Telemetry tab turns the blocks that are valid frames (ARICA-2's HK1/HK2/HK3)
into received frames and Decoded Fields. These tests drive the tab with those
blocks directly, so they need no CW model, SDR or scipy.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from ui.telemetry_tab import _MODE_CW, TelemetryTab

HK1 = "2FFE8594EB880124"
HK3 = "00D7C2A8D6B8EA"
END = datetime(2026, 9, 20, 7, 1, 48, tzinfo=UTC)
START = datetime(2026, 9, 20, 7, 1, 31, tzinfo=UTC)


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


class _FakeCwTab(QObject):
    frame_block_ready = Signal(str, object, object)


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


def _add(
    conn: sqlite3.Connection, norad: int, name: str, description: str, mode: str = "CW"
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO satellites (norad_cat_id, name, is_hidden) VALUES (?, ?, 0)",
        (norad, name),
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES (?, ?, ?, ?, NULL, 1)",
        (f"{norad}-{description}", norad, description, mode),
    )


@pytest.fixture
def cw_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    _add(conn, 41847, "CAS-2T", "CW Telemetry")
    _add(conn, 68796, "ARICA-2", "Mode U - CW")
    _add(conn, 68796, "ARICA-2", "Mode U - GMSK4k8 - AX.25", mode="GMSK")
    _add(conn, 99999, "LINEAR", "Linear transponder", mode="USB")
    return conn


def _make_tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def _combo_items(tab: TelemetryTab) -> list[tuple[int, str]]:
    combo = tab._combo_cw_sat
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def _select_cw_mode(tab: TelemetryTab) -> None:
    tab._combo_mode.setCurrentIndex(tab._combo_mode.findText(_MODE_CW))


def _running_tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    """A tab in CW TLM mode with ARICA-2 selected and started."""
    tab = _make_tab(qtbot, conn)
    _select_cw_mode(tab)
    tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(68796))
    tab._on_start()
    return tab


def _decode_value(tab: TelemetryTab, frame: str, field: str) -> str:
    table = tab._decode_tables[frame]
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and item.text() == field:
            value = table.item(row, 1)
            return value.text() if value is not None else ""
    raise AssertionError(f"no row {field!r} in {frame}")


class TestSatelliteCombo:
    def test_lists_the_satellites_the_db_search_finds(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        norads = [n for n, _ in _combo_items(tab)]
        # ARICA-2 via its CW frame definition ("Mode U - CW" has no TLM in it),
        # CAS-2T via "CW Telemetry"; the linear transponder and the GMSK entry do not count.
        assert set(norads) == {41847, 68796}

    def test_satellites_that_can_be_decoded_come_first(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        assert [n for n, _ in _combo_items(tab)] == [68796, 41847]

    def test_hidden_and_dead_transmitters_are_left_out(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        cw_conn.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = 41847")
        tab = _make_tab(qtbot, cw_conn)
        assert [n for n, _ in _combo_items(tab)] == [68796]


class TestModeSwitch:
    def test_cw_mode_shows_its_own_combo_and_hides_the_direwolf_controls(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        tab.show()
        _select_cw_mode(tab)
        assert tab._combo_cw_sat.isVisible()
        assert not tab._combo_afsk_sat.isVisible()
        assert not tab._combo_gr_sat.isVisible()
        assert not tab._baud_combo.isVisible()
        assert not tab._btn_backend_log.isVisible()

    def test_switching_to_cw_mode_selects_the_satellite(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        with qtbot.waitSignal(tab.satellite_selected) as blocker:
            _select_cw_mode(tab)
        assert blocker.args == [68796, "cw_tlm"]

    def test_changing_the_cw_satellite_reports_it(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        _select_cw_mode(tab)
        with qtbot.waitSignal(tab.satellite_selected) as blocker:
            tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(41847))
        assert blocker.args == [41847, "cw_tlm"]


class TestStartStop:
    def test_start_asks_for_the_cw_decoder_and_stop_releases_it(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        _select_cw_mode(tab)
        tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(68796))

        with qtbot.waitSignal(tab.cw_tlm_start_requested):
            tab._on_start()
        assert tab._cw_tlm_norad == 68796
        assert not tab._btn_start.isEnabled()
        assert tab._btn_stop.isEnabled()
        assert tab._decode_tabs_norad == 68796  # HK1..HK3 tabs are ready

        with qtbot.waitSignal(tab.cw_tlm_stop_requested):
            tab._on_stop()
        assert tab._cw_tlm_norad is None
        assert tab._btn_start.isEnabled()

    def test_a_satellite_without_a_frame_format_does_not_start(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        _select_cw_mode(tab)
        tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(41847))
        requested: list[bool] = []
        tab.cw_tlm_start_requested.connect(lambda: requested.append(True))

        tab._on_start()

        assert requested == []
        assert tab._cw_tlm_norad is None
        assert tab._btn_start.isEnabled()
        assert "ARICA-2" in tab._lbl_status.text()

    def test_stopping_when_not_running_sends_nothing(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        stops: list[bool] = []
        tab.cw_tlm_stop_requested.connect(lambda: stops.append(True))
        tab._on_stop()
        assert stops == []


class TestReceivedBlocks:
    def test_a_valid_frame_becomes_a_row_decoded_fields_and_a_log_entry(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)

        tab._on_cw_block(HK1, START, END)

        assert tab._table.rowCount() == 1
        item = lambda col: tab._table.item(0, col).text()  # noqa: E731
        assert item(0) == "07:01:48"  # the block's end time, UTC
        assert item(1) == "JS1YSD"
        assert item(2) == "ARICA-2"
        assert item(3) == f"[HK1] {HK1}"
        assert tab._frame_count == 1
        assert _decode_value(tab, "HK1", "SBD error") == "Abnormal"
        assert _decode_value(tab, "HK1", "Angular Velocity X") == "-2.591 ≦ Gx ＜ -2.061"
        row = cw_conn.execute(
            "SELECT received_at, norad_cat_id, raw_hex FROM telemetry_log"
        ).fetchone()
        assert row["received_at"].startswith("2026-09-20T07:01:48")
        assert (row["norad_cat_id"], row["raw_hex"]) == (68796, HK1)

    def test_hk3_updates_its_own_tab(self, qtbot: QtBot, cw_conn: sqlite3.Connection) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK3, START, END)
        assert _decode_value(tab, "HK3", "Battery Voltage").startswith("8.2009")
        assert _decode_value(tab, "HK3", "Power Flow Status") == "charge"

    def test_a_frame_that_fails_the_plausibility_checks_is_greyed_out_and_not_used(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        bits = list(f"{int(HK1, 16):064b}")
        bits[54] = "1"  # inside the always-zero not_used field
        bad = f"{int(''.join(bits), 2):016X}"

        tab._on_cw_block(bad, START, END)

        assert tab._table.rowCount() == 1
        assert tab._table.item(0, 3).text().startswith(f"[?] HK1 {bad}")
        assert tab._frame_count == 0  # not counted
        assert _decode_value(tab, "HK1", "SBD error") == "—"  # Decoded Fields untouched
        assert cw_conn.execute("SELECT COUNT(*) FROM telemetry_log").fetchone()[0] == 0

    def test_a_misread_frame_length_is_listed_as_a_candidate_only(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1[:-1], START, END)  # a dropped digit
        assert tab._table.item(0, 3).text().startswith(f"[?] {HK1[:-1]}")
        assert tab._frame_count == 0
        assert cw_conn.execute("SELECT COUNT(*) FROM telemetry_log").fetchone()[0] == 0

    def test_the_id_text_and_noise_are_ignored(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block("DE JS1YSD ARICA2", START, END)
        tab._on_cw_block("TTNNEE", START, END)
        assert tab._table.rowCount() == 0

    def test_blocks_are_ignored_when_not_running(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        assert tab._table.rowCount() == 0


class TestCwTabConnection:
    def test_blocks_from_the_attached_cw_tab_arrive(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        cw = _FakeCwTab()
        tab.attach_cw_tab(cw)

        cw.frame_block_ready.emit(HK1, START, END)

        assert tab._table.rowCount() == 1

    def test_attaching_another_cw_tab_replaces_the_first(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        first, second = _FakeCwTab(), _FakeCwTab()
        tab.attach_cw_tab(first)
        tab.attach_cw_tab(first)  # same tab again: still one connection
        tab.attach_cw_tab(second)

        first.frame_block_ready.emit(HK1, START, END)
        assert tab._table.rowCount() == 0
        second.frame_block_ready.emit(HK1, START, END)
        assert tab._table.rowCount() == 1


class TestMainWindowWiring:
    """MainWindow's handlers, called unbound with a stand-in for self."""

    def test_start_opens_the_cw_tab_attaches_and_starts_it(self) -> None:
        from ui.main_window import MainWindow

        cw_tab, telemetry_tab = MagicMock(), MagicMock()
        fake: Any = MagicMock()
        fake._comms_tab_keys = {telemetry_tab: "telemetry", cw_tab: "cw"}
        fake._find_comms_tab = lambda key: MainWindow._find_comms_tab(fake, key)

        MainWindow._on_telemetry_cw_tlm_start(fake, telemetry_tab)

        fake._on_open_cw.assert_called_once_with()
        telemetry_tab.attach_cw_tab.assert_called_once_with(cw_tab)
        cw_tab.start_decoding.assert_called_once_with()
        # ... and the Telemetry tab, not the CW tab, is left in front.
        fake._tab_widget.setCurrentWidget.assert_called_with(telemetry_tab)

    def test_stop_stops_the_cw_tab(self) -> None:
        from ui.main_window import MainWindow

        cw_tab = MagicMock()
        fake: Any = MagicMock()
        fake._comms_tab_keys = {cw_tab: "cw"}
        fake._find_comms_tab = lambda key: MainWindow._find_comms_tab(fake, key)

        MainWindow._on_telemetry_cw_tlm_stop(fake)

        cw_tab.stop_decoding.assert_called_once_with()

    def test_start_without_an_openable_cw_tab_does_not_crash(self) -> None:
        from ui.main_window import MainWindow

        telemetry_tab = MagicMock()
        fake: Any = MagicMock()
        fake._comms_tab_keys = {telemetry_tab: "telemetry"}
        fake._find_comms_tab = lambda key: MainWindow._find_comms_tab(fake, key)

        MainWindow._on_telemetry_cw_tlm_start(fake, telemetry_tab)

        telemetry_tab.attach_cw_tab.assert_not_called()

    def test_choosing_a_cw_tlm_satellite_keeps_the_telemetry_tab_in_front(self) -> None:
        from ui.main_window import MainWindow

        fake: Any = MagicMock()
        current = object()
        fake._tab_widget.currentWidget.return_value = current

        MainWindow._on_telemetry_satellite_requested(fake, 68796, "cw_tlm")

        fake._select_telemetry_satellite.assert_called_once_with(68796, "cw_tlm")
        fake._tab_widget.setCurrentWidget.assert_called_once_with(current)

    def test_other_modes_do_not_touch_the_current_tab(self) -> None:
        from ui.main_window import MainWindow

        fake: Any = MagicMock()
        MainWindow._on_telemetry_satellite_requested(fake, 25544, "afsk")
        fake._select_telemetry_satellite.assert_called_once_with(25544, "afsk")
        fake._tab_widget.setCurrentWidget.assert_not_called()
