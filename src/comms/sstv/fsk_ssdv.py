"""SSDV from a raw 2FSK downlink (800 baud, tones 1600 Hz apart), e.g. HADES-SA.

Why this exists
---------------
HADES-SA (NORAD 68446, 436.875 MHz, SatNOGS "FSK 800 bps 1600 Hz separation") sends
SSDV images as bare 2FSK: no AX.25/HDLC and no CCSDS framing, so neither the
"SSDV (AX.25)" mode (Direwolf) nor the "SSDV (CCSDS BPSK 9k6)" mode (ASRTU-1) can see it.

Stream format (found by analysing a real pass on 2026-10-12)
------------------------------------------------------------
* 800 baud 2FSK, tone spacing 1600 Hz (modulation index 2). Bit 1 is the *lower* tone.
* Each packet is the standard 256-byte SSDV packet (header, 205 payload bytes, CRC-32,
  32 Reed-Solomon parity bytes) **without** its leading ``55 66``; the callsign field
  starts with ``BF 35``, which therefore doubles as the sync word. 252 bytes follow the
  sync word, then ``0x55`` filler. Rebuilding ``55 66 BF 35`` + 252 bytes gives a packet
  the stock ``ssdv`` tool decodes (the CRC-32 only checks out with that exact prefix).
* The same stream also carries AMSAT-EA "Genesis" telemetry packets (they start with the
  same ``BF 35``); those are rejected by the CRC / header checks below.

Demodulation
------------
Non-coherent: the I/Q is decimated to 12.5 kHz, the two tone frequencies are found from
a 6 s spectrum, each symbol is correlated with both tones and the stronger one wins.
Symbol timing is the phase that maximises the decision margin. Windows overlap by half
and only sync words in the first half are taken, so a packet is never split by a window
edge.

A packet whose CRC-32 is valid is reported as verified. One whose CRC fails (bit errors)
is still reported -- ssdv's Reed-Solomon decoder can repair up to 16 bad bytes -- but only
if its sync word has at most one bit error and its header looks like SSDV.
"""

from __future__ import annotations

import math
import queue
import threading
import zlib
from typing import Any, NamedTuple

import numpy as np
from PySide6.QtCore import QThread, Signal

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

BAUD = 800
TONE_SEPARATION_HZ = 1600.0
WORK_RATE = 12500  # Hz; 15.625 samples per symbol
SYNC_WORD = b"\xbf\x35"
BODY_BYTES = 252  # bytes after the sync word
PACKET_PREFIX = b"\x55\x66" + SYNC_WORD  # restores a standard 256-byte SSDV packet
PACKET_SIZE = 256
_CRC_END = PACKET_SIZE - 4 - 32  # CRC-32 covers packet[1:_CRC_END]

_WINDOW_S = 6.0
_STEP_S = 3.0
_WINDOW = int(_WINDOW_S * WORK_RATE)
_STEP = int(_STEP_S * WORK_RATE)
_STEP_SYMBOLS = int(_STEP_S * BAUD)  # sync hits are taken from the first half only
# Tone search: the pair must lie within +-_SEARCH_HZ of the centre, and each tone must
# stand out from the median of the smoothed spectrum by this power ratio (about 7 dB).
_SEARCH_HZ = 5500.0
_TONE_RATIO = 5.0
_SPECTRUM_FFT = 1 << 17
_SYNC_BITS = np.unpackbits(np.frombuffer(SYNC_WORD, dtype=np.uint8))
_MAX_SYNC_ERRORS_VERIFIED = 2
_MAX_SYNC_ERRORS_UNVERIFIED = 1
_SYNC_MERGE_SYMBOLS = 40  # candidates closer than this are the same packet
SAMPLES_PER_SYMBOL = WORK_RATE / BAUD
# Per-packet timing refinement: offsets (in samples) tried around the window-wide phase.
_REFINE_OFFSETS = tuple(np.arange(-2.0, 2.01, 0.5).tolist())
# Decimator overlap-save margins, in units of `up` output samples / `down` input samples.
_DROP_UNITS = 16
_TAIL_UNITS = 32


