"""Tests for the SSTV / SSDV tab's SSDV mode: Raw Packets / Image sub-tabs, live frames,
pasted hex, and self-started AX.25 reception."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QMimeData, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import comms.sstv.ssdv as ssdv_mod
from comms.sstv.ssdv import format_hex_line
from tests.test_ssdv import AX25_HEADER, make_packet
from ui.sstv_tab import SstvTab


class _FakeEngine(QObject):
    raw_frame_received = Signal(bytes)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append((name, args, kwargs))
        return True, ""

    def start_sdr_direwolf(self, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        return self._record("start_sdr_direwolf", *args, **kwargs)

    def start_rig(self, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        return self._record("start_rig", *args, **kwargs)

    def stop(self, *args: Any) -> None:
        self._record("stop", *args)

    def restart_if_modem_changed(self, *args: Any) -> None:
        self._record("restart_if_modem_changed", *args)

    def sync_sdr_baud(self, *args: Any, **kwargs: Any) -> None:
        self._record("sync_sdr_baud", *args, **kwargs)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class _FakeSignal:
    def connect(self, _slot: Any) -> None:
        pass

    def disconnect(self, _slot: Any) -> None:
        pass


class _FakePipeline:
    """Just enough of SDRPipeline for the SSTV side's audio hookup."""

    audio_ready = _FakeSignal()

    def request_audio(self, _owner: str) -> None:
        pass

    def release_audio(self, _owner: str) -> None:
        pass


class _FakeRig:
    is_sdr = True
    _pipeline = _FakePipeline()


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    sdr_connected = Signal()
    sdr_disconnected = Signal()
    transmitter_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._rig1: Any = None
        self._rig2: Any = None

    def current_transmitter(self) -> dict[str, Any] | None:
        return {"baud": 1200}


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return c


@pytest.fixture
def rc(qtbot: QtBot) -> _FakeRadioControl:
    w = _FakeRadioControl()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def engine() -> _FakeEngine:
    return _FakeEngine()


