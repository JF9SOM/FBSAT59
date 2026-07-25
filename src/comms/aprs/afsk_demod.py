"""Bell 202 AFSK 1200 baud demodulator for APRS / AX.25.

Algorithm
---------
1. Decimate I/Q to ~9 600 Hz (8× oversampling at 1 200 baud).
   If scipy is available, a proper FIR anti-alias filter is applied before
   decimation; otherwise simple stride-based decimation is used.
2. Compute instantaneous frequency via the phase-difference method:
       f[n] = angle(iq[n] * conj(iq[n-1])) * Fs / (2π)
3. Smooth with a one-symbol-wide box filter.
4. Threshold at 1 700 Hz (midpoint of mark=1 200 Hz and space=2 200 Hz).
5. NRZI decode: a frequency change between symbols → bit 0, no change → bit 1.
6. HDLC sync + bit-unstuffing + CRC-16/CCITT verification.

The class exposes the same ``frame_received(bytes)`` Signal as KissClient
so it is a drop-in replacement for the Direwolf receive path.

Usage
-----
    demod = AfskDemodulator(sample_rate=2_400_000)
    demod.frame_received.connect(on_frame)
    demod.start()

    # From SDRPipeline subscriber callback (called in pipeline thread):
    pipeline.subscribe(demod.push_samples)

    # To stop:
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

try:
    from scipy import signal as sp_signal

    _SCIPY: bool = True
except ImportError:
    sp_signal = None
    _SCIPY = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MARK_HZ: float = 1200.0
_SPACE_HZ: float = 2200.0
_BAUD: float = 1200.0
_THRESHOLD_HZ: float = (_MARK_HZ + _SPACE_HZ) / 2.0  # 1700 Hz
_OVERSAMPLE: int = 8  # samples per symbol after decimation
_TARGET_RATE: int = int(_BAUD * _OVERSAMPLE)  # 9 600 Hz


# ---------------------------------------------------------------------------
# CRC-16/CCITT (AX.25 FCS)
# ---------------------------------------------------------------------------


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT with polynomial 0x8408 and initial value 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else (crc >> 1)
    return crc


# ---------------------------------------------------------------------------
# HDLC frame synchroniser
# ---------------------------------------------------------------------------


class _HdlcState:
    """Sliding-window HDLC frame extractor with NRZI + bit-unstuffing.

    AX.25 bit ordering: LSB first within each byte.
    Flag pattern: 0x7E = 01111110 (transmitted MSB→LSB as 0,1,1,1,1,1,1,0).
    """

    _FLAG = 0x7E
    _MIN_FRAME_BYTES = 14  # dest(7) + src(7) = minimum AX.25 frame

    def __init__(self) -> None:
        self.last_tone: int = 0  # previous demodulated tone (0=mark,1=space)
        self._shift: int = 0  # 8-bit shift register for flag detection
        self._in_frame: bool = False
        self._ones: int = 0  # consecutive 1-bits (bit-stuffing counter)
        self._bit_pos: int = 0  # bit position within current byte (0-7)
        self._byte: int = 0  # byte being assembled
        self._frame: bytearray = bytearray()

    def push_bit(self, bit: int) -> bytes | None:
        """Push one NRZI-decoded data bit; return a validated frame or None."""
        # --- shift register for flag detection ---
        self._shift = ((self._shift >> 1) | (bit << 7)) & 0xFF

        if self._shift == self._FLAG:
            result: bytes | None = None
            if self._in_frame and len(self._frame) >= self._MIN_FRAME_BYTES:
                result = self._validate()
            self._reset()
            self._in_frame = True
            return result

        if not self._in_frame:
            return None

        # --- bit-unstuffing ---
        if bit == 1:
            self._ones += 1
            if self._ones > 5:
                # abort — invalid sequence
                self._reset()
                return None
        else:
            if self._ones == 5:
                # stuffed zero — silently discard
                self._ones = 0
                return None
            self._ones = 0

        # --- assemble byte (LSB first) ---
        self._byte |= bit << self._bit_pos
        self._bit_pos += 1
        if self._bit_pos == 8:
            self._frame.append(self._byte)
            self._byte = 0
            self._bit_pos = 0

        return None

    def _reset(self) -> None:
        self._in_frame = False
        self._ones = 0
        self._bit_pos = 0
        self._byte = 0
        self._frame = bytearray()

    def _validate(self) -> bytes | None:
        """Check CRC and return frame payload (without FCS), or None."""
        raw = bytes(self._frame)
        if len(raw) < 2:
            return None
        payload = raw[:-2]
        fcs_rx = raw[-2] | (raw[-1] << 8)
        if _crc16_ccitt(payload) != fcs_rx:
            return None
        return payload


# ---------------------------------------------------------------------------
# Demodulator QThread
# ---------------------------------------------------------------------------


class AfskDemodulator(QThread):
    """Bell 202 AFSK 1200 baud demodulator.

    Emits ``frame_received(bytes)`` for each valid AX.25 frame, using the
    same signal signature as ``KissClient`` so it can be used interchangeably.
    """

    frame_received: Signal = Signal(bytes)

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._sample_rate = sample_rate
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()
        self._hdlc = _HdlcState()
        # Residual samples carried between consecutive push_samples() calls
        self._residual: np.ndarray = np.array([], dtype=np.complex64)
        # Diagnostic-only (see sdr.diag_log): counts blocks dropped because
        # this thread wasn't draining the queue fast enough. Logged on the
        # first drop and every 50th thereafter so a sustained backlog is
        # still visible without flooding the log.
        self._diag_drop_count: int = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe().

        Safe to call from any thread; drops the block (does not block the
        SDR pipeline's own thread) if the internal queue is full, i.e. this
        demodulator thread is not keeping up.
        """
        try:
            self._q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            self._diag_drop_count += 1
            if self._diag_drop_count == 1 or self._diag_drop_count % 50 == 0:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().info(
                    "afsk_demod queue full, dropped block (total drops=%d)",
                    self._diag_drop_count,
                )

    def stop(self) -> None:
        """Stop the demodulator thread."""
        self._stop_event.set()
        self.wait(3000)

    # ------------------------------------------------------------------ #
    # QThread.run
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            self._process(iq)

    # ------------------------------------------------------------------ #
    # DSP pipeline
    # ------------------------------------------------------------------ #

    def _process(self, iq: np.ndarray) -> None:
        sr = self._sample_rate

        # ---- 1. Decimate to ~_TARGET_RATE ----
        dec = max(1, round(sr / _TARGET_RATE))
        if dec > 1:
            if _SCIPY and sp_signal is not None:
                try:
                    iq = sp_signal.decimate(
                        iq.astype(np.complex128), dec, ftype="fir", zero_phase=True
                    ).astype(np.complex64)
                except Exception:
                    iq = iq[::dec]
            else:
                iq = iq[::dec]
        actual_rate: float = sr / dec

        # ---- 2. Prepend residual ----
        iq = np.concatenate([self._residual, iq])

        # ---- 3. Instantaneous frequency (phase-difference method) ----
        if len(iq) < 2:
            self._residual = iq
            return
        phase_diff = np.angle(iq[1:] * np.conj(iq[:-1]))
        inst_freq: np.ndarray = phase_diff * (actual_rate / (2.0 * np.pi))

        # ---- 4. One-symbol box-filter smoothing ----
        sym_samples = max(1, int(round(actual_rate / _BAUD)))
        kernel = np.ones(sym_samples, dtype=np.float32) / sym_samples
        smoothed = np.convolve(inst_freq.astype(np.float32), kernel, mode="same")

        # ---- 5. Symbol sampling + NRZI decode ----
        n_syms = len(smoothed) // sym_samples
        for i in range(n_syms):
            centre = i * sym_samples + sym_samples // 2
            if centre >= len(smoothed):
                break
            # tone: 0 = mark (f < 1700), 1 = space (f >= 1700)
            tone = 1 if smoothed[centre] >= _THRESHOLD_HZ else 0
            # NRZI: bit = 1 (no tone change), bit = 0 (tone change)
            bit = 1 if (tone == self._hdlc.last_tone) else 0
            self._hdlc.last_tone = tone

            frame = self._hdlc.push_bit(bit)
            if frame is not None:
                self.frame_received.emit(frame)

        # ---- 6. Save residual for next call ----
        used = n_syms * sym_samples
        # +1 because inst_freq has one fewer sample than iq
        self._residual = iq[used + 1 :]
