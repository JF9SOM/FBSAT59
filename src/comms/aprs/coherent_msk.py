"""Coherent MSK/GMSK demodulator + HDLC deframer for 4800/9600 baud G3RUH AX.25.

Why this exists
---------------
The FM discriminator + Direwolf path (comms.aprs.g3ruh_demod) needs about 12-14
dB Eb/N0 to decode 4800/9600 baud G3RUH frames. Satellite passes routinely
deliver less than that, and every discriminator-based decoder tried (Direwolf,
SatNOGS' gr-satnogs flowgraph, GNU Radio's gmsk_demod, gr-satellites) topped out
at the same handful of frames on a real ARICA-2 recording where a coherent
detector recovered ten times as many (2026-09-24, see docs/communications.md).

A 4800/9600 baud GMSK/GFSK signal with modulation index h = 0.5 (deviation =
baud / 4) is MSK, and MSK can be detected coherently:

1. The signal is resampled to 8 samples/symbol and low-pass filtered.
2. At symbol boundaries the carrier phase is a multiple of 90 degrees, so
   multiplying the k-th boundary sample by ``(-j)^k`` turns it into BPSK. The
   carrier phase (and its drift) is tracked by squaring that BPSK stream and
   averaging over a short window, with the phase *unwrapped* -- a branch flip of
   the ``angle()/2`` estimate would otherwise flip the decisions and cost a bit
   error each time.
3. A half-sine matched filter (2 symbols wide) is applied to the derotated I/Q
   arms, the bits are the differential decode of the resulting signs.
4. G3RUH descrambling, NRZI decoding and HDLC deframing (CRC-16/X.25) follow.
   Frames are returned as raw bytes (CRC stripped) -- whether they are valid AX.25
   is left to comms.aprs.parser (ARICA-2's frames, for instance, are HDLC frames
   whose content is not an AX.25 header).

The symbol timing is found by brute force: all 8 timing phases and both bit
polarities are decoded and CRC-valid frames are merged. Frames are never
"guessed" -- only CRC-valid frames are emitted.

Measured (synthetic G3RUH frames in AWGN, 30 frames per point): the 50 % decode
point is ~8 dB Eb/N0 at 4800 baud (Direwolf ~13.5, GNU Radio gmsk_demod ~13),
and ~8.5 dB SNR in 11 kHz at 9600 baud (Direwolf ~12.5, gr-satellites ~14).

Streaming
---------
CoherentMskStream buffers I/Q in chunks with an overlap longer than any frame,
so a frame cut by one chunk boundary is complete in the next; duplicates from the
overlap are removed by (content, time). CoherentMskSdrDemod runs a stream on a
worker thread fed by SDRPipeline.subscribe(), like the discriminators in
g3ruh_demod.py / afsk_audio_demod.py.
"""

from __future__ import annotations

import math
import queue
import re
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

SAMPLES_PER_SYMBOL = 8
MIN_FRAME_BYTES = 17
MAX_FRAME_BYTES = 1024
# Low-pass cutoff (Hz) applied before symbol-rate sampling, per baud rate.
_LOWPASS_HZ: dict[int, float] = {4800: 3500.0, 9600: 7000.0}
# Window (in symbols, each side) of the carrier-phase averager, and the block
# length (symbols) over which the residual carrier frequency is estimated.
_PHASE_WINDOW = 12
_FREQ_BLOCK = 128
# Chunk geometry (seconds). The overlap must exceed the longest frame plus its
# preamble; 1 s covers 1024 bytes at 9600 baud and 512 bytes at 4800 baud. A
# frame is reported at most one chunk after it ends.
_CHUNK_S = 2.0
_OVERLAP_S = 1.0
# The same frame decoded at several timing phases, or again in the overlap of
# the next chunk, lands within this many seconds of itself.
_DEDUP_WINDOW_S = 0.03
# A chunk is only decoded when the in-band power stands out from the noise
# floor by this factor (density ratio) -- saves CPU between passes.
_GATE_RATIO = 1.3

_FLAG_RE = re.compile("01111110")


def _make_crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _make_crc_table()


