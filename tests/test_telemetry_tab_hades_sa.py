"""The Telemetry tab's HADES FSK mode with HADES-SA (800 baud) next to UNNE-1B."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import ui.telemetry_tab as telemetry_tab_mod
from comms.telemetry.hades_fsk import HadesFrame
from comms.telemetry.hades_fsk_sdr import HADES_SA_NORAD, make_stream
from comms.telemetry.hades_sa_fsk import HadesSaFskStream
from ui.telemetry_tab import TelemetryTab

STATUS = bytes.fromhex(
    "33868b0901848b0901010044470035001bff00008400ffffffffff006438001225000045780043"
)


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
    c.execute("INSERT INTO satellites VALUES (68446, 'HADES-SA', 0)")
    c.execute("INSERT INTO satellites VALUES (98205, 'UNNE-1B', 0)")
    for uuid, norad, desc, mode, baud in (
        ("a", 68446, "SSDV", "SSDV", 800),
        ("b", 68446, "FSK 800 bps 1600 Hz separation", "GENESIS FSK", 800),
        ("c", 68446, "FSK 200 bps 1125 Hz separation", "GENESIS FSK", 200),
        ("d", 98205, "Mode U - 200bps FSK TLM", "FSK", 200),
    ):
        c.execute(
            "INSERT INTO transmitters VALUES (?, ?, ?, ?, ?, 1)", (uuid, norad, desc, mode, baud)
        )
    return c


def _tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def _select_sat(tab: TelemetryTab, norad: int) -> None:
    tab._combo_mode.setCurrentIndex(tab._combo_mode.findText(telemetry_tab_mod._MODE_HADES))
    combo = tab._combo_hades_sat
    combo.setCurrentIndex(combo.findData(norad))


def _frame() -> HadesFrame:
    return HadesFrame(0, 0.0, 300.0, 14.6, 3, 3, STATUS, False)


def test_combo_lists_hades_sa_and_unne_1b(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    combo = _tab(qtbot, conn)._combo_hades_sat
    assert sorted(combo.itemData(i) for i in range(combo.count())) == [68446, 98205]


def test_stream_choice_follows_the_satellite() -> None:
    assert isinstance(make_stream(250_000, HADES_SA_NORAD), HadesSaFskStream)
    assert not isinstance(make_stream(250_000, 98205), HadesSaFskStream)


def test_start_for_hades_sa_runs_the_800_baud_demodulator(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    pipeline = _FakePipeline()
    tab._sdr_pipeline = pipeline
    tab._on_start()
    try:
        assert tab._hades_demod is not None
        assert isinstance(tab._hades_demod._stream, HadesSaFskStream)
        assert tab._hades_norad == 68446
        assert "800 baud" in tab._lbl_status.text()
    finally:
        tab._on_stop()
    assert pipeline.subscribers == []


def test_start_for_unne_1b_still_runs_the_200_baud_demodulator(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 98205)
    tab._sdr_pipeline = _FakePipeline()
    tab._on_start()
    try:
        assert tab._hades_demod is not None
        assert not isinstance(tab._hades_demod._stream, HadesSaFskStream)
        assert "200 baud" in tab._lbl_status.text()
    finally:
        tab._on_stop()


def test_a_hades_sa_status_frame_is_shown_logged_and_decoded(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    tab._hades_norad = 68446
    tab._on_hades_frame(_frame(), 1.0)
    assert tab._table.rowCount() == 1
    assert tab._table.item(0, 3).text().startswith("[Status]")
    assert tab._table.item(0, 2).text() == "HADES-SA"
    row = conn.execute("SELECT kind, norad_cat_id, frame_hex FROM telemetry_log").fetchone()
    assert (row["kind"], row["norad_cat_id"], row["frame_hex"]) == ("hades", 68446, STATUS.hex())


def test_decoded_fields_tabs_are_hades_sas_and_fill_from_a_frame(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    tab._rebuild_decode_tabs(68446)
    # status (3) and BBS data (15), no SSDV (10) and no UNNE-1B voice
    assert sorted(tab._decode_tables, key=int) == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "8",
        "9",
        "12",
        "14",
        "15",
    ]
    tab._hades_norad = 68446
    tab._on_hades_frame(_frame(), 1.0)
    table = tab._decode_tables["3"]
    labels = [table.item(r, 0).text() for r in range(table.rowCount())]
    assert "Current SSDV image id" in labels
