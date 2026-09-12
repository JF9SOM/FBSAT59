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
5. Symbol-clock recovery: a per-sample digital PLL tracks the transmitter's
   bit clock (which is not synchronized to the receiver's), decoding one
   bit each time it completes a symbol period; every observed tone
   transition -- which can only occur at a true symbol boundary -- nudges
   the PLL back into alignment. NRZI decode: a frequency change between
   symbols → bit 0, no change → bit 1.
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

# Digital-PLL correction strength applied to _pll_phase whenever a raw tone
# transition is observed (see _process()). 0.0 = no correction (free-running
# clock only); 1.0 = snap the phase fully onto the transition every time
# (too twitchy under noise). 0.5 halves the current phase error per
# transition -- the same damped-correction idea classic Bell 202 TNC modems
# use for bit sync.
_PLL_GAIN: float = 0.5


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
        # TEMPORARY diagnostic (do not remove until the user confirms real
        # decodes over SDR): counts flags seen (any 0x7E, whether or not a
        # frame followed) and CRC pass/fail on candidate frames, logged
        # periodically. Answers a narrower question than "did a frame
        # decode": is the bitstream even HDLC-flag-synchronized at all
        # (PLL/timing working), separate from whether assembled frames pass
        # CRC (signal/DSP quality).
        self._diag_flag_count: int = 0
        self._diag_crc_ok: int = 0
        self._diag_crc_fail: int = 0

    def push_bit(self, bit: int) -> bytes | None:
        """Push one NRZI-decoded data bit; return a validated frame or None."""
        # --- shift register for flag detection ---
        self._shift = ((self._shift >> 1) | (bit << 7)) & 0xFF

        if self._shift == self._FLAG:
            self._diag_flag_count += 1
            result: bytes | None = None
            was_in_frame = self._in_frame
            frame_len = len(self._frame)
            if self._in_frame and len(self._frame) >= self._MIN_FRAME_BYTES:
                result = self._validate()
                if result is not None:
                    self._diag_crc_ok += 1
                else:
                    self._diag_crc_fail += 1
            # TEMPORARY diagnostic (do not remove until confirmed working):
            # log EVERY flag event (not sampled) with the byte length (and a
            # hex preview) of whatever accumulated since the previous flag,
            # to tell "genuinely nothing between flags" (idle/flag-fill, or
            # noise triggering the flag pattern in isolation) apart from
            # "a real frame started but never reached 14 bytes" (garbled
            # mid-frame, e.g. a PLL/bit-sync robustness issue under real
            # noise that the earlier synthetic-signal tests couldn't catch).
            from sdr.diag_log import get_sdr_diag_logger

            get_sdr_diag_logger().info(
                "afsk_demod HDLC flag #%d: was_in_frame=%s frame_len=%d hex=%s "
                "totals(crc_ok=%d crc_fail=%d)",
                self._diag_flag_count,
                was_in_frame,
                frame_len,
                bytes(self._frame[:20]).hex(),
                self._diag_crc_ok,
                self._diag_crc_fail,
            )
            self._reset()
            self._in_frame = True
            return result

        if not self._in_frame:
            return None

        # --- bit-unstuffing ---
        if bit == 1:
            self._ones += 1
            if self._ones > 6:
                # Genuine abort sequence (7+ consecutive 1s) -- not
                # explainable by a flag (which has exactly six 1-bits) or
                # valid stuffing (which never allows more than five in a
                # row), so this really is a protocol violation.
                self._reset()
                return None
            if self._ones == 6:
                # Exactly six consecutive 1s can only be a flag's own six
                # 1-bits in progress -- properly stuffed data never reaches
                # six in a row (a 0 is always stuffed in after the fifth).
                # Do not treat this bit as frame data or abort yet: the
                # very next bit is the flag's closing 0, which the
                # unconditional shift-register check above will recognize
                # and handle. (Without this, every frame's own valid
                # closing flag was mistaken for a bit-stuffing violation
                # and the fully-assembled frame was discarded one bit
                # before the shift register could ever recognize it as a
                # flag -- no frame could ever be returned via the normal
                # closing-flag path, regardless of signal quality.)
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
        # Symbol-clock recovery (digital PLL) state, carried across
        # consecutive _process() calls the same way _residual is. See the
        # per-sample loop in _process() for how it's used: `_pll_phase` is
        # the fraction of the current symbol elapsed (wraps 1.0 -> 0.0 at
        # each symbol boundary, which is when a bit is decoded); `_raw_tone`
        # is the last per-sample (not per-symbol) mark/space reading, so a
        # real tone transition -- which in a clean Bell 202 signal can only
        # occur exactly at a symbol boundary -- can be used to nudge
        # `_pll_phase` back into alignment with the transmitter's clock.
        # Without this, sampling drifted onto a fixed grid with no relation
        # to the actual incoming bit timing and never decoded anything.
        self._pll_phase: float = 0.0
        self._raw_tone: int = 0
        self._pll_seeded: bool = False
        # Streaming state for the one-symbol box filter (step 4 in
        # _process()): the last (sym_samples - 1) inst_freq values, carried
        # over so the *next* call's convolution has real history at its
        # leading edge instead of the implicit zero-padding
        # np.convolve(..., mode="same") would otherwise use there. Without
        # this, every block boundary (every ~16384-sample SDRPipeline
        # block, i.e. roughly every 15-65ms depending on sample rate)
        # corrupted a few samples right at the point where PLL continuity
        # matters most -- fatal once packets span more than one block.
        self._box_state: np.ndarray = np.array([], dtype=np.float32)
        # DC-offset removal (single-pole high-pass, 30 Hz cutoff, same
        # design as sdr.demodulator.Demodulator._remove_dc()). Most SDRs
        # (RTL-SDR included) produce a DC spike from LO self-mixing right
        # at the tuned centre frequency -- i.e. right on top of the FM
        # carrier this demodulator needs, since it's tuned to sit exactly
        # at baseband DC for direct phase-difference discrimination. The
        # SDR Control tab's own NFM audio path already strips this before
        # demodulating; this raw-I/Q subscriber tap never did, so it saw a
        # biased instantaneous-frequency estimate even on a clean, audibly
        # correct-sounding signal. Applied with scipy when available (a
        # proper streaming IIR, state carried in _dc_zi_i/_dc_zi_q the same
        # way _box_state is); otherwise falls back to simple per-block mean
        # subtraction, which is cruder but still much better than nothing.
        alpha_dc = float(np.clip(1.0 - (2.0 * np.pi * 30.0 / sample_rate), 0.0, 0.9999))
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -alpha_dc], dtype=np.float64)
        self._dc_zi_i: np.ndarray = np.zeros(1)
        self._dc_zi_q: np.ndarray = np.zeros(1)
        # Diagnostic-only (see sdr.diag_log): counts blocks dropped because
        # this thread wasn't draining the queue fast enough. Logged on the
        # first drop and every 50th thereafter so a sustained backlog is
        # still visible without flooding the log.
        self._diag_drop_count: int = 0
        # TEMPORARY diagnostic (do not remove until confirmed working): see
        # push_samples().
        self._diag_recv_count: int = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe().

        Safe to call from any thread; drops the block (does not block the
        SDR pipeline's own thread) if the internal queue is full, i.e. this
        demodulator thread is not keeping up.
        """
        self._diag_recv_count += 1
        if self._diag_recv_count == 1 or self._diag_recv_count % 200 == 0:
            from sdr.diag_log import get_sdr_diag_logger

            get_sdr_diag_logger().info(
                "afsk_demod push_samples: block #%d received (len=%d, peak_abs=%.4f)",
                self._diag_recv_count,
                len(iq),
                float(np.max(np.abs(iq))) if len(iq) else 0.0,
            )
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

    def _remove_dc(self, iq: np.ndarray) -> np.ndarray:
        """Strip the SDR's DC/LO-leakage offset from I and Q separately."""
        if _SCIPY and sp_signal is not None:
            i_dc_raw, self._dc_zi_i = sp_signal.lfilter(
                self._dc_b, self._dc_a, iq.real.astype(np.float32), zi=self._dc_zi_i
            )
            q_dc_raw, self._dc_zi_q = sp_signal.lfilter(
                self._dc_b, self._dc_a, iq.imag.astype(np.float32), zi=self._dc_zi_q
            )
            return (
                np.asarray(i_dc_raw, dtype=np.float32) + 1j * np.asarray(q_dc_raw, dtype=np.float32)
            ).astype(np.complex64)
        if len(iq) == 0:
            return iq
        centered: np.ndarray = (iq - np.mean(iq)).astype(np.complex64)
        return centered

    def _process(self, iq: np.ndarray) -> None:
        sr = self._sample_rate
        iq = self._remove_dc(iq)

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

        # ---- 4. One-symbol box-filter smoothing (streaming) ----
        sym_samples = max(1, int(round(actual_rate / _BAUD)))
        if len(self._box_state) != sym_samples - 1:
            # First call, or actual_rate rounded to a different sym_samples
            # than last time (sr/dec can drift a little from call to call).
            self._box_state = np.zeros(sym_samples - 1, dtype=np.float32)
        kernel = np.ones(sym_samples, dtype=np.float32) / sym_samples
        extended = np.concatenate([self._box_state, inst_freq.astype(np.float32)])
        # "valid" (never "same"): every output here is a true, unpadded
        # average -- history came from _box_state, not implicit zeros.
        smoothed = np.convolve(extended, kernel, mode="valid")
        if sym_samples > 1:
            self._box_state = extended[-(sym_samples - 1) :]

        # ---- 5. Symbol-clock recovery (digital PLL) + NRZI decode ----
        # Per-sample loop (not a fixed i*sym_samples grid): _pll_phase
        # advances by 1/sym_samples every raw sample and a bit is decoded
        # each time it wraps past 1.0. A real tone transition can only
        # happen at a true symbol boundary in a clean Bell 202 signal, so
        # whenever the per-sample tone changes, that sample IS (up to
        # noise) a boundary -- used to damp-correct _pll_phase toward 0/1.0
        # via _PLL_GAIN. Without this the sampling instant is fixed to an
        # arbitrary block-relative grid with no relation to the actual
        # incoming bit clock and never stays aligned long enough to decode
        # a frame, however clean the signal is otherwise.
        phase_inc = 1.0 / sym_samples
        tones = smoothed >= _THRESHOLD_HZ  # 0 = mark, 1 = space, per raw sample
        for tone_bool in tones:
            tone = 1 if tone_bool else 0
            if not self._pll_seeded:
                self._raw_tone = tone
                self._pll_seeded = True

            # Capture the tone that held through the symbol *up to* this
            # sample before possibly updating it below. Once locked, a real
            # transition lands on (or very near) the same sample as a phase
            # wrap -- if _raw_tone were updated to the new tone first, the
            # wrap below would sample the symbol that is only just starting
            # instead of the one that just finished.
            prev_tone = self._raw_tone
            if tone != prev_tone:
                if self._pll_phase < 0.5:
                    self._pll_phase *= 1.0 - _PLL_GAIN
                else:
                    self._pll_phase += (1.0 - self._pll_phase) * _PLL_GAIN
                self._raw_tone = tone

            self._pll_phase += phase_inc
            if self._pll_phase >= 1.0:
                self._pll_phase -= 1.0
                # Symbol boundary reached -- prev_tone held steady (mark or
                # space) through the symbol that just ended, so it is that
                # symbol's decoded tone.
                bit = 1 if (prev_tone == self._hdlc.last_tone) else 0
                self._hdlc.last_tone = prev_tone
                frame = self._hdlc.push_bit(bit)
                if frame is not None:
                    self.frame_received.emit(frame)

        # ---- 6. Save residual for next call ----
        # The PLL loop above always consumes the *entire* smoothed/inst_freq
        # array now (unlike the old fixed-grid sampler, which intentionally
        # left a partial symbol's worth unconsumed and, as a side effect,
        # always retained enough raw iq tail for continuity). All that's
        # actually needed for the next call's phase-difference calculation
        # (phase_diff[0] = angle(new_iq[1] * conj(new_iq[0])), where
        # new_iq[0] is this residual) is this block's very last raw sample.
        self._residual = iq[-1:]
