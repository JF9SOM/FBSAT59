"""Tests for the Telemetry tab's "HADES FSK" mode (200 baud 2FSK, UNNE-1B).

No display, SDR or network needed: the demodulator thread is real but fed nothing, and
the frames the tab gets are built by hand.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import ui.telemetry_tab as telemetry_tab_mod
from comms import mode_detection
from comms.telemetry.hades_fsk import HadesFrame
from ui.telemetry_tab import TelemetryTab

UNNE1B = 98205
TYPE1_PAYLOAD = bytes.fromhex("1c7afd1300ac25854d5f00cbb86d86007423400ffb01c7011000000000")


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


class _FakeDevice:
    sample_rate = 250_000


class _FakePipeline:
    """Records subscribe()/unsubscribe() like SDRPipeline."""

    def __init__(self) -> None:
        self._device = _FakeDevice()
        self.subscribers: list[Any] = []

    def subscribe(self, fn: Any) -> None:
        self.subscribers.append(fn)

    def unsubscribe(self, fn: Any) -> None:
        self.subscribers.remove(fn)


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
    c.execute("INSERT INTO satellites VALUES (?, 'UNNE-1B', 0)", (UNNE1B,))
    c.execute(
        "INSERT INTO transmitters VALUES ('u1', ?, 'Mode U - 200bps FSK TLM', 'FSK', 200, 1)",
        (UNNE1B,),
    )
    return c


def _tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def _frame(repaired: bool = False) -> HadesFrame:
    return HadesFrame(0, 0.0, 9_300.0, 14.6, 1, 12, TYPE1_PAYLOAD, repaired)


def _select_hades_mode(tab: TelemetryTab) -> None:
    tab._combo_mode.setCurrentIndex(tab._combo_mode.findText(telemetry_tab_mod._MODE_HADES))


def test_transmitter_matcher() -> None:
    good = {"norad_cat_id": UNNE1B, "mode": "FSK", "baud": 200, "description": "x"}
    assert mode_detection.is_hades_fsk_transmitter(good)
    assert not mode_detection.is_hades_fsk_transmitter({**good, "baud": 800})
    assert not mode_detection.is_hades_fsk_transmitter({**good, "mode": "CW"})
    assert not mode_detection.is_hades_fsk_transmitter({**good, "norad_cat_id": 25544})


def test_combo_lists_unne1b_from_db(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    combo = tab._combo_hades_sat
    assert [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())] == [
        (UNNE1B, f"UNNE-1B  ({UNNE1B})")
    ]


def test_combo_empty_when_transmitter_is_not_200_baud(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    conn.execute("UPDATE transmitters SET baud = 9600")
    assert _tab(qtbot, conn)._combo_hades_sat.count() == 0


def test_mode_switch_shows_hades_widgets_and_hides_the_rest(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    tab.show()
    _select_hades_mode(tab)
    assert tab._combo_hades_sat.isVisible()
    assert tab._btn_hades_sat_search.isVisible()
    assert not tab._combo_afsk_sat.isVisible()
    assert not tab._baud_combo.isVisible()
    assert not tab._btn_satnogs_toggle.isVisible()
    assert not tab._btn_satnogs_send.isVisible()
    tab._combo_mode.setCurrentIndex(0)  # back to AX.25
    assert tab._combo_afsk_sat.isVisible()
    assert tab._btn_satnogs_toggle.isVisible()
    assert not tab._combo_hades_sat.isVisible()


def test_selecting_the_satellite_emits_hades_mode(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    seen: list[tuple[int, str]] = []
    tab.satellite_selected.connect(lambda n, m: seen.append((n, m)))
    _select_hades_mode(tab)
    assert (UNNE1B, "hades") in seen


def test_start_without_a_satellite_or_sdr_reports_why(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    conn.execute("DELETE FROM transmitters")
    tab = _tab(qtbot, conn)
    _select_hades_mode(tab)
    tab._on_start()
    assert tab._hades_demod is None
    assert tab._btn_start.isEnabled()
    assert "satellite" in tab._lbl_status.text().lower()


def test_start_subscribes_and_stop_unsubscribes(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    _select_hades_mode(tab)
    pipeline = _FakePipeline()
    tab._sdr_pipeline = pipeline
    tab._on_start()
    try:
        assert tab._hades_demod is not None
        assert len(pipeline.subscribers) == 1
        assert tab._hades_norad == UNNE1B
        assert not tab._btn_start.isEnabled()
        assert tab._btn_stop.isEnabled()
    finally:
        tab._on_stop()
    assert tab._hades_demod is None
    assert pipeline.subscribers == []
    assert tab._btn_start.isEnabled()


def test_frame_adds_a_row_and_logs_it(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    tab._hades_norad = UNNE1B
    tab._on_hades_frame(_frame(), 4.0)
    assert tab._table.rowCount() == 1
    data = tab._table.item(0, 3).text()
    assert data.startswith("[Power]")
    assert "uptime 1310074 s" in data
    assert "+9.3 kHz" in data
    assert "1C 7A FD 13" in data
    assert tab._table.item(0, 2).text() == "UNNE-1B"
    row = conn.execute(
        "SELECT kind, norad_cat_id, frame_hex, parsed_json FROM telemetry_log"
    ).fetchone()
    assert (row["kind"], row["norad_cat_id"], row["frame_hex"]) == (
        "hades",
        UNNE1B,
        TYPE1_PAYLOAD.hex(),
    )
    saved = json.loads(row["parsed_json"])
    assert saved["type"] == 1 and saved["uptime"] == 1310074 and saved["repaired"] is False


def test_frame_is_ignored_when_not_running(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    tab._on_hades_frame(_frame(), 0.0)
    assert tab._table.rowCount() == 0


def test_repaired_frame_is_marked_and_dimmed(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    tab._hades_norad = UNNE1B
    tab._on_hades_frame(_frame(repaired=True), 0.0)
    item = tab._table.item(0, 3)
    assert "repaired" in item.text()
    assert item.data(telemetry_tab_mod._DIM_ROLE) is True
    # a repaired frame is not counted as received
    assert tab._frame_count == 0


def test_load_saved_shows_the_logged_frames_again(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    tab._hades_norad = UNNE1B
    tab._on_hades_frame(_frame(), 0.0)
    tab._on_clear()
    assert tab._table.rowCount() == 0
    _select_hades_mode(tab)
    tab._on_load_saved()
    assert tab._table.rowCount() == 1
    assert tab._table.item(0, 3).text().startswith("[Power]")
    # Loading twice does not add the same row again.
    tab._on_load_saved()
    assert tab._table.rowCount() == 1


def test_hades_rows_do_not_leak_into_other_modes(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _tab(qtbot, conn)
    tab._hades_norad = UNNE1B
    tab._on_hades_frame(_frame(), 0.0)
    tab._on_clear()
    assert tab._load_saved_frames(UNNE1B) == 0  # AX.25 / HDLC rows only
    assert tab._load_saved_frames(UNNE1B, gr=True) == 0
    assert tab._load_saved_frames(UNNE1B, hades=True) == 1
