"""Real-audio <-> complex-baseband bridge for Rig + Sound Card reception/
transmission of the AX100 "ASM+Golay" GMSK signal (GreenCube/MARMOTSat).

Background
----------
The satellite's actual downlink is genuinely GMSK-modulated RF. GreenCube's
own ground-station software (GNURadio scripts in the Digipeater Manual)
receives/transmits it by running the radio in **SSB mode** and doing the
GMSK modulation/demodulation entirely in software on the resulting audio:
the GMSK baseband is placed at a fixed offset (1600 Hz by default,
`SSB_RX_offset`/`SSB_TX_offset` in GreenCube's config.ini) inside the SSB
audio passband, so the pure modulated signal survives SSB's asymmetric
filtering undistorted. This module reimplements that audio<->baseband
bridge so this project's Rig + Sound Card path (mirroring FT4/Q65's
`AudioDeviceManager`-based I/O) can feed/consume the same
GmskDiscriminator/`frame.encode_frame()` code used for the SDR path.

Not yet validated against a real MARMOTSat/GreenCube signal — the 1600 Hz
offset and USB/LSB sideband convention come from GreenCube's published
manual, not from an actual over-the-air capture through this project's own
audio chain (see CLAUDE.md's Phase 0/1 plan).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

DEFAULT_SHIFT_HZ = 1600.0  # GreenCube config.ini's SSB_RX_offset/SSB_TX_offset default
_HILBERT_TAPS = 101  # odd length, Type III FIR Hilbert transformer


def _hilbert_fir_taps(numtaps: int) -> NDArray[np.float64]:
    """Coefficients of an odd-length, antisymmetric (Type III) FIR Hilbert
    transformer: h[n] = 2/(pi*n) for odd n, 0 for even n, Hamming-windowed.
    """
    if numtaps % 2 == 0:
        raise ValueError("numtaps must be odd for a Type III Hilbert FIR filter")
    half = (numtaps - 1) // 2
    n = np.arange(-half, half + 1)
    h = np.zeros_like(n, dtype=np.float64)
    odd_mask = n % 2 != 0
    h[odd_mask] = 2.0 / (np.pi * n[odd_mask])
    window = np.hamming(numtaps)
    return h * window


class AnalyticSignalConverter:
    """Stateful real audio -> complex analytic signal converter.

    Delays the direct ("I") branch to match the FIR Hilbert transformer's
    group delay on the ("Q") branch, so the two stay time-aligned across
    successive process() calls (a plain scipy.signal.hilbert() on each
    chunk independently would introduce edge artifacts at every chunk
    boundary since it assumes each chunk is a whole periodic signal).
    """

    def __init__(self, numtaps: int = _HILBERT_TAPS) -> None:
        if not _SCIPY_AVAILABLE:
            raise RuntimeError("scipy is required for the audio Hilbert bridge")
        self._taps = _hilbert_fir_taps(numtaps)
        self._group_delay = (numtaps - 1) // 2
        self._hilbert_zi = np.zeros(numtaps - 1, dtype=np.float64)
        self._delay_buffer = np.zeros(self._group_delay, dtype=np.float32)

    def process(self, audio: NDArray[np.float32]) -> NDArray[np.complex64]:
        """Convert one chunk of real audio into a complex analytic signal
        at the same sample rate."""
        if len(audio) == 0:
            return np.array([], dtype=np.complex64)

        q, self._hilbert_zi = sp_signal.lfilter(
            self._taps, [1.0], audio.astype(np.float64), zi=self._hilbert_zi
        )
        delayed_i = np.concatenate([self._delay_buffer, audio])
        i = delayed_i[: len(audio)]
        self._delay_buffer = (
            delayed_i[-self._group_delay :] if self._group_delay else np.zeros(0, dtype=np.float32)
        )

        analytic: NDArray[np.complex64] = (i.astype(np.float32) + 1j * q.astype(np.float32)).astype(
            np.complex64
        )
        return analytic


class FrequencyShifter:
    """Stateful complex-exponential mixer with a continuously-running phase
    accumulator, so successive process() calls on chunked audio don't click
    at chunk boundaries."""

    def __init__(self, shift_hz: float, sample_rate: float) -> None:
        self._shift_hz = shift_hz
        self._sample_rate = sample_rate
        self._phase = 0.0

    def process(self, iq: NDArray[np.complex64]) -> NDArray[np.complex64]:
        if len(iq) == 0:
            return iq
        n = np.arange(len(iq))
        phase = self._phase + 2.0 * np.pi * self._shift_hz * n / self._sample_rate
        mixer = np.exp(1j * phase).astype(np.complex64)
        self._phase = float(
            (phase[-1] + 2.0 * np.pi * self._shift_hz / self._sample_rate) % (2.0 * np.pi)
        )
        result: NDArray[np.complex64] = (iq * mixer).astype(np.complex64)
        return result


class RxAudioBridge:
    """Real SSB/FM audio -> complex baseband IQ, ready for
    GmskDiscriminator.process() / Ax100DigiReceiver.push_samples()."""

    def __init__(self, sample_rate: float, shift_hz: float = DEFAULT_SHIFT_HZ) -> None:
        self._analytic = AnalyticSignalConverter()
        self._shifter = FrequencyShifter(shift_hz=-shift_hz, sample_rate=sample_rate)

    def process(self, audio: NDArray[np.float32]) -> NDArray[np.complex64]:
        analytic = self._analytic.process(audio)
        return self._shifter.process(analytic)


def synthesize_gmsk_audio(
    bits: NDArray[np.uint8],
    sample_rate: float,
    baud: float,
    deviation_hz: float,
    shift_hz: float = DEFAULT_SHIFT_HZ,
    bt: float = 0.3,
) -> NDArray[np.float32]:
    """Synthesize a Gaussian-shaped CPFSK (GMSK) waveform for `bits`
    (NRZ, 0/1 one value per symbol) and upshift it by `shift_hz` so it can
    be played into an SSB-mode transceiver's microphone/data input.

    `bt` is the Gaussian filter's bandwidth-time product (0.3 is GSM/many
    amateur GMSK implementations' common default; not yet confirmed
    against a real MARMOTSat/GreenCube transmission).
    """
    if not _SCIPY_AVAILABLE:
        raise RuntimeError("scipy is required for GMSK audio synthesis")
    if len(bits) == 0:
        return np.array([], dtype=np.float32)

    sps = max(1, round(sample_rate / baud))
    nrz = 2.0 * bits.astype(np.float64) - 1.0
    upsampled = np.repeat(nrz, sps)

    span_symbols = 4
    alpha = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    t = np.arange(-span_symbols * sps // 2, span_symbols * sps // 2 + 1) / sps
    kernel = np.exp(-(t**2) / (2 * alpha**2))
    kernel /= np.sum(kernel)
    shaped = np.convolve(upsampled, kernel, mode="same")

    freq = shaped * deviation_hz
    phase = 2.0 * np.pi * np.cumsum(freq) / sample_rate
    baseband = np.exp(1j * phase).astype(np.complex64)

    shifter = FrequencyShifter(shift_hz=shift_hz, sample_rate=sample_rate)
    shifted = shifter.process(baseband)
    result: NDArray[np.float32] = shifted.real.astype(np.float32)
    return result
