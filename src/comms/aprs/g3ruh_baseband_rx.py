"""Brute-force G3RUH baseband receiver for 4800/9600 baud audio from a radio.

Direwolf's G3RUH demodulator is a single fixed filter/PLL chain, and on weak
satellite signals it misses frames that are in the audio: on a real ARICA-2 pass
(2026-10-07, rig audio) it found 2 of 3 CRC-valid frames, the app live found 1.
This receiver runs next to it on the same audio and tries what a PLL can only
guess at: every sample phase within the bit, both polarities, and several
low-pass bandwidths at once, keeping any frame whose CRC checks.

Per window of audio:

  1. zero-phase pre-filter in the frequency domain (high-pass at 40 Hz to drop
     DC and drift, then one of several low-pass cut-offs; the same response as
     ``sosfiltfilt`` of a 2nd order high-pass and 4th order Butterworth low-pass)
  2. for each filter, sample phase and polarity: slice the level, undo the
     G3RUH scrambler (x^17 + x^12 + 1) and NRZI, find the 0x7E flags, unstuff
     and check the CRC-16/X.25 FCS
  3. a frame is accepted only when at least ``min_hits`` of those candidates
     produced it. A random segment passes a 16-bit CRC by chance about once in
     65536 candidates and there are tens of thousands per minute of noise, so a
     single hit is not trusted; a real frame decodes at neighbouring phases and
     filters many times over. Measured on four real rig-audio recordings: the
     real frames had 4, 18, 26 and 46 hits, the one false frame in 55 minutes
     of audio had 2. A frame with fewer than ``strong_hits`` hits must also pass
     the optional ``validator`` (e.g. "parses as an AX.25 UI frame").

The audio is windowed with overlap, so the same frame shows up in several
windows; each distinct frame is reported once. numpy only (no scipy).
"""

from __future__ import annotations

import contextlib
import queue
import time
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QThread, Signal

from comms.aprs.g3ruh_tx import fcs16

SAMPLE_RATE = 48_000
# Low-pass cut-offs (Hz) tried in parallel; None = no low-pass. Best single values
# on real rig audio were 3.3-3.6 kHz, but which one wins varies from frame to frame.
DEFAULT_CUTOFFS_HZ: tuple[float | None, ...] = (
    None,
    3000.0,
    3300.0,
    3600.0,
    3900.0,
    4200.0,
    4800.0,
)
_HIGHPASS_HZ = 40.0
_FLAG = np.array([0, 1, 1, 1, 1, 1, 1, 0], dtype=np.uint8)
_MIN_FRAME_BYTES = 17  # AX.25 header (14) + control/PID + at least 1 byte (FCS excluded)
_MAX_FRAME_BYTES = 400


def _response(n: int, cutoff_hz: float | None) -> NDArray[np.float64]:
    """Zero-phase magnitude response over the rfft bins of an *n*-point FFT."""
    f = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    x = np.maximum(f, 1e-9) / _HIGHPASS_HZ
    resp: NDArray[np.float64] = x**4 / (1.0 + x**4)  # filtfilt of a 2nd order Butterworth HPF
    if cutoff_hz is not None:
        resp = resp / (1.0 + (f / cutoff_hz) ** 8)  # filtfilt of a 4th order Butterworth LPF
    return resp


