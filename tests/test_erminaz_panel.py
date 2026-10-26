"""ERMINAZ-2 panel and the transmitter matcher that selects it."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from comms.erminaz.frames import MAX_MESSAGE_LENGTH
from comms.erminaz.rx import ErminazRxFrame
from comms.mode_detection import is_erminaz_digipeater_transmitter, is_message_box_transmitter
from ui.erminaz_panel import ErminazPanel


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO app_settings VALUES ('callsign', 'JF9SOM')")
    return conn


def _tm_frame(data: bytes, apid: int = 600) -> bytes:
    packet = apid.to_bytes(2, "big") + bytes.fromhex("c000") + (len(data) - 1).to_bytes(2, "big")
    frame = (102 << 4 | 1).to_bytes(2, "big") + bytes([1, 2]) + bytes.fromhex("1800")
    frame += packet + data
    return frame + b"\xe0" * (223 - len(frame))


def test_matcher_selects_the_digipeater_transmitters_not_the_cw_beacon() -> None:
    digi = {"norad_cat_id": 98207, "description": "Mode U/U - FSK9k6 - Digipeater"}
    gmsk = {
        "norad_cat_id": 98207,
        "description": "Mode-U/U 9k6 GMSK Telemetry, SSDV, Digipeater",
    }
    cw = {"norad_cat_id": 98207, "description": "Mode U - CW"}
    assert is_erminaz_digipeater_transmitter(digi) and is_erminaz_digipeater_transmitter(gmsk)
    assert is_message_box_transmitter(digi)
    assert not is_erminaz_digipeater_transmitter(cw)
    assert not is_erminaz_digipeater_transmitter({**digi, "norad_cat_id": 12345})
    assert is_erminaz_digipeater_transmitter({**digi, "norad_cat_id": 98209})


def test_panel_logs_digipeater_messages_only(qtbot: QtBot) -> None:
    conn = _conn()
    panel = ErminazPanel(conn, None, autostart=False)
    qtbot.addWidget(panel)
    data = bytes.fromhex("a80c") + b"ALL\x00\x00\x00DP0PQA" + b"DIGIPEATER ON\x00"
    panel._on_frame(ErminazRxFrame(_tm_frame(data), 1.0, 1.3, 0, 0), 10.0)
    panel._on_frame(ErminazRxFrame(_tm_frame(b"\x00" * 40, apid=300), 2.0, 2.3, 0, 0), 11.0)
    assert panel._table.rowCount() == 1
    assert panel._table.item(0, 1).text() == "ERMINAZ-2U"
    assert panel._table.item(0, 2).text() == "DP0PQA"
    assert panel._table.item(0, 4).text() == "DIGIPEATER ON"
    assert panel._frames_total == 2 and panel._digi_total == 1
    saved = conn.execute("SELECT spacecraft, source, dest, content FROM erminaz_log").fetchall()
    assert [tuple(r) for r in saved] == [(102, "DP0PQA", "ALL", "DIGIPEATER ON")]


def test_send_explains_missing_input(qtbot: QtBot) -> None:
    conn = _conn()
    conn.execute("DELETE FROM app_settings WHERE key = 'callsign'")
    panel = ErminazPanel(conn, None, autostart=False)
    qtbot.addWidget(panel)
    panel._on_send()
    assert "My Call" in panel._tx_status_label.text()
    conn.execute("INSERT INTO app_settings VALUES ('callsign', 'JF9SOM')")
    panel._on_send()
    assert "Message is required" in panel._tx_status_label.text()
    panel._message_edit.setText("x" * (MAX_MESSAGE_LENGTH + 1))
    panel._on_send()
    assert "too long" in panel._tx_status_label.text()


def test_send_needs_a_connected_rig(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = ErminazPanel(_conn(), None, autostart=False)
    qtbot.addWidget(panel)
    monkeypatch.setattr(
        "ui.erminaz_panel.build_uplink",
        lambda *_a, **_k: (b"", np.zeros(7200, dtype=np.float32)),
    )
    panel._message_edit.setText("hello")
    panel._on_send()
    assert "TX rig not connected" in panel._tx_status_label.text()


def test_satellite_combo_follows_the_selected_spacecraft(qtbot: QtBot) -> None:
    panel = ErminazPanel(_conn(), None, autostart=False)
    qtbot.addWidget(panel)
    assert panel.current_norad() == 98207
    with qtbot.waitSignal(panel.satellite_changed):
        panel._sat_combo.setCurrentIndex(panel._sat_combo.findData("ERMINAZ-2X"))
    assert panel.current_norad() == 98209