class FskSsdvPacket(NamedTuple):
    """One SSDV packet from the 2FSK stream."""

    data: bytes  # 256 bytes, starting with 55 66
    verified: bool  # CRC-32 valid (no bit errors); False = left to ssdv's RS decoder


def crc_ok(packet: bytes) -> bool:
    """True if a 256-byte SSDV packet's CRC-32 matches."""
    if len(packet) != PACKET_SIZE:
        return False
    return zlib.crc32(packet[1:_CRC_END]) == int.from_bytes(packet[_CRC_END : _CRC_END + 4], "big")


def header_plausible(packet: bytes) -> bool:
    """Cheap SSDV header sanity check for a packet whose CRC failed.

    Header after ``55 66``: callsign(4), image id, packet id(2), width/16, height/16,
    flags, MCU offset, MCU index(2).
    """
    if len(packet) != PACKET_SIZE:
        return False
    width, height = packet[9], packet[10]
    return 1 <= width <= 64 and 1 <= height <= 64


class _Decimator:
    """Streaming I/Q decimation to WORK_RATE (overlap-save around scipy's resample_poly)."""

    def __init__(self, sample_rate: float) -> None:
        rate = int(round(sample_rate))
        g = math.gcd(rate, WORK_RATE)
        self.up = WORK_RATE // g
        self.down = rate // g
        self._pending = np.zeros(0, dtype=np.complex64)
        self._tail = np.zeros(0, dtype=np.complex64)
        # Work on blocks of about a quarter second.
        self._min_units = max(1, int(0.25 * WORK_RATE / self.up))

    def push(self, iq: np.ndarray) -> np.ndarray:
        """Add raw I/Q; return the newly available samples at WORK_RATE."""
        self._pending = np.concatenate([self._pending, np.asarray(iq, dtype=np.complex64)])
        units = len(self._pending) // self.down
        if units < self._min_units:
            return np.zeros(0, dtype=np.complex64)
        n = units * self.down
        x = np.concatenate([self._tail, self._pending[:n]])
        self._pending = self._pending[n:]
        y = np.asarray(sp_signal.resample_poly(x, self.up, self.down), dtype=np.complex64)
        drop = _DROP_UNITS * self.up if len(self._tail) else 0
        hold = _DROP_UNITS * self.up
        keep_raw = min(len(x), _TAIL_UNITS * self.down)
        self._tail = x[len(x) - keep_raw :].copy()
        return y[drop : max(drop, len(y) - hold)]


def find_tones(seg: np.ndarray) -> tuple[float, float] | None:
    """Frequencies (Hz, low and high) of the FSK tone pair in *seg*, or None if absent."""
    n = len(seg)
    if n < WORK_RATE:
        return None
    spec = np.abs(np.fft.fft(seg * np.hanning(n), _SPECTRUM_FFT)) ** 2
    spec = np.fft.fftshift(spec)
    freqs = np.fft.fftshift(np.fft.fftfreq(_SPECTRUM_FFT, 1.0 / WORK_RATE))
    bin_hz = WORK_RATE / _SPECTRUM_FFT
    width = max(1, int(round(30.0 / bin_hz)))
    csum = np.concatenate([[0.0], np.cumsum(spec)])
    half = width // 2
    idx = np.arange(_SPECTRUM_FFT)
    lo = np.clip(idx - half, 0, _SPECTRUM_FFT)
    hi = np.clip(idx + half + 1, 0, _SPECTRUM_FFT)
    smooth = (csum[hi] - csum[lo]) / (hi - lo)
    band = np.abs(freqs) < _SEARCH_HZ
    floor = float(np.median(smooth[band]))
    if floor <= 0.0:
        return None
    sep = int(round(TONE_SEPARATION_HZ / bin_hz))
    first = np.flatnonzero(band & (freqs <= _SEARCH_HZ - TONE_SEPARATION_HZ))
    if len(first) == 0:
        return None
    score = smooth[first] * smooth[first + sep]
    best = int(first[int(np.argmax(score))])
    if smooth[best] < _TONE_RATIO * floor or smooth[best + sep] < _TONE_RATIO * floor:
        return None
    return float(freqs[best]), float(freqs[best + sep])