@pytest.fixture
def tab(
    qtbot: QtBot,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SstvTab:
    # Completed images are auto-saved as PNGs: keep them out of the real Pictures folder
    def _save_here(
        self: SstvTab, qimg: QImage, mode: str, ts: Any, sat_name: str | None = None
    ) -> str:
        path = str(tmp_path / f"{mode}_{ts:%H%M%S%f}.png")
        qimg.save(path)
        return path

    monkeypatch.setattr(SstvTab, "_auto_save_image", _save_here)
    t = SstvTab(conn, rc, aprs_engine=engine)
    qtbot.addWidget(t)
    return t


def _connect_sdr(rc: _FakeRadioControl) -> None:
    rc._rig1 = _FakeRig()
    rc.sdr_connected.emit()


def _fake_ssdv_binary(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """ssdv replaced by a stub that writes a picture; returns the packet data it was given."""
    seen: list[bytes] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(Path(argv[-2]).read_bytes())  # ssdv is given files: ... <packets> <image>
        img = QImage(80, 60, QImage.Format.Format_RGB32)
        img.fill(QColor("#c07030"))
        img.save(argv[-1], "PNG")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(ssdv_mod, "find_ssdv", lambda: "/fake/ssdv")
    monkeypatch.setattr(ssdv_mod.subprocess, "run", run)
    return seen


def _paste(tab: SstvTab, text: str) -> None:
    mime = QMimeData()
    mime.setText(text)
    tab._raw_edit.insertFromMimeData(mime)


def _tab_names(tab: SstvTab) -> list[str]:
    return [tab._view_tabs.tabText(i) for i in range(tab._view_tabs.count())]


# ---------------------------------------------------------------- layout


def test_image_area_has_raw_packets_and_image_subtabs(tab: SstvTab) -> None:
    assert _tab_names(tab) == ["Raw Packets", "Image"]
    assert tab._view_tabs.currentWidget() is tab._image_page  # SSTV is the default mode


def test_switching_mode_selects_the_matching_subtab(tab: SstvTab) -> None:
    tab._mode_combo.setCurrentText("SSDV")
    assert tab._view_tabs.currentWidget() is tab._raw_page
    tab._mode_combo.setCurrentText("SSTV")
    assert tab._view_tabs.currentWidget() is tab._image_page


# ------------------------------------------------- self-started reception


def test_ssdv_mode_starts_sdr_reception_without_the_aprs_tab(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV")
    name, args, kwargs = engine.calls[-1]
    assert name == "start_sdr_direwolf"
    assert args[0] == "sstv"
    assert args[1] is rc._rig1._pipeline
    assert kwargs == {"modem": "1200", "satellite": True}
    assert "APRS" not in tab._status_label.text()


def test_ssdv_mode_starts_sound_card_reception_with_a_rig(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    rc.rig_connected.emit()
    tab._mode_combo.setCurrentText("SSDV")
    name, args, kwargs = engine.calls[-1]
    assert name == "start_rig"
    assert args[0] == "sstv"
    assert kwargs == {"modem": "1200"}


def test_ssdv_mode_without_an_input_says_so_and_never_asks_for_the_aprs_tab(
    tab: SstvTab, engine: _FakeEngine
) -> None:
    tab._mode_combo.setCurrentText("SSDV")
    assert engine.calls == []
    text = tab._status_label.text()
    assert "no audio source" in text
    assert "open APRS tab" not in text


def test_connecting_an_input_after_entering_ssdv_mode_starts_reception(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    tab._mode_combo.setCurrentText("SSDV")
    _connect_sdr(rc)
    assert engine.names() == ["start_sdr_direwolf"]


def test_disconnecting_the_input_releases_the_pipeline(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV")
    rc.sdr_disconnected.emit()
    assert engine.names() == ["start_sdr_direwolf", "stop"]
    assert engine.calls[-1][1] == ("sstv",)


def test_leaving_ssdv_mode_releases_the_pipeline_and_stops_listening(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV")
    tab._mode_combo.setCurrentText("SSTV")
    assert engine.names() == ["start_sdr_direwolf", "stop"]
    engine.raw_frame_received.emit(b"\x03")  # the engine no longer feeds this tab
    assert tab._raw_edit.toPlainText() == ""


def test_a_transponder_change_keeps_the_ax25_baud_in_step(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV")
    rc.transmitter_changed.emit({"description": "X"})
    assert engine.names()[-2:] == ["restart_if_modem_changed", "sync_sdr_baud"]
    assert engine.calls[-1][2] == {"satellite": True}


# ------------------------------------------------------------ live frames


def test_live_frames_are_listed_as_hex_lines(tab: SstvTab, engine: _FakeEngine) -> None:
    tab._mode_combo.setCurrentText("SSDV")
    engine.raw_frame_received.emit(bytes.fromhex("94a662b29caa60"))
    engine.raw_frame_received.emit(bytes.fromhex("76200de4"))
    assert tab._raw_edit.toPlainText().splitlines() == ["94 A6 62 B2 9C AA 60", "76 20 0D E4"]
    assert "Frames: 2" in tab._raw_count_label.text()
    assert "SSDV packets: 0" in tab._raw_count_label.text()


def test_a_live_frame_with_an_ssdv_packet_feeds_the_decoder(
    qtbot: QtBot, tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV")
    pkt = make_packet(size=100)
    engine.raw_frame_received.emit(AX25_HEADER + pkt)
    assert "SSDV packets: 1" in tab._raw_count_label.text()
    qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    assert seen[0] == pkt
    assert tab._image_label.pixmap() is not None and not tab._image_label.pixmap().isNull()


# ----------------------------------------------------------- pasted hex


def test_pasting_hex_builds_the_image_and_shows_the_image_tab(
    tab: SstvTab, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._view_tabs.setCurrentWidget(tab._raw_page)
    packets = [make_packet(size=100, pid=p) for p in range(3)]
    _paste(tab, "\n".join(format_hex_line(AX25_HEADER + p) for p in packets))
    assert seen == [b"".join(packets)]
    assert tab._view_tabs.currentWidget() is tab._image_page
    assert not tab._image_label.pixmap().isNull()
    assert tab._history_list.count() == 1  # committed once
    assert tab._save_btn.isEnabled()


def test_pasting_satnogs_style_text_works(tab: SstvTab, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    pkt = make_packet(size=100)
    text = (
        "data_obs/2026/9/23/6/15046484/data_15046484_2026-09-23T06-47-28\n"
        + format_hex_line(AX25_HEADER + pkt)
        + "\nLoad More Data(10)\n"
    )
    _paste(tab, text)
    assert seen == [pkt]


def test_pasting_frames_without_ssdv_packets_explains_and_stays_on_raw_packets(
    tab: SstvTab, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._view_tabs.setCurrentWidget(tab._raw_page)
    _paste(tab, "94 A6 62 B2 9C AA 60 94 A6 62 B2 A4 AA E1 03 F0 22 FE 64 6A\n76 20 0D E4")
    assert seen == []
    assert "none contains an SSDV packet" in tab._status_label.text()
    assert tab._view_tabs.currentWidget() is tab._raw_page
    assert tab._history_list.count() == 0


def test_pasting_text_without_hex_says_so(tab: SstvTab) -> None:
    _paste(tab, "hello world")
    assert "No hex frames" in tab._status_label.text()


def test_build_button_rebuilds_from_the_whole_text(
    tab: SstvTab, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._raw_edit.setPlainText(format_hex_line(AX25_HEADER + make_packet(size=100)))
    assert seen == []  # typing does not build anything
    tab._decode_hex_btn.click()
    assert len(seen) == 1


def test_a_paste_rebuilds_from_live_and_pasted_lines_together(
    tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV")
    live = make_packet(size=100, pid=0)
    engine.raw_frame_received.emit(AX25_HEADER + live)
    pasted = make_packet(size=100, pid=1)
    _paste(tab, format_hex_line(AX25_HEADER + pasted))
    assert seen[-1] == live + pasted


def test_clear_packets_empties_the_text_and_the_buffer(
    tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV")
    engine.raw_frame_received.emit(AX25_HEADER + make_packet(size=100))
    tab._clear_raw_btn.click()
    assert tab._raw_edit.toPlainText() == ""
    assert tab._ssdv_decoder is not None and tab._ssdv_decoder.packet_count == 0
    assert tab._raw_count_label.text() == ""


def test_leaving_ssdv_mode_commits_the_current_image(
    qtbot: QtBot, tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV")
    engine.raw_frame_received.emit(AX25_HEADER + make_packet(size=100))
    qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    assert tab._history_list.count() == 0  # progressive updates are not history entries
    tab._mode_combo.setCurrentText("SSTV")
    assert tab._history_list.count() == 1


def test_a_paste_becomes_its_own_lines_wherever_the_cursor_is(tab: SstvTab) -> None:
    tab._raw_edit.appendPlainText("AA BB")
    tab._raw_edit.appendPlainText("CC DD")
    cursor = tab._raw_edit.textCursor()
    cursor.setPosition(2)  # in the middle of the first line
    tab._raw_edit.setTextCursor(cursor)
    _paste(tab, "11 22")  # no trailing newline
    assert tab._raw_edit.toPlainText().splitlines() == ["AA", "11 22", " BB", "CC DD"]


def test_a_paste_at_the_end_after_live_lines_keeps_every_frame_intact(tab: SstvTab) -> None:
    tab._raw_edit.appendPlainText("AA BB")
    cursor = tab._raw_edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    tab._raw_edit.setTextCursor(cursor)
    _paste(tab, "11 22\n33 44")
    assert tab._raw_edit.toPlainText().splitlines() == ["AA BB", "11 22", "33 44"]


# ---------------------------------------------------- SSTV mode: reception


class _SignalPipeline(QObject):
    """An SDR pipeline whose audio_ready is a real signal, so audio can be fed through it."""

    audio_ready = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[str] = []
        self.released: list[str] = []

    def request_audio(self, owner: str) -> None:
        self.requested.append(owner)

    def release_audio(self, owner: str) -> None:
        self.released.append(owner)


def _connect_sdr_with(rc: _FakeRadioControl, pipeline: _SignalPipeline) -> None:
    rig = _FakeRig()
    rig._pipeline = pipeline  # type: ignore[assignment]
    rc._rig1 = rig
    rc.sdr_connected.emit()


def test_sstv_decoder_listens_as_soon_as_the_tab_opens(tab: SstvTab) -> None:
    """It used to start only after switching to SSDV and back."""
    assert tab._mode_combo.currentText() == "SSTV"
    assert tab._decoder is not None
    assert tab._decoder._active


def test_sdr_audio_is_decoded_at_the_sdr_audio_rate(tab: SstvTab, rc: _FakeRadioControl) -> None:
    from sdr.demodulator import AUDIO_RATE

    pipeline = _SignalPipeline()
    _connect_sdr_with(rc, pipeline)
    assert tab._decoder is not None
    assert (
        tab._decoder.sample_rate == AUDIO_RATE
    )  # not 44100: 48 kHz audio at 44.1 kHz decodes nothing
    assert pipeline.requested == ["SSTV/SSDV"]


def test_sound_card_audio_is_decoded_at_44100(
    tab: SstvTab, rc: _FakeRadioControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any, int, Any]] = []

    class _Manager:
        def acquire_input(self, owner: str, device: Any, rate: int, callback: Any) -> None:
            calls.append((owner, device, rate, callback))

        def release_input(self, *_a: Any) -> None:
            pass

    monkeypatch.setattr("comms.audio_device_manager.get_audio_device_manager", lambda: _Manager())
    rc.rig_connected.emit()  # no SDR in the rig slots: the sound card
    assert tab._decoder is not None and tab._decoder.sample_rate == 44100
    assert len(calls) == 1 and calls[0][2] == 44100


def test_the_rate_follows_the_input_when_it_changes(
    tab: SstvTab, rc: _FakeRadioControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tab._decoder is not None
    assert tab._decoder.sample_rate == 44100  # nothing connected: sound-card rate
    _connect_sdr_with(rc, _SignalPipeline())
    assert tab._decoder.sample_rate == 48000


def test_an_sstv_picture_arriving_through_the_sdr_audio_ends_up_in_the_history(
    qtbot: QtBot, tab: SstvTab, rc: _FakeRadioControl
) -> None:
    """The whole path: SDR audio_ready -> decoder -> image tab and history."""
    pytest.importorskip("scipy")
    pytest.importorskip("pysstv")
    import numpy as np
    from PIL import Image
    from pysstv.color import Robot36

    pipeline = _SignalPipeline()
    _connect_sdr_with(rc, pipeline)
    image = Image.new("RGB", (320, 240), (40, 80, 200))
    audio = np.array(list(Robot36(image, 48000, 16).gen_samples()), dtype=np.float32) / 32768.0
    audio = np.concatenate([np.zeros(48000, np.float32), audio, np.zeros(24000, np.float32)])
    for i in range(0, len(audio), 4096):
        pipeline.audio_ready.emit(audio[i : i + 4096])
    qtbot.waitUntil(lambda: tab._history_list.count() == 1, timeout=5000)
    assert tab._image_label.pixmap() is not None and not tab._image_label.pixmap().isNull()
    assert tab._save_btn.isEnabled()


def test_disconnecting_the_sdr_stops_the_audio_feed(tab: SstvTab, rc: _FakeRadioControl) -> None:
    pipeline = _SignalPipeline()
    _connect_sdr_with(rc, pipeline)
    rc.sdr_disconnected.emit()
    assert pipeline.released == ["SSTV/SSDV"]
