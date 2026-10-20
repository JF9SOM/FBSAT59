"""Foresail-1p panel and the transmitter matcher that selects it."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from comms.foresail.rx import ForesailRxFrame
from comms.foresail.skylink import build_repeater_frame, build_skylink_frame, tdd_extension
from comms.mode_detection import is_foresail1p_digipeater_transmitter, is_message_box_transmitter
from ui.foresail1p_panel import Foresail1pPanel, describe_frame


def _sat_frame(payload: bytes, **kwargs: object) -> bytes:
    """A frame as the satellite sends it (the uplink builder refuses the satellite's identity)."""
    raw = build_skylink_frame(payload, identity="OH2F1T", **kwargs)  # type: ignore[arg-type]
    return raw.replace(b"OH2F1T", b"OH2F1S", 1)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO app_settings VALUES ('callsign', 'JF9SOM')")
    return conn


def _rx(payload: bytes, start: float = 1.0) -> ForesailRxFrame:
    return ForesailRxFrame(payload, start, start + 0.1, 0, 0)


def test_matcher_selects_the_digipeater_not_the_telemetry_transmitter() -> None:
    digipeater = {
        "norad_cat_id": 66778,
        "description": "Mode U/U - GMSK9k6 - Digipeater - Skylink",
    }
    telemetry = {"norad_cat_id": 66778, "description": "MODE U - GMSK 9k6 TLM Skylink"}
    assert is_foresail1p_digipeater_transmitter(digipeater)
    assert is_message_box_transmitter(digipeater)
    assert not is_foresail1p_digipeater_transmitter(telemetry)
    assert not is_foresail1p_digipeater_transmitter({**digipeater, "norad_cat_id": 12345})


def test_describe_repeater_and_telemetry_frames() -> None:
    ax25 = build_repeater_frame("APRS", "N0CALL", b">hi", ("OH2F1S-11",))
    repeater = describe_frame(_parse(_sat_frame(ax25)))
    assert repeater[0] == "Repeater (VC 3)"
    assert repeater[1:4] == ("N0CALL", "APRS", "OH2F1S-11")
    assert repeater[4] == ">hi"
    pus = bytes(7) + bytes([3, 4]) + bytes(20)
    telemetry = describe_frame(_parse(_sat_frame(pus, vc=1)))
    assert telemetry[0] == "Telemetry (VC 1)" and "UHF housekeeping" in telemetry[4]


def _parse(raw: bytes):  # noqa: ANN202
    from comms.foresail.skylink import parse_skylink_frame

    frame = parse_skylink_frame(raw)
    assert frame is not None
    return frame


def test_panel_logs_repeated_frames_and_tracks_the_satellite_window(qtbot: QtBot) -> None:
    conn = _conn()
    panel = Foresail1pPanel(conn, None, autostart=False)
    qtbot.addWidget(panel)
    ax25 = build_repeater_frame("APRS", "N0CALL", b">heard", ("OH2F1S-11",))
    repeater = _sat_frame(ax25, extensions=(tdd_extension(250, 100),))
    panel._on_frame(_rx(repeater), 50.0)
    assert panel._table.rowCount() == 1
    assert panel._table.item(0, 5).text() == ">heard"
    assert panel._tdd.window_end is not None
    saved = conn.execute("SELECT source, dest, via, content FROM foresail1p_log").fetchall()
    assert [tuple(r) for r in saved] == [("N0CALL", "APRS", "OH2F1S-11", ">heard")]

    # Telemetry frames feed the window tracker but are listed only when asked for.
    telemetry = _sat_frame(bytes(7) + bytes([3, 4]) + bytes(8), vc=1)
    panel._on_frame(_rx(telemetry), 51.0)
    assert panel._table.rowCount() == 1
    panel._telemetry_check.setChecked(True)
    panel._on_frame(_rx(telemetry), 52.0)
    assert panel._table.rowCount() == 2


def test_panel_ignores_its_own_transmission(qtbot: QtBot) -> None:
    panel = Foresail1pPanel(_conn(), None, autostart=False)
    qtbot.addWidget(panel)
    ax25 = build_repeater_frame("APRS", "JF9SOM", b">mine")
    panel._on_frame(_rx(build_skylink_frame(ax25, identity="PyGS")), 10.0)
    assert panel._table.rowCount() == 0


def test_send_without_callsign_or_message_explains_why(qtbot: QtBot) -> None:
    conn = _conn()
    conn.execute("DELETE FROM app_settings WHERE key = 'callsign'")
    panel = Foresail1pPanel(conn, None, autostart=False)
    qtbot.addWidget(panel)
    panel._on_send()
    assert "My Call" in panel._tx_status_label.text()
    conn.execute("INSERT INTO app_settings VALUES ('callsign', 'JF9SOM')")
    panel._on_send()
    assert "Message is required" in panel._tx_status_label.text()


@pytest.fixture
def stub_uplink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the uplink builder, which needs the RS library (not what is tested here)."""
    monkeypatch.setattr(
        "ui.foresail1p_panel.build_uplink",
        lambda *_a, **_k: (b"", np.zeros(7200, dtype=np.float32)),  # 0.15 s at 48 kHz
    )


def test_auto_mode_arms_the_message_and_cancel_clears_it(qtbot: QtBot, stub_uplink: None) -> None:
    panel = Foresail1pPanel(_conn(), None, autostart=False)
    qtbot.addWidget(panel)
    panel._mode_combo.setCurrentIndex(1)
    panel._message_edit.setText(">test")
    panel._on_send()
    assert panel._armed is not None
    panel._on_cancel_armed()
    assert panel._armed is None


def test_armed_message_waits_for_the_satellite_window_to_end(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, stub_uplink: None
) -> None:
    panel = Foresail1pPanel(_conn(), None, autostart=False)
    qtbot.addWidget(panel)
    sent: list[tuple[object, ...]] = []
    monkeypatch.setattr(panel, "_transmit", lambda *args: sent.append(args))
    panel._mode_combo.setCurrentIndex(1)
    panel._message_edit.setText(">test")
    panel._on_send()
    panel._tdd.observe(100.0, 250, 100)  # window ends at 100.1
    panel._try_armed(100.0)
    assert sent == [] and panel._armed is not None  # inside the window: keep waiting
    panel._try_armed(101.0)  # well after the guard
    assert len(sent) == 1 and panel._armed is None
