"""Arica2Panel / MessageBoxTab: command sending, uplink-window arming, RX rows.

Uses pytest-qt's qtbot (new QWidget tests must register widgets with
qtbot.addWidget). The shared AprsEngine is replaced by a fake that records calls, and
the transmit worker by one that records the audio it would play (the audio is decoded
back with an independent G3RUH receiver, so the whole TX chain is checked).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal
from pytestqt.qtbot import QtBot

import ui.arica2_panel as panel_mod
from comms.arica2.message_box import Command, build_command
from tests.test_g3ruh_tx import _decode
from ui.arica2_panel import Arica2Panel


class _FakeEngine(QObject):
    raw_frame_received: Signal = Signal(bytes)
    error_occurred: Signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def start_sdr_direwolf(self, owner: str, pipeline: Any, modem: str = "1200", **kw: Any) -> Any:
        self.calls.append(f"sdr:{modem}")
        return True, ""

    def start_rig(
        self,
        owner: str,
        call: str,
        ssid: int,
        via: str,
        modem: str = "1200",
    ) -> Any:
        self.calls.append(f"rig:{modem}")
        return True, ""

    def sync_sdr_baud(self, pipeline: Any, modem: str) -> None:
        pass

    def restart_if_modem_changed(self, modem: str) -> None:
        pass

    def stop(self, owner: str) -> None:
        self.calls.append("stop")


class _FakeTxWorker(QObject):
    """Stands in for _Arica2TxWorker: records what would have been played."""

    finished: Signal = Signal()
    error: Signal = Signal(str)
    played: list[tuple[Any, int | None, Any]] = []

    def __init__(
        self, owner: str, audio: Any, out_device: int | None, rig: Any, parent: Any = None
    ) -> None:
        super().__init__(parent)
        _FakeTxWorker.played.append((audio, out_device, rig))

    def run(self) -> None:
        self.finished.emit()


class _FakeBasebandThread(QObject):
    """Stands in for G3ruhBasebandRxThread: no thread, frames are injected by tests."""

    frame_received: Signal = Signal(bytes)
    instances: list[_FakeBasebandThread] = []

    def __init__(self, baud: int = 4800, validator: Any = None, parent: Any = None) -> None:
        super().__init__(parent)
        self.baud = baud
        self.validator = validator
        self.started = False
        self.stopped = False
        self.pushed: list[Any] = []
        _FakeBasebandThread.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def push_samples(self, audio: Any) -> None:
        self.pushed.append(audio)


class _FakeAudioManager:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, int | None, int]] = []
        self.released: list[tuple[str, int | None]] = []

    def acquire_input(self, owner: str, device: int | None, rate: int, cb: Any) -> None:
        self.inputs.append((owner, device, rate))

    def release_input(self, owner: str, device: int | None) -> None:
        self.released.append((owner, device))


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
    c.execute(
        "INSERT INTO app_settings (key, value) VALUES ('soundcard_settings', ?)",
        (json.dumps({"input_device_index": 1, "output_device_index": 3}),),
    )
    return c


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    fake = _FakeEngine()
    monkeypatch.setattr(panel_mod, "get_aprs_engine", lambda _conn: fake)
    _FakeTxWorker.played = []
    monkeypatch.setattr(panel_mod, "PttAudioTxWorker", _FakeTxWorker)
    _FakeBasebandThread.instances = []
    monkeypatch.setattr(panel_mod, "G3ruhBasebandRxThread", _FakeBasebandThread)
    manager = _FakeAudioManager()
    monkeypatch.setattr(panel_mod, "get_audio_device_manager", lambda: manager)
    fake.audio_manager = manager  # type: ignore[attr-defined]
    return fake


def _sent() -> list[bytes]:
    """Payloads decoded back from the audio the panel handed to the TX worker."""
    out: list[bytes] = []
    for audio, _dev, _rig in _FakeTxWorker.played:
        frames = _decode(audio)
        assert len(frames) == 1
        out.append(frames[0])
    return out


def _make(qtbot: QtBot, conn: sqlite3.Connection) -> Arica2Panel:
    p = Arica2Panel(conn, _FakeRadioControl(), parent=None)  # type: ignore[arg-type]
    qtbot.addWidget(p)
    return p


def test_sdr_input_starts_4800_receive_only(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    _make(qtbot, conn)
    assert engine.calls == ["sdr:4800"]


def test_manual_mode_sends_immediately(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._message_edit.setText("hello")
    p._upload_btn.click()
    assert _sent() == [build_command(Command.UPLOAD, "JF9SOM", "hello")]
    _audio, device, rig = _FakeTxWorker.played[0]
    assert device == 3  # the Sound Card output device from Rig Settings
    assert isinstance(rig, _FakeRig)
    assert p._table.rowCount() == 1  # TX echo row


def test_download_uses_the_slot(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._slot_spin.setValue(3)
    p._download_btn.click()
    assert _sent() == [build_command(Command.DOWNLOAD, "JF9SOM", slot=3)]


def test_upload_needs_a_message(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._upload_btn.click()
    assert _sent() == []


def test_missing_callsign_is_reported(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    conn.execute("DELETE FROM app_settings WHERE key = 'callsign'")
    p = _make(qtbot, conn)
    p._message_edit.setText("hi")
    p._upload_btn.click()
    assert _sent() == []
    assert "Set QTH" in p._tx_status_label.text()


def test_auto_mode_waits_for_the_window(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._message_edit.setText("hello")
    p._upload_btn.click()
    assert _sent() == []
    assert p._armed is not None

    p._window_event.emit(6.0, 12.0)
    qtbot.waitUntil(lambda: len(_FakeTxWorker.played) == 1, timeout=3000)
    assert _sent()[0] == build_command(Command.UPLOAD, "JF9SOM", "hello")
    assert p._armed is None


def test_auto_mode_sends_at_once_when_the_window_is_already_open(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._window_event.emit(6.0, 12.0)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    assert len(_FakeTxWorker.played) == 1


def test_window_event_with_too_little_time_left_does_not_fire(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._mode_combo.setCurrentIndex(1)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    p._window_event.emit(6.0, 1.0)
    qtbot.wait(800)
    assert _sent() == []
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
    # Direwolf only receives; the panel transmits through its own audio.
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
    assert engine.calls == ["sdr:4800"]
    assert requests == [("ax100digi", 68796)]  # ARICA-2's satellite is requested

    tab._protocol_combo.setCurrentIndex(0)
    assert engine.calls[-1] == "stop"
    assert requests[-1] == ("ax100digi", 69912)  # back to MARMOTSat

    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'message_box_tab_settings'"
    ).fetchone()
    assert "ax100" in row[0]


def test_tx_level_slider_is_in_db_and_scales_the_audio(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    full = float(np.max(np.abs(_FakeTxWorker.played[0][0])))
    assert full == pytest.approx(1.0, abs=1e-3)  # default 0 dB: full scale

    p._level_slider.setValue(-20)
    assert p._level_label.text() == "-20 dB"
    qtbot.waitUntil(lambda: not p._tx_in_progress, timeout=3000)
    p._parrot_btn.click()
    quiet = float(np.max(np.abs(_FakeTxWorker.played[1][0])))
    assert quiet == pytest.approx(0.1, abs=1e-3)
    assert _sent()[0] == _sent()[1]  # the level changes the amplitude, not the data

    p.shutdown()
    saved = conn.execute("SELECT value FROM app_settings WHERE key = 'arica2_settings'").fetchone()
    assert '"tx_level_db": -20' in saved[0]


def test_legacy_percent_level_setting_is_migrated_to_db(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('arica2_settings', '{\"tx_level\": 10}')"
    )
    p = _make(qtbot, conn)
    assert p._level_slider.value() == -20  # 10 % == -20 dB


def test_no_sound_card_output_means_no_transmission(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    conn.execute("DELETE FROM app_settings WHERE key = 'soundcard_settings'")
    p = _make(qtbot, conn)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    assert _FakeTxWorker.played == []
    assert "Sound Card" in p._tx_status_label.text()


def test_a_second_command_is_refused_while_one_is_transmitting(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Slow(_FakeTxWorker):
        def run(self) -> None:  # never finishes
            pass

    monkeypatch.setattr(panel_mod, "PttAudioTxWorker", _Slow)
    p = _make(qtbot, conn)
    p._message_edit.setText("hi")
    p._parrot_btn.click()
    p._parrot_btn.click()
    assert len(_FakeTxWorker.played) == 1
    assert "already in progress" in p._tx_status_label.text()


def test_rig_sound_card_input_also_runs_the_baseband_decoder(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    assert _FakeBasebandThread.instances == []  # SDR input: the coherent detector is used
    p._rb_soundcard.setChecked(True)
    (thread,) = _FakeBasebandThread.instances
    assert thread.started and thread.baud == 4800
    manager = engine.audio_manager  # type: ignore[attr-defined]
    assert manager.inputs == [("ARICA-2 baseband decoder", 1, 48_000)]

    p._rb_sdr.setChecked(True)  # leaving the sound card releases the audio and the thread
    assert thread.stopped
    assert manager.released == [("ARICA-2 baseband decoder", 1)]


def test_a_frame_seen_by_both_decoders_is_shown_once(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._rb_soundcard.setChecked(True)
    (thread,) = _FakeBasebandThread.instances

    def shifted(text: str) -> bytes:
        return bytes(ord(c) << 1 for c in text.ljust(6))

    frame = shifted("JI1IZR") + b"\x60" + shifted("JS1YSD") + b"\x61\x03\xf0" + b"JI1IZR:N6RFMr73"
    engine.raw_frame_received.emit(frame)  # Direwolf
    thread.frame_received.emit(frame)  # baseband decoder, same frame
    assert p._table.rowCount() == 1
    assert p._table.item(0, 3).text() == "JI1IZR:N6RFMr73"

    other = frame[:-2] + b"99"  # a different frame is still shown
    thread.frame_received.emit(other)
    assert p._table.rowCount() == 2


def test_no_sound_card_input_device_means_no_baseband_decoder(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'soundcard_settings'",
        (json.dumps({"output_device_index": 3}),),
    )
    p = _make(qtbot, conn)
    p._rb_soundcard.setChecked(True)
    assert _FakeBasebandThread.instances == []


def test_baseband_decoder_validates_weak_frames_as_arica2_replies(
    qtbot: QtBot, conn: sqlite3.Connection, engine: _FakeEngine
) -> None:
    p = _make(qtbot, conn)
    p._rb_soundcard.setChecked(True)
    (thread,) = _FakeBasebandThread.instances
    assert thread.validator is not None
    assert not thread.validator(b".l\x8a\x97\x12ob")  # the false frame seen in a real recording
    reply = bytes(ord(c) << 1 for c in "JI1IZR") + b"\x60" + bytes(ord(c) << 1 for c in "JS1YSD")
    assert thread.validator(reply + b"\x61\x03\xf0" + b"saved 'AAA' at box: 1")
