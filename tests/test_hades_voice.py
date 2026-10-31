"""Tests for HADES-SA CODEC2 voice reception (comms.voice, ui.digital_voice_tab)."""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from comms.voice import codec2
from comms.voice.hades_voice import (
    MASK,
    VoiceMessage,
    VoiceMessageAssembler,
    codec2_frames,
    parse_body,
    unmask,
)
from ui.digital_voice_tab import DigitalVoiceTab

FS = 250_000
BAUD = 800


def make_body(number: int, data: bytes | None = None) -> bytes:
    """The 38 bytes after the sync word of a voice packet (data is XOR-masked)."""
    plain = data if data is not None else bytes((number * 7 + i * 3) & 0xFF for i in range(35))
    return bytes([37, 0xB3, number]) + unmask(plain)


def test_mask_is_its_own_inverse() -> None:
    data = bytes(range(35))
    assert unmask(unmask(data)) == data
    assert len(MASK) == 35


def test_codec2_frames_are_28_bits_padded_to_4_bytes() -> None:
    frames = codec2_frames(bytes([0xFF] * 35))
    assert len(frames) == 10
    assert all(f == bytes([0xFF, 0xFF, 0xFF, 0xF0]) for f in frames)
    first = codec2_frames(bytes([0xAB, 0xCD, 0xEF, 0x12]) + bytes(31))
    assert first[0] == bytes([0xAB, 0xCD, 0xEF, 0x10])


def test_parse_body_accepts_only_voice_packets() -> None:
    body = make_body(5)
    packet = parse_body(body)
    assert packet is not None and packet.number == 5
    assert packet.data == bytes((5 * 7 + i * 3) & 0xFF for i in range(35))
    assert parse_body(bytes([37, 0xA3]) + body[2:]) is None  # SSDV type
    assert parse_body(bytes([36]) + body[1:]) is None  # wrong size
    assert parse_body(body[:20]) is None
    assert parse_body(bytes([37, 0xB3, 250]) + body[3:]) is None  # absurd number


def test_assembler_groups_packets_and_splits_messages() -> None:
    asm = VoiceMessageAssembler()
    for n in (0, 1, 3):
        msg, finished = asm.add(n, bytes(35), n * 0.4)
        assert finished is None
    assert msg.packet_count == 3 and msg.missing == [2]
    # the next slot, 10 s later, is a new message
    msg2, finished = asm.add(0, bytes(35), 14.0)
    assert finished is msg and finished.complete
    assert msg2 is not msg and msg2.packet_count == 1


def test_assembler_repairs_a_corrupted_packet_number_from_the_timing() -> None:
    asm = VoiceMessageAssembler()
    for n in range(8):
        number = 40 if n == 5 else n  # no CRC: one number has a bit error
        msg, _f = asm.add(number, bytes([n] * 35), 10.0 + n * 0.4)
    assert sorted(msg.packets) == list(range(8))
    assert msg.packets[5] == bytes([5] * 35)


def test_assembler_numbers_follow_the_arrival_times_across_losses() -> None:
    asm = VoiceMessageAssembler()
    for n in (4, 5, 8, 9):
        msg, _f = asm.add(n, bytes(35), 100.0 + n * 0.4)
    assert msg.missing == [0, 1, 2, 3, 6, 7]


def test_assembler_idle_closes_the_message() -> None:
    now = [0.0]
    asm = VoiceMessageAssembler(clock=lambda: now[0])
    msg, _f = asm.add(0, bytes(35))
    now[0] += 10.0
    assert asm.close_if_idle() is msg and msg.complete
    assert asm.close_if_idle() is None


def test_message_length_and_duration() -> None:
    m = VoiceMessage(0.0)
    m.packets = {2: bytes(35), 5: bytes(35)}
    assert m.length == 6
    assert m.missing == [0, 1, 3, 4]
    assert m.duration_s == pytest.approx(2.4)