def _descramble(x: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """d[n] = x[n] ^ x[n-12] ^ x[n-17] (inverse of the transmit scrambler)."""
    d = x.copy()
    d[12:] ^= x[:-12]
    d[17:] ^= x[:-17]
    result: NDArray[np.uint8] = d
    return result


def _nrzi_decode(line: NDArray[np.uint8]) -> NDArray[np.uint8]:
    prev = np.concatenate((np.zeros(1, dtype=np.uint8), line[:-1]))
    same: NDArray[np.uint8] = (line == prev).astype(np.uint8)  # 1 = no change
    return same


def _frames_in(bits: NDArray[np.uint8]) -> list[bytes]:
    """CRC-valid HDLC frames (FCS stripped) in a decoded bit stream."""
    if len(bits) < 8 * (_MIN_FRAME_BYTES + 4):
        return []
    win = np.lib.stride_tricks.sliding_window_view(bits, 8)
    pos = np.nonzero((win == _FLAG).all(axis=1))[0]
    frames: list[bytes] = []
    for a, b in zip(pos[:-1], pos[1:], strict=False):
        seg = bits[a + 8 : b]
        n = len(seg)
        if n < 8 * (_MIN_FRAME_BYTES + 2) or n > 8 * (_MAX_FRAME_BYTES + 2) * 1.2:
            continue
        out: list[int] = []
        ones = 0
        ok = True
        for bit in seg.tolist():
            if ones == 5:
                if bit == 0:  # stuffed bit
                    ones = 0
                    continue
                ok = False  # six 1s inside a frame: not HDLC data
                break
            out.append(bit)
            ones = ones + 1 if bit else 0
        if not ok or len(out) % 8:
            continue
        arr = np.array(out, dtype=np.uint8).reshape(-1, 8)
        raw = (arr * (1 << np.arange(8))).sum(axis=1).astype(np.uint8).tobytes()
        if (
            _MIN_FRAME_BYTES <= len(raw) - 2 <= _MAX_FRAME_BYTES
            and fcs16(raw[:-2]).to_bytes(2, "little") == raw[-2:]
        ):
            frames.append(raw[:-2])
    return frames


class G3ruhBasebandDecoder:
    """Streaming decoder: push audio, collect newly decoded frames.

    *baud* must divide 48 kHz into a whole number of samples per bit (4800 and
    9600 do). Call :meth:`push_samples` with float audio at 48 kHz and
    :meth:`decode_pending` regularly; it works on the last ``window_s`` seconds
    once at least ``stride_s`` seconds of new audio have arrived.
    """

    def __init__(
        self,
        baud: int = 4800,
        cutoffs_hz: tuple[float | None, ...] = DEFAULT_CUTOFFS_HZ,
        min_hits: int = 3,
        strong_hits: int = 8,
        validator: Callable[[bytes], bool] | None = None,
        window_s: float = 3.0,
        stride_s: float = 1.0,
        dedupe_s: float = 10.0,
    ) -> None:
        if SAMPLE_RATE % baud:
            raise ValueError("48 kHz must be a whole number of samples per bit")
        self._spb = SAMPLE_RATE // baud
        self._cutoffs = cutoffs_hz
        self._min_hits = min_hits
        self._strong_hits = strong_hits
        self._validator = validator
        self._window = int(window_s * SAMPLE_RATE)
        self._stride = int(stride_s * SAMPLE_RATE)
        self._dedupe_s = dedupe_s
        self._buf: NDArray[np.float32] = np.zeros(0, dtype=np.float32)
        self._since_decode = 0
        self._seen: dict[bytes, float] = {}
        self._resp: dict[tuple[int, float | None], NDArray[np.float64]] = {}

    def reset(self) -> None:
        """Forget buffered audio and the list of frames already reported."""
        self._buf = np.zeros(0, dtype=np.float32)
        self._since_decode = 0
        self._seen.clear()

    def push_samples(self, audio: NDArray[np.float32]) -> None:
        """Append mono 48 kHz audio (float, about -1..1)."""
        self._buf = np.concatenate((self._buf, np.asarray(audio, dtype=np.float32)))
        self._since_decode += len(audio)
        if len(self._buf) > 2 * self._window:
            self._buf = self._buf[-2 * self._window :]

    def decode_pending(self, now: float | None = None) -> list[bytes]:
        """Frames newly found since the last call (each distinct frame once)."""
        if self._since_decode < self._stride or len(self._buf) < self._window // 2:
            return []
        self._since_decode = 0
        t = time.monotonic() if now is None else now
        self._seen = {f: ts for f, ts in self._seen.items() if t - ts < self._dedupe_s}
        new: list[bytes] = []
        for frame, hits in self._decode_window(self._buf[-self._window :]).items():
            if frame in self._seen or not self._accept(frame, hits):
                continue
            self._seen[frame] = t
            new.append(frame)
        return new

    def _accept(self, frame: bytes, hits: int) -> bool:
        if hits < self._min_hits:
            return False
        if hits >= self._strong_hits or self._validator is None:
            return True
        return self._validator(frame)

    def _decode_window(self, audio: NDArray[np.float32]) -> dict[bytes, int]:
        """frame -> number of (filter, phase, polarity) candidates that produced it."""
        n = len(audio)
        spec = np.fft.rfft(audio.astype(np.float64))
        hits: dict[bytes, int] = {}
        for cutoff in self._cutoffs:
            key = (n, cutoff)
            resp = self._resp.get(key)
            if resp is None:
                resp = self._resp[key] = _response(n, cutoff)
            y = np.fft.irfft(spec * resp, n)
            for phase in range(self._spb):
                level = y[phase :: self._spb]
                for invert in (False, True):
                    x = ((level > 0) if invert else (level < 0)).astype(np.uint8)
                    bits = _nrzi_decode(_descramble(x))
                    for frame in _frames_in(bits):
                        hits[frame] = hits.get(frame, 0) + 1
        return hits


_QUEUE_BLOCKS = 256


class G3ruhBasebandRxThread(QThread):
    """Runs :class:`G3ruhBasebandDecoder` off the audio callback thread.

    ``push_samples`` only queues the block (it is called from the sound card
    callback and must never block); the thread decodes and emits every new
    frame as ``frame_received``.
    """

    frame_received: Signal = Signal(bytes)

    def __init__(
        self,
        baud: int = 4800,
        validator: Callable[[bytes], bool] | None = None,
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._decoder = G3ruhBasebandDecoder(baud=baud, validator=validator)
        self._queue: queue.Queue[NDArray[np.float32] | None] = queue.Queue(maxsize=_QUEUE_BLOCKS)

    def push_samples(self, audio: NDArray[np.float32]) -> None:
        """Queue one audio block; drops it when the decoder is not keeping up."""
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(np.asarray(audio, dtype=np.float32).copy())

    def stop(self) -> None:
        """Ask the thread to finish and wait for it."""
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self.wait(3000)

    def run(self) -> None:
        while True:
            block = self._queue.get()
            if block is None:
                return
            self._decoder.push_samples(block)
            for frame in self._decoder.decode_pending():
                self.frame_received.emit(frame)
