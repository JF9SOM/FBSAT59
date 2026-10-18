"""Tests for the SSTV / SSDV tab's SSDV mode: Raw Packets / Image sub-tabs, live frames,
pasted hex, and self-started AX.25 reception."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import types
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
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    assert tab._view_tabs.currentWidget() is tab._raw_page
    tab._mode_combo.setCurrentText("SSTV")
    assert tab._view_tabs.currentWidget() is tab._image_page


# ------------------------------------------------------------ satellite combo


def test_satellite_combo_lists_the_satellites_of_each_mode(
    qtbot: QtBot,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
) -> None:
    conn.execute("CREATE TABLE satellites (norad_cat_id INTEGER, name TEXT, is_hidden INTEGER)")
    conn.execute(
        "CREATE TABLE transmitters (norad_cat_id INTEGER, description TEXT, mode TEXT, "
        "baud INTEGER, alive INTEGER)"
    )
    conn.executemany(
        "INSERT INTO satellites VALUES (?, ?, 0)",
        [(25544, "ISS"), (68446, "HADES-SA"), (61781, "ASRTU-1"), (7, "LAPAN"), (1, "OTHER")],
    )
    conn.executemany(
        "INSERT INTO transmitters VALUES (?, ?, 'FM', NULL, 1)",
        [
            (25544, "SSTV Mode U"),
            (7, "AX.25 SSDV 1200"),
            (68446, "SSDV 800 baud"),
            (61781, "SSDV BPSK"),
            (1, "Beacon"),
        ],
    )
    t = SstvTab(conn, rc, aprs_engine=engine)
    qtbot.addWidget(t)

    def listed() -> set[int]:
        return {t._sat_combo.itemData(i) for i in range(t._sat_combo.count())}

    assert listed() == {25544}
    t._mode_combo.setCurrentText("SSDV (AX.25)")
    assert listed() == {7}
    t._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    assert listed() == {61781}
    t._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    assert listed() == {68446}
    with qtbot.waitSignal(t.satellite_selected) as blocker:
        t._mode_combo.setCurrentText("SSTV")
    assert blocker.args == [25544]


# ------------------------------------------------------------------ baud


def test_baud_combo_is_enabled_only_in_ssdv_ax25_mode(tab: SstvTab) -> None:
    assert [tab._baud_combo.itemText(i) for i in range(tab._baud_combo.count())] == [
        "1200",
        "4800",
        "9600",
    ]
    for mode, enabled in (
        ("SSTV", False),
        ("SSDV (AX.25)", True),
        ("SSDV (CCSDS BPSK 9k6)", False),
        ("SSDV (2FSK)", False),
    ):
        if tab._mode_combo.findText(mode) < 0:
            continue
        tab._mode_combo.setCurrentText(mode)
        assert tab._baud_combo.isEnabled() is enabled, mode


def test_choosing_a_baud_persists_it_and_restarts_a_running_session(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine, conn: sqlite3.Connection
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    tab._baud_combo.setCurrentIndex(tab._baud_combo.findData("4800"))
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'ax25_baud_mode'").fetchone()
    assert row["value"] == "4800"
    assert ("restart_if_modem_changed", ("4800",), {}) in engine.calls
    assert any(n == "sync_sdr_baud" and a[1] == "4800" for n, a, _k in engine.calls)


def test_following_a_transmitter_sets_the_baud_combo(tab: SstvTab) -> None:
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    tab._on_transmitter_changed({"description": "Mode U - GMSK 9k6 - AX.25", "norad_cat_id": 1})
    assert tab._baud_combo.currentData() == "9600"


# ------------------------------------------------- self-started reception


def test_ssdv_mode_starts_sdr_reception_without_the_aprs_tab(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
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
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    name, args, kwargs = engine.calls[-1]
    assert name == "start_rig"
    assert args[0] == "sstv"
    assert kwargs == {"modem": "1200"}


def test_ssdv_mode_without_an_input_says_so_and_never_asks_for_the_aprs_tab(
    tab: SstvTab, engine: _FakeEngine
) -> None:
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    assert engine.calls == []
    # The "no audio source" notice lives in the top "Input:" label only.
    assert tab._status_label.text() == ""
    assert "No audio source" in tab._source_label.text()


def test_connecting_an_input_after_entering_ssdv_mode_starts_reception(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    _connect_sdr(rc)
    assert engine.names() == ["start_sdr_direwolf"]


def test_disconnecting_the_input_releases_the_pipeline(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    rc.sdr_disconnected.emit()
    assert engine.names() == ["start_sdr_direwolf", "stop"]
    assert engine.calls[-1][1] == ("sstv",)


def test_leaving_ssdv_mode_releases_the_pipeline_and_stops_listening(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    tab._mode_combo.setCurrentText("SSTV")
    assert engine.names() == ["start_sdr_direwolf", "stop"]
    engine.raw_frame_received.emit(b"\x03")  # the engine no longer feeds this tab
    assert tab._raw_edit.toPlainText() == ""


def test_a_transponder_change_keeps_the_ax25_baud_in_step(
    tab: SstvTab, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    _connect_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    rc.transmitter_changed.emit({"description": "X"})
    assert engine.names()[-2:] == ["restart_if_modem_changed", "sync_sdr_baud"]
    assert engine.calls[-1][2] == {"satellite": True}


# ------------------------------------------------------------ live frames


def test_live_frames_are_listed_as_hex_lines(tab: SstvTab, engine: _FakeEngine) -> None:
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    engine.raw_frame_received.emit(bytes.fromhex("94a662b29caa60"))
    engine.raw_frame_received.emit(bytes.fromhex("76200de4"))
    assert tab._raw_edit.toPlainText().splitlines() == ["94 A6 62 B2 9C AA 60", "76 20 0D E4"]
    assert "Frames: 2" in tab._raw_count_label.text()
    assert "SSDV packets: 0" in tab._raw_count_label.text()


def test_a_live_frame_with_an_ssdv_packet_feeds_the_decoder(
    qtbot: QtBot, tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
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
    assert tab._current_image is not None


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
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    live = make_packet(size=100, pid=0)
    engine.raw_frame_received.emit(AX25_HEADER + live)
    pasted = make_packet(size=100, pid=1)
    _paste(tab, format_hex_line(AX25_HEADER + pasted))
    assert seen[-1] == live + pasted


def test_clear_packets_empties_the_text_and_the_buffer(
    tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    engine.raw_frame_received.emit(AX25_HEADER + make_packet(size=100))
    tab._clear_raw_btn.click()
    assert tab._raw_edit.toPlainText() == ""
    assert tab._ssdv_decoder is not None and tab._ssdv_decoder.packet_count == 0
    assert tab._raw_count_label.text() == ""


def test_leaving_ssdv_mode_commits_the_current_image(
    qtbot: QtBot, tab: SstvTab, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ssdv_binary(monkeypatch)
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
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
    """An SDR pipeline that hands raw I/Q to its subscribers, like SDRPipeline.subscribe()."""

    def __init__(self) -> None:
        super().__init__()
        self._device = types.SimpleNamespace(sample_rate=250_000)
        self.subscribers: list[Any] = []

    def subscribe(self, callback: Any) -> None:
        if callback not in self.subscribers:
            self.subscribers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        self.subscribers = [c for c in self.subscribers if c != callback]

    def emit_iq(self, iq: Any) -> None:
        for callback in list(self.subscribers):
            callback(iq)


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


def test_sdr_audio_is_decoded_at_the_demodulator_rate(tab: SstvTab, rc: _FakeRadioControl) -> None:
    pytest.importorskip("scipy")  # the private FM demodulator needs it (CI has none)
    pipeline = _SignalPipeline()
    _connect_sdr_with(rc, pipeline)
    assert tab._decoder is not None
    # the private demodulator's rate (50 kHz at a 250 kS/s SDR), not 44100: audio read at the
    # wrong rate decodes nothing
    assert tab._decoder.sample_rate == 50_000
    assert len(pipeline.subscribers) == 1  # raw I/Q for the private demodulator
    assert tab._sdr_demod is not None and tab._sdr_demod.output_rate == 50_000


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
    # CI has no PortAudio, so the tab's "is sounddevice available" check needs a stub.
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    rc.rig_connected.emit()  # no SDR in the rig slots: the sound card
    assert tab._decoder is not None and tab._decoder.sample_rate == 44100
    assert len(calls) == 1 and calls[0][2] == 44100


def test_the_rate_follows_the_input_when_it_changes(
    tab: SstvTab, rc: _FakeRadioControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tab._decoder is not None
    assert tab._decoder.sample_rate == 44100  # nothing connected: sound-card rate
    _connect_sdr_with(rc, _SignalPipeline())
    assert tab._decoder.sample_rate == 50_000


def test_an_sstv_picture_arriving_as_sdr_iq_ends_up_in_the_history(
    qtbot: QtBot, tab: SstvTab, rc: _FakeRadioControl
) -> None:
    """The whole path: SDR I/Q -> private FM demodulator -> decoder -> image tab and history."""
    pytest.importorskip("scipy")
    pytest.importorskip("pysstv")
    import numpy as np
    from PIL import Image
    from pysstv.color import Robot36
    from scipy import signal as sp_signal

    pipeline = _SignalPipeline()
    _connect_sdr_with(rc, pipeline)
    image = Image.new("RGB", (320, 240), (40, 80, 200))
    audio = np.array(list(Robot36(image, 48000, 16).gen_samples()), dtype=np.float32) / 32768.0
    audio = np.concatenate([np.zeros(48000, np.float32), audio, np.zeros(24000, np.float32)])
    up = sp_signal.resample_poly(audio, 250_000, 48_000)
    iq = np.exp(2j * np.pi * 3_000.0 * np.cumsum(up) / 250_000).astype(np.complex64)
    demod = tab._sdr_demod
    assert demod is not None
    for i in range(0, len(iq), 16384):
        # the SDR delivers in real time; do not outrun the demodulator thread's bounded queue
        qtbot.waitUntil(lambda: demod._q.qsize() < 32, timeout=10000)
        pipeline.emit_iq(iq[i : i + 16384])
    qtbot.waitUntil(lambda: tab._history_list.count() == 1, timeout=10000)
    assert tab._image_label.pixmap() is not None and not tab._image_label.pixmap().isNull()
    assert tab._current_image is not None


def test_disconnecting_the_sdr_stops_the_audio_feed(tab: SstvTab, rc: _FakeRadioControl) -> None:
    pytest.importorskip("scipy")  # the private FM demodulator needs it (CI has none)
    pipeline = _SignalPipeline()
    _connect_sdr_with(rc, pipeline)
    assert len(pipeline.subscribers) == 1
    rc.sdr_disconnected.emit()
    assert pipeline.subscribers == []
    assert tab._sdr_demod is None


def test_the_recording_buttons_are_named_for_what_they_open(tab: SstvTab) -> None:
    assert tab._decode_file_btn.text() == "📂 Decode MP3 Recording…"
    assert tab._decode_iq_btn.text() == "📂 Decode IQ Recording…"


def test_decode_iq_recording_runs_sdr_controls_open(tab: SstvTab, rc: _FakeRadioControl) -> None:
    """The button is SDR Control's "Open…": file picker, replay, whole-recording spectrogram."""
    calls: list[str] = []

    class _SdrControl:
        def open_recording(self) -> None:
            calls.append("open")

    rc._sdr_control = _SdrControl()  # type: ignore[attr-defined]
    tab._decode_iq_btn.click()
    assert calls == ["open"]


