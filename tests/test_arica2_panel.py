"""Arica2Panel / MessageBoxTab: command sending, uplink-window arming, RX rows.

Uses pytest-qt's qtbot (new QWidget tests must register widgets with
qtbot.addWidget). The shared AprsEngine is replaced by a fake that records calls.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal
from pytestqt.qtbot import QtBot

import ui.arica2_panel as panel_mod
from comms.arica2.message_box import Command, build_command
from ui.arica2_panel import Arica2Panel


class _FakeEngine(QObject):
    raw_frame_received: Signal = Signal(bytes)
    error_occurred: Signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []
        self.calls: list[str] = []
        self.can_transmit = True
        self.gains: list[float] = []

    def start_sdr_direwolf(self, owner: str, pipeline: Any, modem: str = "1200", **kw: Any) -> Any:
        self.calls.append(f"sdr:{modem}:tx={kw.get('tx')}")
        return True, ""

    def start_rig(self, owner: str, call: str, ssid: int, via: str, modem: str = "1200") -> Any:
        self.calls.append(f"rig:{modem}")
        return True, ""

    def sync_sdr_baud(self, pipeline: Any, modem: str) -> None:
        pass

    def restart_if_modem_changed(self, modem: str) -> None:
        pass

    def set_rig(self, rig: Any) -> None:
        pass

    def send_raw(self, payload: bytes, audio_s: float | None = None) -> bool:
        self.sent.append(payload)
        return True

    def stop(self, owner: str) -> None:
        self.calls.append("stop")

    def set_tx_gain(self, gain: float) -> None:
        self.gains.append(gain)


class _FakeRig:
    is_connected = True


class _FakePipeline:
    class _Device:
        sample_rate = 250_000

    _device = _Device()

    def subscribe(self, _cb: Any) -> None:
        pass

    def unsubscribe(self, _cb: Any) -> None:
        pass


class _FakeSdrControl:
    _pipeline = _FakePipeline()


class _FakeRadioControl:
    _rig1 = _FakeRig()
    _rig2 = None
    _sdr_control = _FakeSdrControl()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute("INSERT INTO app_settings (key, value) VALUES ('callsign', 'JF9SOM')")
    return c


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    fake = _FakeEngine()
    monkeypatch.setattr(panel_mod, "get_aprs_engine", lambda _conn: fake)
    return fake


def _make(qtbot: QtBot, conn: sqlite3.Connection) -> Arica2Panel:
    p = Arica2Panel(conn, _FakeRadioControl(), parent=None)  # type: ignore[arg-type]
    qtbot.addWidget(p)
    return p


def test_sdr_input_starts_4800_with_tx(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    _make(qtbot, conn)
    assert engine.calls == ["sdr:4800:tx=True"]


def test_manual_mode_sends_immediately(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._message_edit.setText("hello")
    p._upload_btn.click()
    assert engine.sent == [build_command(Command.UPLOAD, "JF9SOM", "hello")]
    assert p._table.rowCount() == 1  # TX echo row


def test_download_uses_the_slot(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._slot_spin.setValue(3)
    p._download_btn.click()
    assert engine.sent == [build_command(Command.DOWNLOAD, "JF9SOM", slot=3)]


def test_upload_needs_a_message(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._upload_btn.click()
    assert engine.sent == []


def test_missing_callsign_is_reported(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    conn.execute("DELETE FROM app_settings WHERE key = 'callsign'")
    p = _make(qtbot, conn)
    p._message_edit.setText("hi")
    p._upload_btn.click()
    assert engine.sent == []
    assert "Set QTH" in p._tx_status_label.text()


def test_auto_mode_waits_for_the_window(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._message_edit.setText("hello")
    p._upload_btn.click()
    assert engine.sent == []
    assert p._armed is not None

    p._window_event.emit(6.0, 12.0)
    qtbot.waitUntil(lambda: len(engine.sent) == 1, timeout=3000)
    assert engine.sent[0] == build_command(Command.UPLOAD, "JF9SOM", "hello")
    assert p._armed is None


def test_auto_mode_sends_at_once_when_the_window_is_already_open(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._window_event.emit(6.0, 12.0)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    assert len(engine.sent) == 1


def test_window_event_with_too_little_time_left_does_not_fire(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    p._window_event.emit(6.0, 1.0)
    qtbot.wait(800)
    assert engine.sent == []
    assert p._armed is not None


def test_cancel_clears_the_armed_command(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    p._cancel_btn.click()
    assert p._armed is None


def test_soundcard_input_is_manual_only(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._rb_soundcard.setChecked(True)
    assert p._mode_combo.currentData() == "manual"
    assert "rig:4800" in engine.calls


def test_received_frame_is_parsed_and_stored(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)

    def shifted(text: str, n: int) -> bytes:
        return bytes(ord(c) << 1 for c in text.ljust(n))

    frame = (
        shifted("JF9SO", 5)
        + shifted("JS1YSD", 6)
        + bytes([0x61, 0x03, 0xF0])
        + b"saved '' at box: 3"
    )
    engine.raw_frame_received.emit(frame)
    assert p._table.item(0, 1).text() == "JS1YSD"
    assert p._table.item(0, 3).text() == "saved '' at box: 3"
    assert conn.execute("SELECT COUNT(*) FROM arica2_log").fetchone()[0] == 1

    engine.raw_frame_received.emit(b"\x01\x02\x03")
    assert p._table.item(1, 1).text() == "?"


def test_shutdown_releases_the_engine(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p.shutdown()
    assert engine.calls[-1] == "stop"


def test_message_box_tab_switches_protocol_and_hands_over_the_input(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    from ui.message_box_tab import MessageBoxTab

    tab = MessageBoxTab(conn, _FakeRadioControl(), parent=None)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    assert tab.current_protocol() == "ax100"
    assert engine.calls == []  # ARICA-2 panel idle while AX100 is shown

    requests: list[tuple[str, int]] = []
    tab.satellite_requested.connect(lambda key, norad: requests.append((key, norad)))

    tab._protocol_combo.setCurrentIndex(1)
    assert tab.current_protocol() == "arica2"
    assert engine.calls == ["sdr:4800:tx=True"]
    assert requests == [("ax100digi", 68796)]  # ARICA-2's satellite is requested

    tab._protocol_combo.setCurrentIndex(0)
    assert engine.calls[-1] == "stop"
    assert requests[-1] == ("ax100digi", 69912)  # back to MARMOTSat

    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'message_box_tab_settings'"
    ).fetchone()
    assert "ax100" in row[0]


def test_tx_level_slider_sets_the_engine_gain_and_is_restored(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    assert engine.gains[-1] == 1.0  # default level on start
    p._level_slider.setValue(30)
    assert engine.gains[-1] == 0.3
    assert p._level_label.text() == "30%"
    p.shutdown()
    assert engine.gains[-1] == 1.0  # engine-wide gain put back for APRS
    saved = conn.execute("SELECT value FROM app_settings WHERE key = 'arica2_settings'").fetchone()
    assert '"tx_level": 30' in saved[0]