def crc16_x25(data: bytes) -> int:
    """CRC-16/X.25 (the AX.25 / HDLC frame check sequence) of *data*."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFF


def descramble_nrzi(symbols: np.ndarray) -> np.ndarray:
    """G3RUH descramble (1 + x^12 + x^17) then NRZI-decode a 0/1 symbol array."""
    d = symbols.astype(np.uint8)
    out = d.copy()
    out[12:] ^= d[:-12]
    out[17:] ^= d[:-17]
    nrzi = np.ones_like(out)
    nrzi[1:] = (out[1:] == out[:-1]).astype(np.uint8)
    return nrzi


def hdlc_frames(bits: np.ndarray) -> list[tuple[bytes, int]]:
    """Extract CRC-valid HDLC frames from a NRZI-decoded 0/1 bit array.

    Returns ``(frame_without_crc, bit_index_of_the_opening_flag)`` tuples.
    """
    text = "".join("1" if b else "0" for b in bits.tolist())
    flags = [m.start() for m in _FLAG_RE.finditer(text)]
    frames: list[tuple[bytes, int]] = []
    for start, end in zip(flags, flags[1:], strict=False):
        body = text[start + 8 : end]
        if len(body) < 8 * MIN_FRAME_BYTES or len(body) > 8 * (MAX_FRAME_BYTES + 2) * 2:
            continue
        body = body.replace("111110", "11111")  # remove stuffed zeros
        n = len(body) // 8
        if n < MIN_FRAME_BYTES:
            continue
        octets = bytes(int(body[i * 8 : i * 8 + 8][::-1], 2) for i in range(n))
        if len(octets) - 2 > MAX_FRAME_BYTES:
            continue
        if crc16_x25(octets[:-2]) == int.from_bytes(octets[-2:], "little"):
            frames.append((octets[:-2], start))
    return frames


def _track_carrier_phase(y: np.ndarray, phase: int) -> np.ndarray:
    """Per-sample carrier phase (rad) estimated from the symbol-boundary samples.

    *y* is the complex signal at SAMPLES_PER_SYMBOL samples/symbol and *phase* the
    timing phase (0..SAMPLES_PER_SYMBOL-1) of the boundary samples.
    """
    spb = SAMPLES_PER_SYMBOL
    z = y[phase::spb]
    n = len(z)
    k = np.arange(n)
    w = z * np.exp(-0.5j * np.pi * k)  # MSK boundary samples -> BPSK (+-1)
    u = w * w  # squaring removes the BPSK sign
    slope = np.zeros(n)
    for a in range(0, n, _FREQ_BLOCK):
        b = min(n, a + _FREQ_BLOCK)
        if b - a > 2:
            seg = u[a:b]
            slope[a:b] = np.angle(np.sum(seg[1:] * np.conj(seg[:-1]))) / 2.0
    cum = np.cumsum(slope)  # residual carrier frequency, integrated
    u2 = (w * np.exp(-1j * cum)) ** 2
    csum = np.concatenate([[0], np.cumsum(u2)])
    lo = np.clip(k - _PHASE_WINDOW, 0, n)
    hi = np.clip(k + _PHASE_WINDOW + 1, 0, n)
    theta_2 = np.unwrap(np.angle(csum[hi] - csum[lo]))  # continuous 2*theta
    theta_k = theta_2 / 2.0 + cum
    idx = phase + spb * k
    return np.asarray(np.interp(np.arange(len(y)), idx, theta_k))


def _coherent_bits(y: np.ndarray, phase: int) -> np.ndarray:
    """Coherently detected raw (scrambled, NRZI) bits at timing phase *phase*."""
    spb = SAMPLES_PER_SYMBOL
    theta = _track_carrier_phase(y, phase)
    yd = y * np.exp(-1j * theta)
    n = (len(y) - phase) // spb
    if n < 2:
        return np.zeros(0, dtype=np.uint8)
    win = np.cos(np.pi * np.arange(-spb, spb + 1) / (2 * spb))  # half-sine, 2 symbols
    i_arm = np.convolve(yd.real, win[::-1], mode="same")
    q_arm = np.convolve(yd.imag, win[::-1], mode="same")
    k = np.arange(n)
    idx = phase + spb * k
    keep = idx < len(y)
    idx, k = idx[keep], k[keep]
    r = np.where(k % 2 == 0, i_arm[idx], q_arm[idx])  # I on even, Q on odd boundaries
    wv = r * np.where((k // 2) % 2 == 0, 1.0, -1.0)
    s = (wv > 0).astype(np.uint8)
    bits = np.ones_like(s)
    bits[1:] = (s[1:] == s[:-1]).astype(np.uint8)  # same sign -> +1 -> bit 1
    return bits


class CoherentMskDecoder:
    """Stateless chunk decoder: complex I/Q at *sample_rate* -> HDLC frames."""

    def __init__(self, sample_rate: float, baud: int) -> None:
        self.sample_rate = float(sample_rate)
        self.baud = int(baud)
        self.symbol_rate_hz = float(baud * SAMPLES_PER_SYMBOL)
        rate = int(round(self.sample_rate))
        target = int(round(self.symbol_rate_hz))
        g = math.gcd(rate, target)
        self._up = target // g
        self._down = rate // g
        lp = _LOWPASS_HZ.get(self.baud, 0.73 * self.baud)
        self._lp_taps = (
            sp_signal.firwin(101, lp, fs=self.symbol_rate_hz) if _SCIPY_AVAILABLE else None
        )
        self._lp_hz = lp

    def _to_symbol_rate(self, iq: np.ndarray) -> np.ndarray:
        y = sp_signal.resample_poly(iq, self._up, self._down)
        return np.asarray(sp_signal.lfilter(self._lp_taps, 1.0, y), dtype=np.complex64)

    def signal_present(self, iq: np.ndarray) -> bool:
        """Cheap gate: does in-band power stand out from the noise floor?"""
        if len(iq) < 4096 or not _SCIPY_AVAILABLE:
            return False
        nper = 4096
        f, p = sp_signal.welch(iq, fs=self.sample_rate, nperseg=nper, return_onesided=False)
        f = np.fft.fftshift(f)
        p = np.fft.fftshift(p)
        inband = p[np.abs(f) < 0.6 * self.baud].mean()
        lo, hi = 2.0 * self.baud, min(4.0 * self.baud, self.sample_rate * 0.45)
        if hi <= lo:
            return True
        floor = np.median(p[(np.abs(f) > lo) & (np.abs(f) < hi)])
        return bool(inband > _GATE_RATIO * floor)

    def decode(self, iq: np.ndarray) -> list[tuple[bytes, float]]:
        """Decode one chunk. Returns ``(frame, seconds_from_chunk_start)`` tuples.

        The same frame decoded at several timing phases / polarities is merged.
        """
        if not _SCIPY_AVAILABLE or len(iq) < 2048:
            return []
        y = self._to_symbol_rate(iq)
        spb = SAMPLES_PER_SYMBOL
        found: list[tuple[bytes, float]] = []
        for phase in range(spb):
            raw = _coherent_bits(y, phase)
            if len(raw) < 64:
                continue
            for invert in (0, 1):
                bits = descramble_nrzi(raw ^ invert)
                for frame, bit_idx in hdlc_frames(bits):
                    found.append((frame, (bit_idx * spb + phase) / self.symbol_rate_hz))
        return _merge_duplicates(found)


def _merge_duplicates(found: list[tuple[bytes, float]]) -> list[tuple[bytes, float]]:
    """Merge identical frames decoded within _DEDUP_WINDOW_S of each other."""
    found.sort(key=lambda item: item[1])
    out: list[tuple[bytes, float]] = []
    last_seen: dict[bytes, float] = {}
    for frame, t in found:
        prev = last_seen.get(frame)
        if prev is not None and t - prev < _DEDUP_WINDOW_S:
            last_seen[frame] = t
            continue
        last_seen[frame] = t
        out.append((frame, t))
    return out


class CoherentMskStream:
    """Feeds arbitrary I/Q blocks to a CoherentMskDecoder, chunked with overlap."""

    def __init__(self, sample_rate: float, baud: int) -> None:
        self.decoder = CoherentMskDecoder(sample_rate, baud)
        self._rate = float(sample_rate)
        self._chunk = int(_CHUNK_S * self._rate)
        self._overlap = int(_OVERLAP_S * self._rate)
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start = 0  # absolute sample index of _buf[0]
        self._recent: dict[bytes, float] = {}  # frame -> absolute time (s)

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start = 0
        self._recent.clear()

    def feed(self, iq: np.ndarray) -> list[bytes]:
        """Add I/Q samples; return newly decoded (de-duplicated) frames."""
        self._buf = np.concatenate([self._buf, np.asarray(iq, dtype=np.complex64)])
        out: list[bytes] = []
        while len(self._buf) >= self._chunk:
            out.extend(self._decode_buffer())
            drop = len(self._buf) - self._overlap
            self._buf = self._buf[drop:]
            self._buf_start += drop
        return out

    def flush(self) -> list[bytes]:
        """Decode whatever is buffered (end of stream / tests)."""
        out = self._decode_buffer() if len(self._buf) >= 2048 else []
        self._buf = np.zeros(0, dtype=np.complex64)
        return out

    def _decode_buffer(self) -> list[bytes]:
        if not self.decoder.signal_present(self._buf):
            return []
        t0 = self._buf_start / self._rate
        fresh: list[bytes] = []
        for frame, t in self.decoder.decode(self._buf):
            t_abs = t0 + t
            prev = self._recent.get(frame)
            if prev is not None and abs(t_abs - prev) < 2 * _DEDUP_WINDOW_S:
                self._recent[frame] = t_abs
                continue
            self._recent[frame] = t_abs
            fresh.append(frame)
        # forget frames older than two chunks
        horizon = t0 - 2 * _CHUNK_S
        for key in [k for k, v in self._recent.items() if v < horizon]:
            del self._recent[key]
        return fresh


class CoherentMskSdrDemod(QThread):
    """Runs a CoherentMskStream on an SDR pipeline's raw I/Q in a worker thread.

    Usage
    -----
    demod = CoherentMskSdrDemod(sample_rate=int(pipeline._device.sample_rate), baud=4800)
    demod.frame_received.connect(consumer)   # bytes: HDLC frame without CRC
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()
    """

    frame_received: Signal = Signal(bytes)

    def __init__(self, sample_rate: int, baud: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._stream = CoherentMskStream(sample_rate, baud)
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
                    "coherent_msk queue full, dropped block (total drops=%d)",
                    self._diag_drop_count,
                )

    def stop(self) -> None:
        self._stop_event.set()
        self.wait(3000)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                frames = self._stream.feed(iq)
            except Exception:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().exception("CoherentMskSdrDemod.run(): feed() raised")
                raise
            for frame in frames:
                self.frame_received.emit(frame)
