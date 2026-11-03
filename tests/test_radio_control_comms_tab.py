"""The comms tab opened after the rig connects follows the default transmitter too."""

from __future__ import annotations

from typing import Any

from pytestqt.qtbot import QtBot

_CW = {"description": "Mode V - CW", "mode": "CW", "norad_cat_id": 59112}
_APRS = {"description": "Mode V/V - APRS digipeater", "mode": "AFSK", "norad_cat_id": 59112}
_SSTV = {"description": "Mode V - SSTV", "mode": "SSTV", "norad_cat_id": 59112}


def _make(qtbot: QtBot) -> Any:
    from ui.radio_control_widget import RadioControlWidget

    w = RadioControlWidget()
    qtbot.addWidget(w)
    return w


def test_default_aprs_row_opens_the_aprs_tab_after_connect(qtbot: QtBot) -> None:
    w = _make(qtbot)
    w.set_transmitters([_APRS, _CW, _SSTV], default_index=0)
    with qtbot.waitSignal(w.aprs_transponder_selected, timeout=1000):
        w._finish_rig1_connect(True)


def test_default_cw_row_remembers_no_tab(qtbot: QtBot) -> None:
    w = _make(qtbot)
    w.set_transmitters([_CW, _APRS], default_index=0)
    assert w._pending_comms_tab is None


def test_picking_another_row_still_updates_the_tab(qtbot: QtBot) -> None:
    w = _make(qtbot)
    w.set_transmitters([_CW, _APRS, _SSTV], default_index=0)
    w._xpdr_combo.setCurrentIndex(1)
    assert w._pending_comms_tab == "aprs"
    w._xpdr_combo.setCurrentIndex(2)
    assert w._pending_comms_tab == "sstv"


def test_no_transmitters_clears_the_tab(qtbot: QtBot) -> None:
    w = _make(qtbot)
    w.set_transmitters([_APRS], default_index=0)
    w.set_transmitters([], default_index=0)
    assert w._pending_comms_tab is None
