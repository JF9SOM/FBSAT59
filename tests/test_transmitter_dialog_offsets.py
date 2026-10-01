"""TransmitterDialog: DL/UL offset fields and effective-frequency labels."""

from __future__ import annotations

import sqlite3

import pytest
from pytestqt.qtbot import QtBot

from data.database import SCHEMA_SQL
from data.transmitter_manager import TransmitterManager
from ui.transmitter_dialog import TransmitterDialog


@pytest.fixture()
def tm() -> TransmitterManager:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return TransmitterManager(conn)


def test_add_mode_saves_offsets_and_shows_effective(qtbot: QtBot, tm: TransmitterManager) -> None:
    dlg = TransmitterDialog(tm, norad_cat_id=44909)
    qtbot.addWidget(dlg)
    dlg._desc_edit.setText("FT4")
    dlg._dl_spin.setValue(435.612)
    dlg._ul_spin.setValue(145.993)
    dlg._ul_offset_spin.setValue(-2400)
    assert dlg._eff_ul_label.text() == "145.990600 MHz"
    assert dlg._eff_dl_label.text() == "435.612000 MHz"
    dlg._on_accept()
    row = tm.get_transmitters(44909)[0]
    assert row["ul_offset_hz"] == -2400.0
    assert row["rx_offset_hz"] == 0.0


def test_edit_mode_prefills_and_updates(qtbot: QtBot, tm: TransmitterManager) -> None:
    xpdr_uuid = tm.add_manual_transmitter(
        norad_cat_id=44909,
        description="FT4",
        downlink_low=435612000,
        mode="USB-D",
        uplink_low=145993000,
        rx_offset_hz=50.0,
        ul_offset_hz=-2400.0,
    )
    existing = dict(
        tm._conn.execute("SELECT * FROM transmitters WHERE uuid = ?", (xpdr_uuid,)).fetchone()
    )
    dlg = TransmitterDialog(tm, existing=existing)
    qtbot.addWidget(dlg)
    assert dlg._dl_offset_spin.value() == 50
    assert dlg._ul_offset_spin.value() == -2400
    dlg._ul_offset_spin.setValue(-1800)
    dlg._on_accept()
    row = tm.get_transmitters(44909)[0]
    assert row["ul_offset_hz"] == -1800.0
    assert row["rx_offset_hz"] == 50.0


def test_effective_label_uses_band_centre_and_dash_when_none(
    qtbot: QtBot, tm: TransmitterManager
) -> None:
    dlg = TransmitterDialog(tm)
    qtbot.addWidget(dlg)
    dlg._ul_spin.setValue(0.0)
    assert dlg._eff_ul_label.text() == "-"
    dlg._dl_spin.setValue(145.800)
    dlg._dl_high_spin.setValue(146.000)
    dlg._dl_offset_spin.setValue(1000)
    assert dlg._eff_dl_label.text() == "145.901000 MHz"
