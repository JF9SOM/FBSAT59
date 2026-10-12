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
    now = [0.0]
    asm = VoiceMessageAssembler(clock=lambda: now[0])
    for n in (0, 1, 3):
        now[0] += 0.4
        msg, finished = asm.add(n, bytes(35))
        assert finished is None
    assert msg.packet_count == 3 and msg.missing == [2]
    now[0] += 0.4
    msg2, finished = asm.add(0, bytes(35))  # numbering restarts
    assert finished is msg and finished.complete
    assert msg2 is not msg
    now[0] += 10.0
    assert asm.close_if_idle() is msg2 and msg2.complete
    assert asm.close_if_idle() is None


def test_assembler_gap_starts_a_new_message() -> None:
    now = [0.0]
    asm = VoiceMessageAssembler(clock=lambda: now[0])
    first, _f = asm.add(4, bytes(35))
    now[0] += 5.0
    second, finished = asm.add(5, bytes(35))
    assert finished is first and second is not first


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


def _fsk(bodies: list[bytes], snr_db: float = 20.0) -> np.ndarray:
    rng = np.random.default_rng(2)
    bits: list[int] = [1, 0] * 400
    for body in bodies:
        raw = b"\x55" * 4 + b"\xbf\x35" + body + b"\x55" * 2
        bits += [(b >> (7 - i)) & 1 for b in raw for i in range(8)]
    sym = np.array(bits)
    n = int(len(sym) * FS / BAUD)
    idx = (np.arange(n) * BAUD // FS).clip(max=len(sym) - 1)
    freq = np.where(sym[idx] == 1, -650.0, 950.0) + 150.0
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
    tab._on_packet(0, bytes(35))
    tab._on_packet(1, bytes(35))
    tab._on_packet(3, bytes(35))
    assert tab._list.count() == 1
    text = tab._list.item(0).text()
    assert "3/4 packets" in text and "1.6 s" in text
    tab._on_packet(0, bytes(35))  # numbering restarts: a second message
    assert tab._list.count() == 2
    assert tab._save_btn.isEnabled()


def test_idle_tick_completes_the_message(tab: DigitalVoiceTab) -> None:
    tab._on_packet(0, bytes(35))
    tab._assembler.current.last_at -= 10.0  # type: ignore[union-attr]
    tab._on_idle_tick()
    assert "receiving" not in tab._list.item(0).text()