def tone_sums(seg: np.ndarray, f_low: float, f_high: float) -> tuple[np.ndarray, np.ndarray]:
    """Running sums of *seg* mixed with each tone (symbol energies are differences of these)."""
    t = np.arange(len(seg)) / WORK_RATE
    c_high = np.concatenate([[0], np.cumsum(seg * np.exp(-2j * np.pi * f_high * t))])
    c_low = np.concatenate([[0], np.cumsum(seg * np.exp(-2j * np.pi * f_low * t))])
    return c_high, c_low


def symbol_energies(
    c_high: np.ndarray, c_low: np.ndarray, start: float, count: int
) -> tuple[np.ndarray, np.ndarray]:
    """Per-symbol energy at each tone for *count* symbols starting at sample *start*."""
    pos = start + np.arange(count + 1) * SAMPLES_PER_SYMBOL
    grid = np.arange(len(c_high))
    high = np.interp(pos, grid, c_high.real) + 1j * np.interp(pos, grid, c_high.imag)
    low = np.interp(pos, grid, c_low.real) + 1j * np.interp(pos, grid, c_low.imag)
    return np.abs(np.diff(high)) ** 2, np.abs(np.diff(low)) ** 2


def best_phase(c_high: np.ndarray, c_low: np.ndarray) -> float:
    """Symbol timing phase (in samples) with the largest average decision margin."""
    n = len(c_high) - 1
    best_score = -1.0
    best = 0.0
    for phase in np.arange(0.0, SAMPLES_PER_SYMBOL, 0.5):
        count = int((n - phase) / SAMPLES_PER_SYMBOL) - 1
        e_high, e_low = symbol_energies(c_high, c_low, phase, count)
        score = float(np.sum(np.abs(e_high - e_low) / (e_high + e_low + 1e-12)))
        if score > best_score:
            best_score, best = score, float(phase)
    return best


def sync_candidates(bits: np.ndarray, limit: int | None = None) -> list[tuple[int, int]]:
    """Positions ``(bit_index, sync_errors)`` where the sync word matches with few errors.

    Only positions in the first half window are taken (the second half is the first half
    of the next window), and only where a whole packet follows.
    """
    n = len(bits)
    need = 16 + 8 * BODY_BYTES
    if n < need:
        return []
    count = n - 15
    errors = np.zeros(count, dtype=np.int32)
    for k in range(16):
        errors += bits[k : k + count] != _SYNC_BITS[k]
    stop = min(count, _STEP_SYMBOLS, n - need + 1) if limit is None else limit
    return [
        (int(i), int(errors[i])) for i in np.flatnonzero(errors[:stop] <= _MAX_SYNC_ERRORS_VERIFIED)
    ]


def refine_packet(
    c_high: np.ndarray, c_low: np.ndarray, start: float
) -> tuple[np.ndarray, np.ndarray]:
    """Re-time one packet: try small timing offsets, keep the one that captures most energy.

    The window-wide timing phase is a compromise; locally a fraction of a symbol (or
    more, after a deep fade) can be better. Returns the per-symbol energies at each tone.
    """
    count = 16 + 8 * BODY_BYTES
    best_score = -1.0
    best: tuple[np.ndarray, np.ndarray] | None = None
    for offset in _REFINE_OFFSETS:
        e_high, e_low = symbol_energies(c_high, c_low, start + offset, count)
        score = float(np.sum(np.maximum(e_high, e_low)))
        if score > best_score:
            best_score, best = score, (e_high, e_low)
    assert best is not None
    return best