@pytest.mark.skipif(not codec2.is_available(), reason="libcodec2 not installed")
def test_message_decodes_with_silence_for_missing_packets() -> None:
    m = VoiceMessage(0.0)
    m.packets = {1: bytes(35), 3: bytes(35)}
    pcm = m.to_pcm()
    assert pcm.dtype == np.int16
    assert len(pcm) == 4 * 3200
    assert not pcm[:3200].any() and not pcm[6400:9600].any()


def _fsk(
    bodies: list[bytes], snr_db: float = 20.0, baud: int = BAUD, shift: float = 1600.0
) -> np.ndarray:
    rng = np.random.default_rng(2)
    bits: list[int] = [1, 0] * 400
    for body in bodies:
        raw = b"\x55" * 4 + b"\xbf\x35" + body + b"\x55" * 2
        bits += [(b >> (7 - i)) & 1 for b in raw for i in range(8)]
    sym = np.array(bits)
    n = int(len(sym) * FS / baud)
    idx = (np.arange(n) * baud // FS).clip(max=len(sym) - 1)
    freq = np.where(sym[idx] == 1, -shift / 2, shift / 2) + 150.0
    sig = np.exp(2j * np.pi * np.cumsum(freq) / FS)
    sigma = 10 ** (-snr_db / 20) / np.sqrt(2)
    noise = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return (sig + noise).astype(np.complex64)


def test_stream_decodes_voice_packets_from_iq() -> None:
    pytest.importorskip("scipy")
    from comms.voice.hades_voice import FskVoiceStream

    bodies = [make_body(n) for n in range(6)]
    iq = np.concatenate([_fsk(bodies), np.zeros(8 * FS, dtype=np.complex64)])
    stream = FskVoiceStream(FS)
    got = []
    for a in range(0, len(iq), FS // 4):
        got.extend(stream.feed(iq[a : a + FS // 4]))
    assert {p.number for p in got} == set(range(6))
    for p in got:
        assert p.data == bytes((p.number * 7 + i * 3) & 0xFF for i in range(35))


def test_stream_decodes_the_200_bps_downlink() -> None:
    pytest.importorskip("scipy")
    from comms.voice.hades_voice import SLOW_PACKET_SECONDS, FskVoiceStream

    bodies = [make_body(n) for n in range(5)]
    iq = np.concatenate(
        [_fsk(bodies, baud=200, shift=1170.0), np.zeros(10 * FS, dtype=np.complex64)]
    )
    stream = FskVoiceStream(FS)
    got = []
    for a in range(0, len(iq), FS // 4):
        got.extend(stream.feed(iq[a : a + FS // 4]))
    assert {p.number for p in got} == set(range(5))
    assert all(p.interval_s == SLOW_PACKET_SECONDS for p in got)


def test_assembler_numbers_packets_of_the_slow_downlink_by_their_spacing() -> None:
    asm = VoiceMessageAssembler()
    for n in (0, 1, 2, 4):
        number = 33 if n == 2 else n  # one number has a bit error
        msg, finished = asm.add(number, bytes([n] * 35), 5.0 + n * 1.6, interval_s=1.6)
        assert finished is None
    assert sorted(msg.packets) == [0, 1, 2, 4]
    assert msg.packets[2] == bytes([2] * 35)


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


class _Pipeline:
    class _Device:
        sample_rate = 250_000

    _device = _Device()

    def __init__(self) -> None:
        self.subscribers: list[Any] = []

    def subscribe(self, cb: Any) -> None:
        self.subscribers.append(cb)

    def unsubscribe(self, cb: Any) -> None:
        self.subscribers.remove(cb)


class _Rig:
    is_sdr = True

    def __init__(self, pipeline: _Pipeline) -> None:
        self._pipeline = pipeline


@pytest.fixture
def rc(qtbot: QtBot) -> _FakeRadioControl:
    w = _FakeRadioControl()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def tab(qtbot: QtBot, rc: _FakeRadioControl) -> DigitalVoiceTab:
    t = DigitalVoiceTab(sqlite3.connect(":memory:"), rc)
    t._auto_save_cb.setChecked(False)
    qtbot.addWidget(t)
    return t


def test_tab_without_an_sdr_says_so(tab: DigitalVoiceTab) -> None:
    assert "SDR" in tab._source_label.text()
    assert tab._demod is None


def test_tab_subscribes_to_the_sdr_and_unsubscribes(
    tab: DigitalVoiceTab, rc: _FakeRadioControl
) -> None:
    pipeline = _Pipeline()
    rc._rig1 = _Rig(pipeline)
    rc.sdr_connected.emit()
    demod = tab._demod
    assert demod is not None and len(pipeline.subscribers) == 1
    rc.sdr_disconnected.emit()
    assert tab._demod is None and pipeline.subscribers == []


def test_tab_lists_messages_as_packets_arrive(tab: DigitalVoiceTab) -> None:
    tab._on_packet(0, bytes(35), 0.0)
    tab._on_packet(1, bytes(35), 0.4)
    tab._on_packet(3, bytes(35), 1.2)
    assert tab._list.count() == 1
    text = tab._list.item(0).text()
    assert "3/4 packets" in text and "1.6 s" in text
    tab._on_packet(0, bytes(35), 20.0)  # the next slot: a second message
    assert tab._list.count() == 2
    assert tab._save_btn.isEnabled()


def test_idle_tick_completes_the_message(tab: DigitalVoiceTab) -> None:
    tab._on_packet(0, bytes(35), 0.0)
    tab._assembler.current.last_at -= 10.0  # type: ignore[union-attr]
    tab._on_idle_tick()
    assert "receiving" not in tab._list.item(0).text()


def test_tab_has_a_mode_box_with_the_2fsk_mode(tab: DigitalVoiceTab) -> None:
    assert [tab._mode_combo.itemText(i) for i in range(tab._mode_combo.count())] == [
        "2FSK 800/200bps"
    ]


def test_tab_shows_raw_packets_as_sent(tab: DigitalVoiceTab) -> None:
    tab._on_packet(7, bytes(35), 2.8)
    line = tab._raw_edit.toPlainText()
    assert "#07" in line and "25 B3 07" in line
    assert tab._view_tabs.tabText(0) == "Raw Packets"
    assert tab._view_tabs.tabText(1) == "Speech-to-Text"
    assert "1" in tab._raw_count_label.text()


def test_recognition_result_follows_the_selected_message(tab: DigitalVoiceTab) -> None:
    tab._on_packet(0, bytes(35), 0.0)
    item = tab._list.item(0)
    item.recognized = ("es", "En un lugar de la Mancha")  # type: ignore[union-attr]
    tab._on_selection_changed()
    assert tab._text_edit.toPlainText() == "En un lugar de la Mancha"
    assert "es" in tab._language_label.text()


def test_voice_matcher_picks_the_hades_sa_800_bps_downlink() -> None:
    from comms.mode_detection import COMMS_TAB_CONFIG, is_digital_voice_transmitter

    hades = {"norad_cat_id": 68446, "mode": "GENESIS FSK", "baud": 800}
    assert is_digital_voice_transmitter(hades)
    assert is_digital_voice_transmitter({**hades, "baud": 200})
    assert not is_digital_voice_transmitter({**hades, "baud": 1200})
    assert not is_digital_voice_transmitter({**hades, "norad_cat_id": 25544})
    assert not is_digital_voice_transmitter({**hades, "mode": "SSDV"})
    assert COMMS_TAB_CONFIG["voice"].show_input_source


def test_whisper_audio_is_resampled_to_16k_float() -> None:
    from comms.voice.recognize import to_whisper_audio

    out = to_whisper_audio(np.array([0, 16384, -16384, 32767], dtype=np.int16))
    assert out.dtype == np.float32 and len(out) == 8
    assert out[2] == pytest.approx(0.5)


# ---- CRC-checked packets from another tab (UNNE-1B voice via the Telemetry tab) ----------- #


def test_trusted_message_keeps_its_numbers_at_any_spacing() -> None:
    """UNNE-1B sends a packet every 1.5 s, not every 0.4 s: no timing repair for them."""
    a = VoiceMessageAssembler()
    for n in range(6):
        a.add(n, bytes([n]) * 35, 1.5 * n, trusted=True)
    assert a.current is not None
    assert sorted(a.current.packets) == [0, 1, 2, 3, 4, 5]
    assert a.current.packets[3] == bytes([3]) * 35
    # the same packets without trust are renumbered from the timing (0.4 s per packet)
    b = VoiceMessageAssembler()
    for n in range(6):
        b.add(n, bytes([n]) * 35, 1.5 * n)
    assert b.current is not None
    assert sorted(b.current.packets) != [0, 1, 2, 3, 4, 5]


def test_trusted_message_fills_gaps_from_the_next_cycle() -> None:
    """The satellite repeats the 37 packets; the first copy of each number is kept."""
    a = VoiceMessageAssembler()
    a.add(0, b"A" * 35, 0.0, trusted=True)
    a.add(2, b"C" * 35, 3.0, trusted=True)  # packet 1 lost
    a.add(1, b"B" * 35, 4.5, trusted=True)  # ...and heard in the next cycle
    a.add(2, b"X" * 35, 6.0, trusted=True)  # a repeat does not replace the first copy
    m = a.current
    assert m is not None
    assert m.packets == {0: b"A" * 35, 1: b"B" * 35, 2: b"C" * 35}
    assert m.missing == []


def test_trusted_and_untrusted_packets_never_share_a_message() -> None:
    a = VoiceMessageAssembler()
    a.add(0, bytes(35), 0.0)
    _msg, finished = a.add(0, bytes(35), 1.0, trusted=True)
    assert finished is not None and not finished.trusted
    assert a.current is not None and a.current.trusted


def test_tab_takes_a_trusted_packet_from_outside(tab: DigitalVoiceTab) -> None:
    for n in (0, 1, 2, 4):
        tab.add_external_packet(n, bytes(35), 1.5 * n)
    assert tab._list.count() == 1
    text = tab._list.item(0).text()
    assert "4/5 packets" in text  # numbers 0..4, one missing
    assert " fc " in tab._raw_edit.toPlainText().lower()  # logged with UNNE-1's type/address


def test_main_window_opens_the_voice_tab_behind_and_forwards_packets(
    qtbot: QtBot, rc: _FakeRadioControl
) -> None:
    """MainWindow's slots for the Telemetry tab's voice signals (host stubbed, no full window)."""
    from PySide6.QtWidgets import QTabWidget

    from ui.main_window import MainWindow

    tabs = QTabWidget()
    qtbot.addWidget(tabs)
    tabs.addTab(QWidget(), "Telemetry")
    host: Any = QWidget()  # the tab's parent must be a real widget
    qtbot.addWidget(host)
    host._tab_widget = tabs
    host._conn = sqlite3.connect(":memory:")
    host._radio_control = rc
    host._comms_tab_keys = {}
    host._add_tab_close_button = lambda tab: None
    host._notify_comms_tab_of_rig_state = lambda tab: None
    host._find_comms_tab = lambda key: MainWindow._find_comms_tab(host, key)  # type: ignore[arg-type]
    host._open_digital_voice_tab = lambda activate: MainWindow._open_digital_voice_tab(  # type: ignore[arg-type]
        host, activate
    )
    MainWindow._on_telemetry_voice_packet(host, 0, bytes(35), 0.0)  # type: ignore[arg-type]
    assert tabs.count() == 1  # no tab yet: the packet is dropped, nothing opens by itself
    MainWindow._on_telemetry_voice_tab_requested(host)  # type: ignore[arg-type]
    assert tabs.count() == 2 and tabs.currentIndex() == 0  # opened behind the current tab
    voice = tabs.widget(1)
    assert isinstance(voice, DigitalVoiceTab)
    voice._auto_save_cb.setChecked(False)
    MainWindow._on_telemetry_voice_tab_requested(host)  # type: ignore[arg-type]
    assert tabs.count() == 2  # asking again does not open a second one
    MainWindow._on_telemetry_voice_packet(host, 3, bytes(35), 4.5)  # type: ignore[arg-type]
    assert voice._list.count() == 1 and "1/4 packets" in voice._list.item(0).text()
    MainWindow._on_open_digital_voice(host)  # type: ignore[arg-type]
    assert tabs.currentIndex() == 1  # the menu entry still brings it to the front
