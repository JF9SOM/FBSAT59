"""Foresail-1p GMSK receiver: raw SDR I/Q -> Skylink frames.

Foresail-1p's downlink is 9600 baud GMSK (modulation index 0.5, so MSK: the coherent
detector of ``comms.aprs.coherent_msk`` applies, about 4 dB more sensitive than an FM
discriminator) carrying plain NRZ bits: preamble, the CCSDS ASM ``0x1ACFFC1D``, a Golay(24,12)
length field, then a CCSDS-scrambled conventional-basis RS(255,223) codeword. The sync search,
Golay, descrambler and RS come from ``comms.ax100digi.frame`` (the same AX100 "ASM+Golay"
stack); the Golay field carries only the length, so the scrambler and RS flags are forced.

Checked against a real recording (2026-10-18, S/N about 11 dB): it decodes the two frames
gr-satellites decodes and a third that gr-satellites misses.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QThread, Signal

from comms.aprs.coherent_msk import CoherentMskDecoder
from comms.ax100digi import frame as ax100_frame

BAUD = 9600
_SYNC = ax100_frame._syncword_bits(ax100_frame.SKYLINK_SYNCWORD)
_SYNC_LEN = len(_SYNC)
_SYNC_ERRORS = 4  # sync word bit errors tolerated (gr-satellites' default)
_GOLAY_BITS = 24

# Decode geometry: a pass over the last _WINDOW_S seconds runs every _HOP_S seconds of new
# samples. A frame (255 bytes at 9600 baud is 0.21 s plus preamble) always lies whole in some
# window, and is reported at most about _HOP_S after it ends.
_WINDOW_S = 1.6
_HOP_S = 0.4
# The same frame decoded at several timing phases or in two windows lands this close to itself.
_DEDUP_S = 0.1


@dataclass(frozen=True)
class ForesailRxFrame:
    """A decoded frame: the Skylink frame bytes and where it was heard."""

    payload: bytes  # the Skylink frame (RS parity removed)
    start_s: float  # seconds from the first sample fed: start of the sync word
    end_s: float  # seconds from the first sample fed: end of the frame
    golay_errors: int
    rs_corrected: int | None


def _candidate_positions(bits: NDArray[np.uint8]) -> list[tuple[int, bool]]:
    """Positions where the sync word (or its inverse) matches with few bit errors."""
    if len(bits) < _SYNC_LEN + _GOLAY_BITS:
        return []
    windows = np.lib.stride_tricks.sliding_window_view(bits, _SYNC_LEN)
    distance = (windows != _SYNC).sum(axis=1)
    found = [(int(i), False) for i in np.where(distance <= _SYNC_ERRORS)[0]]
    found += [(int(i), True) for i in np.where(_SYNC_LEN - distance <= _SYNC_ERRORS)[0]]
    return found


class ForesailRxStream:
    """Feeds arbitrary I/Q blocks and returns newly decoded frames (de-duplicated)."""

    def __init__(self, sample_rate: float) -> None:
        self._rate = float(sample_rate)
        self._decoder = CoherentMskDecoder(self._rate, BAUD)
        self._window = int(_WINDOW_S * self._rate)
        self._hop = int(_HOP_S * self._rate)
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start = 0  # absolute index of _buf[0]
        self._since_decode = 0
        self._recent: dict[bytes, float] = {}

    @property
    def sample_rate(self) -> float:
        return self._rate

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start = 0
        self._since_decode = 0
        self._recent.clear()

    def feed(self, iq: NDArray[np.complex64]) -> list[ForesailRxFrame]:
        """Add I/Q samples; return the frames found since the last call."""
        block = np.asarray(iq, dtype=np.complex64)
        self._buf = np.concatenate([self._buf, block])
        self._since_decode += len(block)
        out: list[ForesailRxFrame] = []
        while self._since_decode >= self._hop and len(self._buf) >= self._window:
            out.extend(self._decode_buffer())
            self._since_decode -= self._hop
            drop = len(self._buf) - self._window
            if drop > 0:
                self._buf = self._buf[drop:]
                self._buf_start += drop
        return out

    def flush(self) -> list[ForesailRxFrame]:
        """Decode what is buffered (end of a recording / tests)."""
        out = self._decode_buffer() if len(self._buf) >= 2048 else []
        self._buf = np.zeros(0, dtype=np.complex64)
        return out

    def _decode_buffer(self) -> list[ForesailRxFrame]:
        if not self._decoder.signal_present(self._buf):
            return []
        t0 = self._buf_start / self._rate
        fresh: list[ForesailRxFrame] = []
        for phase, raw in self._decoder.raw_bit_streams(self._buf):
            for position, inverted in _candidate_positions(raw):
                bits: NDArray[np.uint8] = (raw ^ 1).astype(np.uint8) if inverted else raw
                decoded = ax100_frame.decode_frame_at(
                    bits,
                    position,
                    syncword=ax100_frame.SKYLINK_SYNCWORD,
                    forced_flags=(True, True),
                    rs_dual_basis=False,
                )
                if decoded is None:
                    continue
                start = t0 + (phase / 8.0 + position) / BAUD
                end = start + (_SYNC_LEN + _GOLAY_BITS + decoded.frame_len * 8) / BAUD
                previous = self._recent.get(decoded.payload)
                if previous is not None and abs(start - previous) < _DEDUP_S:
                    continue
                self._recent[decoded.payload] = start
                fresh.append(
                    ForesailRxFrame(
                        payload=decoded.payload,
                        start_s=start,
                        end_s=end,
                        golay_errors=decoded.golay_bit_errors,
                        rs_corrected=decoded.rs_bytes_corrected,
                    )
                )
        horizon = t0 - 2 * _WINDOW_S
        for key in [k for k, v in self._recent.items() if v < horizon]:
            del self._recent[key]
        fresh.sort(key=lambda f: f.start_s)
        return fresh


def decode_iq(iq: NDArray[np.complex64], sample_rate: float) -> list[ForesailRxFrame]:
    """Decode a whole recording (or any I/Q array) in one go."""
    stream = ForesailRxStream(sample_rate)
    frames = stream.feed(iq)
    frames.extend(stream.flush())
    # The flush window may repeat frames the hops already found: the stream de-duplicates.
    return frames


class ForesailSdrDemod(QThread):
    """Runs a ForesailRxStream on an SDR pipeline's raw I/Q in a worker thread.

    ::

        demod = ForesailSdrDemod(int(pipeline._device.sample_rate))
        demod.frame_received.connect(consumer)   # ForesailRxFrame, wall_s: monotonic seconds
        demod.start()
        pipeline.subscribe(demod.push_samples)
        ...
        pipeline.unsubscribe(demod.push_samples)
        demod.stop()
    """

    frame_received: Signal = Signal(object, float)  # (ForesailRxFrame, monotonic end time)

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._stream = ForesailRxStream(sample_rate)
        self._q: queue.Queue[NDArray[np.complex64]] = queue.Queue(maxsize=256)
        self._stop_event = threading.Event()
        self._t0: float | None = None  # monotonic time of the first sample

    def push_samples(self, iq: NDArray[np.complex64]) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe() (any thread)."""
        if self._t0 is None:
            self._t0 = time.monotonic() - len(iq) / self._stream.sample_rate
        # A full queue means the decoder fell behind: dropping a block only costs its frames.
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(iq.astype(np.complex64))

    def stop(self) -> None:
        self._stop_event.set()
        self.wait(3000)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            t0 = self._t0 if self._t0 is not None else time.monotonic()
            for frame in self._stream.feed(iq):
                self.frame_received.emit(frame, t0 + frame.end_s)