def decode_window(seg: np.ndarray) -> list[FskSsdvPacket]:
    """Decode one window of I/Q at WORK_RATE into SSDV packets."""
    tones = find_tones(seg)
    if tones is None:
        return []
    c_high, c_low = tone_sums(seg, tones[0], tones[1])
    phase = best_phase(c_high, c_low)
    n = len(c_high) - 1
    e_high, e_low = symbol_energies(c_high, c_low, phase, int((n - phase) / SAMPLES_PER_SYMBOL) - 1)
    margin = e_high - e_low
    candidates: list[tuple[int, int, float]] = []
    for polarity in (-1.0, 1.0):  # bit 1 is the low tone, so the normal polarity is -1
        bits = ((polarity * margin) > 0).astype(np.uint8)
        candidates.extend((i, e, polarity) for i, e in sync_candidates(bits))
    # One packet can be found in both polarities or one bit apart: keep the best of each.
    candidates.sort(key=lambda c: (c[1], c[0]))
    kept: list[tuple[int, int, float]] = []
    for cand in candidates:
        if any(abs(cand[0] - k[0]) < _SYNC_MERGE_SYMBOLS for k in kept):
            continue
        kept.append(cand)
    kept.sort(key=lambda c: c[0])
    packets: list[FskSsdvPacket] = []
    for index, _errors, polarity in kept:
        eh, el = refine_packet(c_high, c_low, phase + index * SAMPLES_PER_SYMBOL)
        bits = ((polarity * (eh - el)) > 0).astype(np.uint8)
        errors = int(np.count_nonzero(bits[:16] != _SYNC_BITS))
        if errors > _MAX_SYNC_ERRORS_VERIFIED:
            continue
        packet = PACKET_PREFIX + np.packbits(bits[16:]).tobytes()
        if crc_ok(packet):
            packets.append(FskSsdvPacket(packet, True))
        elif errors <= _MAX_SYNC_ERRORS_UNVERIFIED and header_plausible(packet):
            packets.append(FskSsdvPacket(packet, False))
    return packets


class FskSsdvStream:
    """Feeds arbitrary I/Q blocks to the 2FSK SSDV demodulator."""

    def __init__(self, sample_rate: float) -> None:
        self._rate = float(sample_rate)
        self._decimator = _Decimator(self._rate)
        self._buf = np.zeros(0, dtype=np.complex64)

    def reset(self) -> None:
        """Forget buffered samples."""
        self._buf = np.zeros(0, dtype=np.complex64)
        self._decimator = _Decimator(self._rate)

    def feed(self, iq: np.ndarray) -> list[FskSsdvPacket]:
        """Add I/Q samples; return the SSDV packets found in the completed windows."""
        if not _SCIPY_AVAILABLE:
            return []
        y = self._decimator.push(iq)
        if len(y):
            self._buf = np.concatenate([self._buf, y])
        out: list[FskSsdvPacket] = []
        while len(self._buf) >= _WINDOW:
            out.extend(decode_window(self._buf[:_WINDOW]))
            self._buf = self._buf[_STEP:]
        return out


class FskSsdvSdrDemod(QThread):
    """Runs a FskSsdvStream on an SDR pipeline's raw I/Q in a worker thread.

    Usage
    -----
    demod = FskSsdvSdrDemod(sample_rate=int(pipeline._device.sample_rate))
    demod.packet_received.connect(consumer)   # (bytes: 256-byte packet, bool: CRC valid)
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()
    """

    packet_received: Signal = Signal(bytes, bool)

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._stream = FskSsdvStream(sample_rate)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self._stop_event = threading.Event()
        self._diag_drop_count = 0

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe() (any thread)."""
        try:
            self._q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            self._diag_drop_count += 1
            if self._diag_drop_count == 1 or self._diag_drop_count % 50 == 0:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().info(
                    "fsk_ssdv queue full, dropped block (total drops=%d)",
                    self._diag_drop_count,
                )

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
            try:
                packets = self._stream.feed(iq)
            except Exception:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().exception("FskSsdvSdrDemod.run(): feed() raised")
                raise
            for packet in packets:
                self.packet_received.emit(packet.data, packet.verified)