def test_decode_iq_recording_without_sdr_control_tells_the_user(
    tab: SstvTab, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(
        "ui.sstv_tab.QMessageBox.information", lambda _p, title, text: shown.append(text)
    )
    tab._decode_iq_btn.click()  # the fake radio control has no _sdr_control
    assert shown == ["SDR Control is not available."]


def test_open_image_loads_saved_png(
    tab: SstvTab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = QImage(40, 30, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    path = tmp_path / "SSTV_ISS_test.png"
    assert img.save(str(path))
    monkeypatch.setattr("ui.sstv_tab.QFileDialog.getOpenFileName", lambda *a, **k: (str(path), ""))
    before = tab._history_list.count()
    tab._open_image_btn.click()
    assert tab._current_image is not None
    assert tab._current_image.width() == 40
    assert tab._current_image is not None
    assert tab._history_list.count() == before + 1


# ----------------------------------------------- gr-satellites SSDV (ASRTU-1)


class _FakeGrBackend(QObject):
    raw_frame_received = Signal(bytes)
    status_changed = Signal(str)
    instances: list[_FakeGrBackend] = []

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.running = False
        self.kiss_supported = True
        self.start_args: tuple[Any, ...] | None = None
        _FakeGrBackend.instances.append(self)

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self, norad: int, samp_rate: int, pipeline: Any) -> tuple[bool, str]:
        self.start_args = (norad, samp_rate, pipeline)
        self.running = True
        return True, ""

    def stop(self) -> None:
        self.running = False


class _FakeGrPipeline(_FakePipeline):
    class _Device:
        sample_rate = 250_000

    _device = _Device()


@pytest.fixture
def fake_gr(monkeypatch: pytest.MonkeyPatch) -> type[_FakeGrBackend]:
    import comms.telemetry.gr_satellites_backend as backend_mod

    _FakeGrBackend.instances = []
    monkeypatch.setattr(backend_mod, "GrSatellitesBackend", _FakeGrBackend)
    return _FakeGrBackend


def _connect_gr_sdr(rc: _FakeRadioControl) -> _FakeGrPipeline:
    pipeline = _FakeGrPipeline()
    rig = _FakeRig()
    rig._pipeline = pipeline  # type: ignore[assignment]
    rc._rig1 = rig
    rc.sdr_connected.emit()
    return pipeline


def test_mode_combo_offers_the_gr_satellites_mode(tab: SstvTab) -> None:
    assert [tab._mode_combo.itemText(i) for i in range(tab._mode_combo.count())] == [
        "SSTV",
        "SSDV (AX.25)",
        "SSDV (CCSDS BPSK 9k6)",
        "SSDV (2FSK 800bps)",
    ]


def test_gr_mode_without_an_sdr_says_so(tab: SstvTab, fake_gr: type[_FakeGrBackend]) -> None:
    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    assert tab._view_tabs.currentWidget() is tab._raw_page
    assert tab._status_label.text() == ""  # the top "Input:" label carries the notice
    assert not any(b.running for b in fake_gr.instances)


def test_gr_mode_starts_gr_satellites_for_asrtu1(
    tab: SstvTab, rc: _FakeRadioControl, fake_gr: type[_FakeGrBackend], engine: _FakeEngine
) -> None:
    pipeline = _connect_gr_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    backend = fake_gr.instances[0]
    assert backend.start_args == (61781, 250_000, pipeline)
    assert not any(name == "start_sdr_direwolf" for name, _a, _k in engine.calls)


def test_gr_mode_starts_when_the_sdr_connects_later(
    tab: SstvTab, rc: _FakeRadioControl, fake_gr: type[_FakeGrBackend]
) -> None:
    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    _connect_gr_sdr(rc)
    assert fake_gr.instances[0].running


def test_leaving_gr_mode_stops_gr_satellites(
    tab: SstvTab, rc: _FakeRadioControl, fake_gr: type[_FakeGrBackend]
) -> None:
    _connect_gr_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    tab._mode_combo.setCurrentText("SSTV")
    assert not fake_gr.instances[0].running


def test_gr_frames_show_as_hex_and_count_ssdv_packets(
    tab: SstvTab, rc: _FakeRadioControl, fake_gr: type[_FakeGrBackend]
) -> None:
    from tests.test_ssdv import make_ccsds_frame, make_dslwp_packet

    _connect_gr_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    backend = fake_gr.instances[0]
    backend.raw_frame_received.emit(make_ccsds_frame(make_dslwp_packet(pid=0)))
    backend.raw_frame_received.emit(make_ccsds_frame(make_dslwp_packet(pid=1)))
    backend.raw_frame_received.emit(make_ccsds_frame(make_dslwp_packet(), vc=0))  # telemetry
    assert tab._raw_edit.toPlainText().count("\n") >= 2
    assert tab._ssdv_frames == 3
    assert tab._ssdv_packets == 2
    assert tab._gr_ssdv_decoder is not None
    assert tab._gr_ssdv_decoder.packet_count == 2


def test_gr_mode_pasted_hex_uses_the_ccsds_finder(
    tab: SstvTab, fake_gr: type[_FakeGrBackend]
) -> None:
    from tests.test_ssdv import make_ccsds_frame, make_dslwp_packet

    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    frame = make_ccsds_frame(make_dslwp_packet())
    tab._raw_edit.setPlainText(frame.hex(" ").upper())
    tab._on_hex_pasted()
    assert tab._gr_ssdv_decoder is not None
    assert tab._gr_ssdv_decoder.packet_count == 1


# ---------------------------------------------------- 2FSK 800bps SSDV (HADES-SA)


class _FskPipeline(_FakeGrPipeline):
    def __init__(self) -> None:
        self.subscribers: list[Any] = []

    def subscribe(self, cb: Any) -> None:
        self.subscribers.append(cb)

    def unsubscribe(self, cb: Any) -> None:
        self.subscribers.remove(cb)


def _connect_fsk_sdr(rc: _FakeRadioControl) -> _FskPipeline:
    pipeline = _FskPipeline()
    rig = _FakeRig()
    rig._pipeline = pipeline  # type: ignore[assignment]
    rc._rig1 = rig
    rc.sdr_connected.emit()
    return pipeline


def test_fsk_mode_without_an_sdr_says_so(tab: SstvTab) -> None:
    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    assert tab._view_tabs.currentWidget() is tab._raw_page
    assert tab._status_label.text() == ""  # the top "Input:" label carries the notice
    assert tab._fsk_demod is None


def test_fsk_mode_subscribes_to_the_sdr_and_leaving_unsubscribes(
    tab: SstvTab, rc: _FakeRadioControl
) -> None:
    pipeline = _connect_fsk_sdr(rc)
    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    demod = tab._fsk_demod
    assert demod is not None
    assert [cb for cb in pipeline.subscribers if cb.__self__ is demod] != []
    tab._mode_combo.setCurrentText("SSTV")
    assert tab._fsk_demod is None
    assert [cb for cb in pipeline.subscribers if cb.__self__ is demod] == []


def test_fsk_mode_starts_when_the_sdr_connects_later(tab: SstvTab, rc: _FakeRadioControl) -> None:
    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    pipeline = _connect_fsk_sdr(rc)
    assert tab._fsk_demod is not None
    assert [cb for cb in pipeline.subscribers if cb.__self__ is tab._fsk_demod] != []


def test_fsk_packets_show_as_hex_and_reach_the_decoder(tab: SstvTab) -> None:
    from tests.test_fsk_ssdv import hades_packet

    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    tab._on_fsk_packet(hades_packet(0), True)
    tab._on_fsk_packet(hades_packet(1), True)
    assert tab._ssdv_frames == 2
    assert tab._ssdv_packets == 2
    assert tab._ssdv_decoder is not None
    assert tab._ssdv_decoder.packet_count == 2
    assert tab._raw_edit.toPlainText().count("\n") >= 1


def test_fsk_bit_errored_copy_is_repaired_and_never_replaces_a_valid_packet(
    tab: SstvTab,
) -> None:
    from tests.test_fsk_ssdv import hades_packet

    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    good = hades_packet(4)
    bad = bytearray(good)
    bad[50] ^= 0xFF
    tab._on_fsk_packet(good, True)
    tab._on_fsk_packet(bytes(bad), False)  # Reed-Solomon repairs it back to the good packet
    assert tab._ssdv_decoder is not None
    assert list(tab._ssdv_decoder._packets.values()) == [good]
    # too many errors to repair: dropped, never handed to the decoder
    wrecked = bytes(good[:20]) + bytes(b ^ 0xFF for b in good[20:120]) + bytes(good[120:])
    tab._on_fsk_packet(wrecked, False)
    assert tab._ssdv_decoder.packet_count == 1


# ------------------------------------------- automatic mode from the satellite


def test_hades_sa_selects_the_2fsk_mode(tab: SstvTab, rc: _FakeRadioControl) -> None:
    rc.transmitter_changed.emit({"description": "FSK 800 bps", "norad_cat_id": 68446})
    assert tab._mode_combo.currentText() == "SSDV (2FSK 800bps)"
    assert tab._view_tabs.currentWidget() is tab._raw_page


def test_asrtu1_selects_the_gr_satellites_mode(
    tab: SstvTab, rc: _FakeRadioControl, fake_gr: type[_FakeGrBackend]
) -> None:
    rc.transmitter_changed.emit({"description": "Mode U - SSDV", "norad_cat_id": 61781})
    assert tab._mode_combo.currentText() == "SSDV (CCSDS BPSK 9k6)"


def test_other_satellites_leave_the_mode_alone(tab: SstvTab, rc: _FakeRadioControl) -> None:
    tab._mode_combo.setCurrentText("SSDV (AX.25)")
    rc.transmitter_changed.emit({"description": "Mode U", "norad_cat_id": 25544})
    assert tab._mode_combo.currentText() == "SSDV (AX.25)"
    rc.transmitter_changed.emit(None)
    assert tab._mode_combo.currentText() == "SSDV (AX.25)"


# ------------------------------------------- packets kept across passes (all modes)


def _second_tab(
    qtbot: QtBot, conn: sqlite3.Connection, rc: _FakeRadioControl, engine: _FakeEngine
) -> SstvTab:
    t = SstvTab(conn, rc, aprs_engine=engine)
    qtbot.addWidget(t)
    return t


def test_ax25_packets_of_two_passes_build_one_picture(
    qtbot: QtBot,
    tab: SstvTab,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
) -> None:
    from tests.test_fsk_ssdv import hades_packet

    tab._ensure_ssdv_decoder()
    tab._on_ax25_frame(AX25_HEADER + hades_packet(0))
    tab._on_ax25_frame(AX25_HEADER + hades_packet(1))
    assert tab._store is not None and tab._store.groups()[0].packets == 2
    # the next pass (a fresh tab: the app was restarted) adds packets 2 and 3
    tab2 = _second_tab(qtbot, conn, rc, engine)
    tab2._ensure_ssdv_decoder()
    tab2._on_ax25_frame(AX25_HEADER + hades_packet(2))
    tab2._on_ax25_frame(AX25_HEADER + hades_packet(3))
    assert tab2._store is not None and len(tab2._store.groups()) == 1
    assert tab2._store.groups()[0].packets == 4
    assert tab2._ssdv_decoder is not None and tab2._ssdv_decoder.packet_count == 4


def test_ccsds_dslwp_packets_are_kept_too(
    qtbot: QtBot,
    tab: SstvTab,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
    fake_gr: type[_FakeGrBackend],
) -> None:
    from tests.test_ssdv import make_ccsds_frame, make_dslwp_packet

    tab._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    tab._on_gr_frame(make_ccsds_frame(make_dslwp_packet(pid=0)))
    tab._on_gr_frame(make_ccsds_frame(make_dslwp_packet(pid=1)))
    assert tab._store is not None
    info = tab._store.groups()[0]
    assert info.kind == "dslwp" and info.norad == 61781 and info.packets == 2
    tab2 = _second_tab(qtbot, conn, rc, engine)
    tab2._mode_combo.setCurrentText("SSDV (CCSDS BPSK 9k6)")
    tab2._on_gr_frame(make_ccsds_frame(make_dslwp_packet(pid=2)))
    assert tab2._gr_ssdv_decoder is not None and tab2._gr_ssdv_decoder.packet_count == 3


def test_2fsk_packets_are_stored_for_hades_sa(tab: SstvTab) -> None:
    from tests.test_fsk_ssdv import hades_packet

    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    tab._on_fsk_packet(hades_packet(0), True)
    tab._on_fsk_packet(hades_packet(1), True)
    assert tab._store is not None
    info = tab._store.groups()[0]
    assert info.norad == 68446 and info.kind == "std" and info.packets == 2


def test_ax25_packets_are_stored_under_the_selected_satellite(
    tab: SstvTab, rc: _FakeRadioControl
) -> None:
    from tests.test_fsk_ssdv import hades_packet

    rc.transmitter_changed.emit({"description": "Mode U", "norad_cat_id": 98207})
    tab._ensure_ssdv_decoder()
    tab._on_ax25_frame(AX25_HEADER + hades_packet(0))
    assert tab._store is not None and tab._store.groups()[0].norad == 98207


def test_building_a_stored_picture_loads_all_its_packets(tab: SstvTab) -> None:
    from tests.test_fsk_ssdv import hades_packet

    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    for n in range(3):
        tab._on_fsk_packet(hades_packet(n), True)
    assert tab._store is not None and tab._ssdv_decoder is not None
    gid = tab._store.groups()[0].id
    tab._ssdv_decoder.reset()
    tab._on_build_stored(gid)
    assert tab._ssdv_decoder.packet_count == 3


def test_stored_images_dialog_lists_pictures(qtbot: QtBot, tab: SstvTab) -> None:
    from tests.test_fsk_ssdv import hades_packet
    from ui.ssdv_store_dialog import SsdvStoreDialog

    tab._mode_combo.setCurrentText("SSDV (2FSK 800bps)")
    tab._on_fsk_packet(hades_packet(0, image=32), True)
    tab._on_fsk_packet(hades_packet(1, image=32), True)
    assert tab._store is not None
    dialog = SsdvStoreDialog(tab._store, lambda n: f"sat{n}")
    qtbot.addWidget(dialog)
    assert dialog._table.rowCount() == 1
    cells = [dialog._table.item(0, c).text() for c in range(5)]  # type: ignore[union-attr]
    assert cells[0] == "sat68446" and cells[2] == "32" and cells[4] == "2"
    got: list[int] = []
    dialog.build_requested.connect(got.append)
    dialog._table.selectRow(0)
    dialog._on_build()
    assert got == [tab._store.groups()[0].id]


# ------------------------------------------------- bottom bar and CSV import


def _bottom_buttons(tab: SstvTab) -> list[str]:
    """Texts of the bottom-row buttons, left to right."""
    from PySide6.QtWidgets import QPushButton

    row = [b for b in tab.findChildren(QPushButton) if b.maximumHeight() == 30]
    return [b.text() for b in sorted(row, key=lambda b: b.geometry().x())]


def test_bottom_row_has_no_status_text_or_save_png_and_csv_sits_before_clear(
    tab: SstvTab,
) -> None:
    from PySide6.QtWidgets import QLabel, QPushButton

    tab.resize(1100, 600)
    tab.show()
    texts = _bottom_buttons(tab)
    assert not any("Save PNG" in t for t in texts)
    assert not hasattr(tab, "_save_btn")
    iq = next(i for i, t in enumerate(texts) if "IQ Recording" in t)
    csv_i = texts.index("📥 Import CSV…")
    clear = next(i for i, t in enumerate(texts) if "Clear" in t)
    assert iq + 1 == csv_i and csv_i + 1 == clear
    # the status line exists for the code but is not shown
    assert not tab._status_label.isVisible()
    visible = [w for w in tab.findChildren(QLabel) if w is tab._status_label and w.isVisible()]
    assert visible == []
    # the buttons are packed at the left edge
    buttons = [b for b in tab.findChildren(QPushButton) if b.maximumHeight() == 30]
    assert min(b.geometry().x() for b in buttons) < 20


def test_warnings_reach_the_main_window_status_bar(
    qtbot: QtBot, conn: sqlite3.Connection, rc: _FakeRadioControl, engine: _FakeEngine
) -> None:
    from PySide6.QtWidgets import QMainWindow

    window = QMainWindow()
    qtbot.addWidget(window)
    t = SstvTab(conn, rc, aprs_engine=engine, parent=window)
    window.setCentralWidget(t)
    qtbot.wait(50)  # let the tab's start-up timers run
    t._status_label.setText("fine")
    assert window.statusBar().currentMessage() == ""
    t._status_label.setText("⚠ something broke")
    assert window.statusBar().currentMessage() == "⚠ something broke"


def _csv_lines() -> str:
    from tests.test_fsk_ssdv import hades_packet

    lines = []
    # newest first, like SatNOGS DB; one frame is damaged beyond repair, one is not SSDV
    for n, ts in (
        (2, "2026-10-17 10:00:02"),
        (1, "2026-10-17 10:00:01"),
        (0, "2026-10-17 10:00:00"),
    ):
        lines.append(f"{ts}|{hades_packet(n)[5:].hex().upper()}||KM7DOS-CN87xi")
    bad = bytearray(hades_packet(3)[5:])
    for i in range(20, 120):
        bad[i] ^= 0xFF
    lines.append(f"2026-10-17 10:00:03|{bytes(bad).hex().upper()}||KM7DOS-CN87xi")
    lines.append("2026-10-17 10:00:04|B316F17A71B55CCD||KM7DOS-CN87xi")  # a CODEC2 frame
    lines.append("not a frame line")
    return "\n".join(lines) + "\n"


def test_csv_extraction_finds_valid_packets_oldest_first(tmp_path: Path) -> None:
    from comms.sstv.csv_import import extract_from_csv

    path = tmp_path / "frames.csv"
    path.write_text(_csv_lines())
    result = extract_from_csv(path)
    assert result.frames == 5 and result.invalid == 1
    assert [p.packet[8] for p in result.packets] == [0, 1, 2]
    assert all(p.norad == 68446 and p.kind == "std" for p in result.packets)
    assert result.packets[0].when < result.packets[-1].when


def test_csv_import_adds_pictures_to_the_store(
    tab: SstvTab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from comms.sstv.csv_import import extract_from_csv

    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2]) or 0)
    )
    monkeypatch.setattr(SstvTab, "_on_stored_images", lambda self: shown.append("dialog"))
    path = tmp_path / "frames.csv"
    path.write_text(_csv_lines())
    tab._on_csv_extracted(extract_from_csv(path))
    assert tab._store is not None
    info = tab._store.groups()[0]
    assert info.norad == 68446 and info.packets == 3
    assert "3 valid SSDV packets (3 new)" in shown[0] and shown[-1] == "dialog"
    # importing the same file again adds nothing new
    shown.clear()
    tab._on_csv_extracted(extract_from_csv(path))
    assert "(0 new)" in shown[0] and tab._store.groups()[0].packets == 3


