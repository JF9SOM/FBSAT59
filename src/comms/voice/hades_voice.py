"""HADES-SA CODEC2 voice messages from the raw 2FSK downlink (800 baud).

Format (AMSAT-EA "HADES-SA SpinnyONE Transmissions Description" V1.0, plus the vendor's own
Soundmodem for HADES-SA, which the spec does not fully describe):

* Type 11 packet: sync ``BF 35``, size ``0x25`` (37), type/address ``0xB?`` (type 11, address
  3 for HADES-SA), frame number (1 byte, 0, 1, 2, ... within one message), then 35 data bytes
  = 280 bits = ten CODEC2 700C frames of 28 bits. There is no CRC.
* The data bytes are XOR-masked with a fixed 35-byte pattern (``MASK``; read from the
  Soundmodem's ``hadessa.dll``). After un-masking, the bits are split into 28-bit frames and
  each is padded with four zero bits to 4 bytes -- the form ``c2dec 700C`` / libcodec2 take.
* A message is up to about 15 s (37 packets of 0.4 s); the satellite repeats stored messages
  in its 20 s voice slots.

The 2FSK front end is shared with the SSDV mode (comms.sstv.fsk_ssdv).
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import numpy as np
from PySide6.QtCore import QThread, Signal

from comms.sstv import fsk_ssdv as fsk

MASK = bytes.fromhex("ed15d53b3470e0fded8390dbaa2e25d65e814186bd67795d70a113ce500c19cafb440d")
DATA_BYTES = 35
FRAMES_PER_PACKET = 10
FRAME_BITS = 28
PACKET_SECONDS = 0.4
VOICE_SIZE = 37
VOICE_TYPE = 11
_BODY_BYTES = 38  # size, type/address, number, 35 data bytes
_NEED_BITS = 16 + 8 * _BODY_BYTES
_MAX_SYNC_ERRORS = 1
_MAX_NUMBER = 200  # numbers beyond this are noise (a message is about 40 packets)
# Packets 0.4 s apart: a silence longer than this ends a message.
MESSAGE_GAP_S = 4.0


class VoicePacket(NamedTuple):
    """One type-11 packet: its number in the message, its un-masked 35 data bytes and the
    stream time (seconds from the first sample fed) at which it started."""

    number: int
    data: bytes
    t: float = 0.0


def unmask(data: bytes) -> bytes:
    """XOR the fixed mask off (or on: it is its own inverse) a 35-byte data block."""
    return bytes(a ^ b for a, b in zip(data, MASK, strict=True))


def codec2_frames(data: bytes) -> list[bytes]:
    """Split 35 un-masked bytes into ten 4-byte CODEC2 700C frames (28 bits + 4 zero bits)."""
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8)).reshape(FRAMES_PER_PACKET, FRAME_BITS)
    padded = np.concatenate([bits, np.zeros((FRAMES_PER_PACKET, 4), dtype=np.uint8)], axis=1)
    raw = np.packbits(padded.reshape(-1)).tobytes()
    return [raw[i : i + 4] for i in range(0, len(raw), 4)]


def parse_body(body: bytes, t: float = 0.0) -> VoicePacket | None:
    """A voice packet from the 38 bytes after the sync word, or None if it is not one."""
    if len(body) < _BODY_BYTES or body[0] != VOICE_SIZE or body[1] >> 4 != VOICE_TYPE:
        return None
    if body[2] > _MAX_NUMBER:
        return None
    return VoicePacket(body[2], unmask(bytes(body[3 : 3 + DATA_BYTES])), t)


def decode_window(seg: np.ndarray) -> list[VoicePacket]:
    """Decode one window of I/Q at fsk.WORK_RATE into voice packets."""
    tones = fsk.find_tones(seg)
    if tones is None:
        return []
    c_high, c_low = fsk.tone_sums(seg, tones[0], tones[1])
    phase = fsk.best_phase(c_high, c_low)
    n = len(c_high) - 1
    count = int((n - phase) / fsk.SAMPLES_PER_SYMBOL) - 1
    e_high, e_low = fsk.symbol_energies(c_high, c_low, phase, count)
    margin = e_high - e_low
    candidates: list[tuple[int, int, float]] = []
    for polarity in (-1.0, 1.0):  # bit 1 is the low tone, so the normal polarity is -1
        bits = ((polarity * margin) > 0).astype(np.uint8)
        candidates.extend((i, e, polarity) for i, e in fsk.sync_candidates(bits, _NEED_BITS))
    candidates.sort(key=lambda c: (c[1], c[0]))
    kept: list[tuple[int, int, float]] = []
    for cand in candidates:
        if any(abs(cand[0] - k[0]) < 40 for k in kept):
            continue
        kept.append(cand)
    kept.sort(key=lambda c: c[0])
    out: list[VoicePacket] = []
    for index, _errors, polarity in kept:
        eh, el = fsk.refine_packet(
            c_high, c_low, phase + index * fsk.SAMPLES_PER_SYMBOL, count=_NEED_BITS
        )
        bits = ((polarity * (eh - el)) > 0).astype(np.uint8)
        if int(np.count_nonzero(bits[:16] != fsk.SYNC_BITS)) > _MAX_SYNC_ERRORS:
            continue
        start = (phase + index * fsk.SAMPLES_PER_SYMBOL) / fsk.WORK_RATE
        packet = parse_body(np.packbits(bits[16:]).tobytes(), start)
        if packet is not None:
            out.append(packet)
    return out


class FskVoiceStream:
    """Feeds arbitrary I/Q blocks to the voice demodulator."""

    def __init__(self, sample_rate: float) -> None:
        self._rate = float(sample_rate)
        self._decimator = fsk.Decimator(self._rate)
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start_s = 0.0  # stream time of self._buf[0]

    def feed(self, iq: np.ndarray) -> list[VoicePacket]:
        """Add I/Q samples; return the voice packets found in the completed windows."""
        if not fsk._SCIPY_AVAILABLE:
            return []
        y = self._decimator.push(iq)
        if len(y):
            self._buf = np.concatenate([self._buf, y])
        out: list[VoicePacket] = []
        while len(self._buf) >= fsk._WINDOW:
            out.extend(
                p._replace(t=p.t + self._buf_start_s)
                for p in decode_window(self._buf[: fsk._WINDOW])
            )
            self._buf = self._buf[fsk._STEP :]
            self._buf_start_s += fsk._STEP / fsk.WORK_RATE
        return out


class FskVoiceSdrDemod(QThread):
    """Runs a FskVoiceStream on an SDR pipeline's raw I/Q in a worker thread."""

    packet_received: Signal = Signal(int, bytes, float)  # number, 35 data bytes, stream time

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._stream = FskVoiceStream(sample_rate)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self._stop_event = threading.Event()

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe() (any thread)."""
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(iq.astype(np.complex64))

    def stop(self) -> None:
        """Stop the worker thread."""
        self._stop_event.set()
        self.wait(3000)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            for packet in self._stream.feed(iq):
                self.packet_received.emit(packet.number, packet.data, packet.t)


class VoiceMessage:
    """The packets of one voice message, by packet number."""

    def __init__(self, started_at: float) -> None:
        self.packets: dict[int, bytes] = {}
        self.started_at = started_at
        self.last_at = started_at
        self.complete = False  # set when the message is closed
        # True when the packets come from a CRC-checked source (UNNE-1B's type 15 through the
        # Telemetry tab): their numbers are right as sent and need no repair from the timing.
        self.trusted = False
        self.arrivals: list[tuple[int, bytes, float]] = []  # (number as sent, data, stream time)
        self.last_t = 0.0

    @property
    def packet_count(self) -> int:
        """Number of distinct packets received."""
        return len(self.packets)

    @property
    def length(self) -> int:
        """Packets from number 0 to the highest number received."""
        return max(self.packets) + 1 if self.packets else 0

    @property
    def missing(self) -> list[int]:
        """Numbers (from 0 to the highest received) with no packet."""
        return [n for n in range(self.length) if n not in self.packets]

    @property
    def duration_s(self) -> float:
        """Audio length, counting missing packets as silence."""
        return self.length * PACKET_SECONDS

    def renumber(self) -> None:
        """Rebuild ``packets`` from the arrivals, repairing packet numbers from the timing.

        A voice packet has no CRC, so its number can have a bit error. The packets are sent
        0.4 s apart, so ``number - t / 0.4`` is the same for all of them; the median of that
        offset is taken as the truth, and the number of a packet that disagrees is replaced
        by the one its arrival time implies.

        A ``trusted`` message (CRC-checked packets, not sent 0.4 s apart) keeps its numbers.
        """
        if self.trusted:
            self.packets = {}
            for number, data, _t in self.arrivals:
                self.packets.setdefault(number, data)
            return
        offsets = sorted(n - t / PACKET_SECONDS for n, _d, t in self.arrivals)
        median = offsets[len(offsets) // 2]
        packets: dict[int, bytes] = {}
        for number, data, t in self.arrivals:
            if abs(number - t / PACKET_SECONDS - median) > 0.6:
                number = round(median + t / PACKET_SECONDS)
            if number >= 0:
                packets.setdefault(number, data)
        self.packets = packets

    def to_pcm(self) -> np.ndarray:
        """Decode to 8 kHz int16 audio; missing packets are silence (needs libcodec2)."""
        from comms.voice.codec2 import SAMPLE_RATE, Codec2Decoder

        spp = int(SAMPLE_RATE * PACKET_SECONDS)
        chunks: list[np.ndarray] = []
        decoder: Codec2Decoder | None = None
        try:
            for number in range(self.length):
                data = self.packets.get(number)
                if data is None:
                    if decoder is not None:
                        decoder.close()
                        decoder = None
                    chunks.append(np.zeros(spp, dtype=np.int16))
                    continue
                if decoder is None:
                    decoder = Codec2Decoder()
                chunks.extend(decoder.decode(f) for f in codec2_frames(data))
        finally:
            if decoder is not None:
                decoder.close()
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)


class VoiceMessageAssembler:
    """Groups voice packets into messages.

    The satellite sends a message in its own slot, so a message ends when no packet came for
    MESSAGE_GAP_S of stream time (the next slot carries other packets). Packet numbers are
    repaired from the arrival times (see VoiceMessage.renumber). ``add()`` returns
    ``(message, finished)`` where *finished* is the message that was just closed, if any.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.current: VoiceMessage | None = None

    def add(
        self, number: int, data: bytes, t: float | None = None, trusted: bool = False
    ) -> tuple[VoiceMessage, VoiceMessage | None]:
        """Add one packet; *t* is its stream time (default: the clock).

        *trusted*: the packet's number is certain (see VoiceMessage.trusted). Packets of the two
        kinds never share a message.
        """
        now = self._clock()
        if t is None:
            t = now
        finished: VoiceMessage | None = None
        cur = self.current
        if cur is not None and (t - cur.last_t > MESSAGE_GAP_S or cur.trusted != trusted):
            cur.complete = True
            finished = cur
            cur = None
        if cur is None:
            cur = VoiceMessage(now)
            cur.trusted = trusted
            self.current = cur
        cur.arrivals.append((number, data, t))
        cur.last_t = t
        cur.last_at = now
        cur.renumber()
        return cur, finished

    def close_if_idle(self) -> VoiceMessage | None:
        """Close the current message if no packet came for MESSAGE_GAP_S; returns it."""
        cur = self.current
        if cur is not None and self._clock() - cur.last_at > MESSAGE_GAP_S:
            cur.complete = True
            self.current = None
            return cur
        return None
