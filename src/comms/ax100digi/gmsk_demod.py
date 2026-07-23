"""GMSK/FSK demodulation for the AX100 "ASM+Golay" downlink (GreenCube/
MARMOTSat-compatible), from SDR-fed complex I/Q.

Not yet validated against a real captured GreenCube/MARMOTSat signal — the
deviation/bandwidth constants and the fixed-phase symbol sampling strategy
below are a first pass pending real-air verification (see CLAUDE.md's
Phase 0/1 plan: record IQ during a real pass, then tune).

Design notes
------------
Uses the same non-coherent phase-difference discriminator technique as
sdr/demodulator.py's NFM path and aprs/g3ruh_demod.py (no coherent PLL/
Costas loop needed for FSK-family modulations).

For symbol timing recovery, rather than a continuously-adapting clock
recovery loop (Mueller & Muller / Gardner), this picks a *fixed* sampling
phase per decode cycle by brute-force: the discriminator output is
resampled to a fixed number of samples-per-symbol, sliced into `sps`
candidate bit streams (one per starting phase), and every candidate is
independently run through the actual frame decoder (ASM sync + Golay +
RS). Only a phase whose bits happen to line up with real symbol
boundaries can pass Golay's 3-bit error correction and (if used) RS's
correction — so the frame decoder itself is the phase-goodness test, and
no separate correlation/PLL logic is needed. This is only correct because
AX100 frames are short (<=258 bytes => ~1.7s at 1200 baud): SDR clock
drift over that span is negligible, so a single fixed phase per capture
window is sufficient (a continuously-tracked loop would be needed for
frames long enough for the sample clock to drift by more than a
fraction of a symbol).
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from numpy.typing import NDArray

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

DEFAULT_BAUD = 1200.0
# MSK (h=0.5) peak frequency deviation is Rb/4; GMSK's Gaussian pre-filter
# does not change this nominal deviation, only the pulse shape/ISI.
DEFAULT_DEVIATION_HZ = DEFAULT_BAUD / 4.0
DEFAULT_SPS = 8  # samples-per-symbol used for the fixed-phase slicer


class GmskDiscriminator:
    """Stateful I/Q -> baseband discriminator DSP for a narrowband GMSK/FSK
    signal, mirroring aprs/g3ruh_demod.py's G3ruhDiscriminator but tuned for
    a much narrower deviation/bandwidth (1200 baud AX100 vs 9600 baud
    G3RUH)."""

    def __init__(
        self,
        input_rate: float,
        baud: float = DEFAULT_BAUD,
        deviation_hz: float = DEFAULT_DEVIATION_HZ,
    ) -> None:
        self._input_rate = input_rate
        self._baud = baud
        self._deviation_hz = deviation_hz
        self._dc_zi_i = np.zeros(1, dtype=np.float32)
        self._dc_zi_q = np.zeros(1, dtype=np.float32)
        self._build_filters()

    def _build_filters(self) -> None:
        rate = self._input_rate
        alpha_dc = float(np.clip(1.0 - (2.0 * np.pi * 30.0 / rate), 0.0, 0.9999))
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -alpha_dc], dtype=np.float64)

        # IF half-bandwidth: deviation plus a couple of symbol-rate widths
        # of margin for the Gaussian pulse's spectral spreading.
        if_half_bw = self._deviation_hz + 2.0 * self._baud
        if_bw = float(np.clip(if_half_bw / (rate / 2.0), 0.001, 0.499))
        self._if_b = sp_signal.firwin(63, if_bw).astype(np.float32) if _SCIPY_AVAILABLE else None

    @property
    def output_rate(self) -> float:
        return self._input_rate

    def process(self, iq: NDArray[np.complex64]) -> NDArray[np.float32]:
        """Demodulate one I/Q block. Returns a real-valued discriminator
        signal at the input sample rate (possibly empty)."""
        if len(iq) == 0 or not _SCIPY_AVAILABLE or self._if_b is None:
            return np.array([], dtype=np.float32)

        i_dc, self._dc_zi_i = sp_signal.lfilter(
            self._dc_b, self._dc_a, iq.real.astype(np.float32), zi=self._dc_zi_i
        )
        q_dc, self._dc_zi_q = sp_signal.lfilter(
            self._dc_b, self._dc_a, iq.imag.astype(np.float32), zi=self._dc_zi_q
        )
        iq_dc = (np.asarray(i_dc) + 1j * np.asarray(q_dc)).astype(np.complex64)

        iq_if = sp_signal.lfilter(self._if_b, [1.0], iq_dc)
        if len(iq_if) < 2:
            return np.array([], dtype=np.float32)

        prev = np.empty_like(iq_if)
        prev[0] = iq_if[0]
        prev[1:] = iq_if[:-1]
        discrim = np.angle(iq_if * np.conj(prev))
        freq = discrim * (self._input_rate / (2 * np.pi * self._deviation_hz))
        result: NDArray[np.float32] = freq.astype(np.float32)
        return result


def resample_to_sps(
    discrim: NDArray[np.float32],
    sample_rate: float,
    baud: float = DEFAULT_BAUD,
    sps: int = DEFAULT_SPS,
) -> NDArray[np.float32]:
    """Resample a discriminator signal to exactly `sps` samples per symbol."""
    if not _SCIPY_AVAILABLE:
        raise RuntimeError("scipy is required for GMSK symbol resampling")
    target_rate = baud * sps
    frac = Fraction(target_rate / sample_rate).limit_denominator(1000)
    resampled: NDArray[np.float32] = sp_signal.resample_poly(
        discrim, frac.numerator, frac.denominator
    )
    return resampled


def slice_bits_all_phases(
    oversampled: NDArray[np.float32], sps: int = DEFAULT_SPS
) -> list[NDArray[np.uint8]]:
    """Hard-slice an `sps`-oversampled discriminator signal into `sps`
    candidate NRZ bit streams, one per starting sample phase.

    Positive discriminator value -> bit 1 (mark), matching this project's
    other FSK-family demodulators; not yet verified against a real AX100
    signal's mark/space polarity convention.
    """
    candidates = []
    for phase in range(sps):
        samples = oversampled[phase::sps]
        bits = (samples > 0).astype(np.uint8)
        candidates.append(bits)
    return candidates