def test_csv_without_ssdv_says_so(tab: SstvTab, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from comms.sstv.csv_import import CsvExtract

    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2]) or 0)
    )
    tab._on_csv_extracted(CsvExtract(10, 10, [], 0))
    assert "No SSDV packets" in shown[0]


# ------------------------------------------------- saving pictures (auto and by hand)


def test_safe_name_replaces_characters_a_file_name_cannot_hold() -> None:
    name = "Mode V/U - 200 bps UP, 800 bps DOWN - TLM, SSDV, Codec 2, BBS"
    assert "/" not in SstvTab._safe_name(name)
    assert SstvTab._safe_name("a:b*c?d") == "a-b-c-d"
    assert SstvTab._safe_name("日本語/テスト") == "日本語-テスト"
    assert SstvTab._safe_name("  ..x.. ") == "x"
    assert len(SstvTab._safe_name("x" * 300)) == 80


def _real_save_tab(
    qtbot: QtBot,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SstvTab:
    """A tab that really writes pictures (the shared fixture stubs the auto-save out)."""
    monkeypatch.setattr(SstvTab, "_image_save_dir", staticmethod(lambda mode="SSTV": folder / mode))
    t = SstvTab(conn, rc, aprs_engine=engine)
    qtbot.addWidget(t)
    return t


def test_auto_save_works_when_the_transmitter_name_has_a_slash(
    qtbot: QtBot,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = _real_save_tab(qtbot, conn, rc, engine, tmp_path, monkeypatch)
    rc.transmitter_changed.emit(
        {"description": "Mode V/U - 200 bps UP, TLM, SSDV", "norad_cat_id": 68446}
    )
    img = QImage(20, 20, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    t._record_completed_image(img, "SSDV")
    saved = list((tmp_path / "SSDV").glob("*.png"))
    assert len(saved) == 1 and "/" not in saved[0].name and "Mode V-U" in saved[0].name


def test_a_failed_save_is_reported(
    qtbot: QtBot,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = _real_save_tab(qtbot, conn, rc, engine, tmp_path, monkeypatch)
    img = QImage(10, 10, QImage.Format.Format_RGB32)
    assert not t._save_image_to(img, str(tmp_path / "no_such_folder" / "x.png"))
    assert t._status_label.text().startswith("⚠ Could not save the image")


def test_stored_picture_is_saved_under_its_satellite_and_image_id(
    qtbot: QtBot,
    conn: sqlite3.Connection,
    rc: _FakeRadioControl,
    engine: _FakeEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = _real_save_tab(qtbot, conn, rc, engine, tmp_path, monkeypatch)
    t._ssdv_pending = QImage(10, 10, QImage.Format.Format_RGB32)
    t._ssdv_pending.fill(QColor("blue"))
    t._commit_pending_ssdv("HADES-SA (68446) image 48")
    names = [p.name for p in (tmp_path / "SSDV").glob("*.png")]
    assert len(names) == 1 and names[0].startswith("SSDV_HADES-SA (68446) image 48_")


def test_right_click_menu_saves_a_picture_by_hand(tab: SstvTab, tmp_path: Path) -> None:
    img = QImage(30, 20, QImage.Format.Format_RGB32)
    img.fill(QColor("green"))
    menu = tab._image_menu(img)
    actions = menu.actions()
    assert len(actions) == 1 and actions[0].text() == "💾 Save Image As…"
    assert actions[0].isEnabled()
    assert not tab._image_menu(None).actions()[0].isEnabled()
    target = tmp_path / "manual.png"
    assert tab._save_image_to(img, str(target)) and QImage(str(target)).width() == 30
