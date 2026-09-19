"""
Software demodulators for common amateur satellite modes.

All demodulators operate on complex64 numpy arrays (I/Q samples) and produce
float32 PCM audio arrays at the configured audio sample rate.

Supported modes:
  NFM   — Narrow FM (FM satellites, e.g. SO-50, AO-91)
  USB   — Upper Sideband (linear transponders)
  LSB   — Lower Sideband
  CW    — Morse code (direct decimation + BPF + envelope detection)
"""

from __future__ import annotations

import logging
import threading
from enum import Enum

import numpy as np

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

logger = logging.getLogger(__name__)

AUDIO_RATE: int = 48_000  # Output sample rate (Hz)
NFM_DEVIATION: float = 5_000.0  # Narrow FM deviation (Hz)
CW_PITCH_HZ: float = 600.0  # CW pitch reference (Hz) — kept for future use
SSB_BW_HZ: float = 2_700.0  # SSB audio bandwidth (Hz)
NFM_DEEMPH_TAU: float = 75e-6  # De-emphasis time constant (75 µs, US standard)


# Length of the IF / audio low-pass FIR filters built in _build_filters().
_FIR_TAPS: int = 63

# Low-pass applied before the final resample to AUDIO_RATE: keeps everything
# above ~20 kHz (SDR noise, adjacent channels) from folding into the audio band.
_AUDIO_AA_CUTOFF_HZ: float = 20_000.0


class _StrideDecimator:
    """Integer-factor decimation that keeps its sample phase across blocks.

    Blocks rarely hold a multiple of *factor* samples; a plain ``x[::factor]``
    per block would drop or repeat a sample at every block edge, so the
    effective rate would be off by up to ``factor / block`` (~0.1%).
    Anti-aliasing is left to the surrounding filters.
    """

    def __init__(self, factor: int) -> None:
        self._factor = max(1, factor)
        self._phase = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self._factor <= 1:
            return x
        start = self._phase
        self._phase = (start - len(x)) % self._factor
        return x[start :: self._factor]


class _StreamFilter:
    """FIR filter that keeps its delay line across blocks (real input)."""

    def __init__(self, taps: np.ndarray) -> None:
        self._b = taps
        self._zi = np.zeros(len(taps) - 1, dtype=np.float64)

    @classmethod
    def lowpass(cls, cutoff_hz: float, rate: float, numtaps: int = 63) -> _StreamFilter | None:
        """Low-pass at *cutoff_hz* for a stream at *rate*; None if it would not filter anything."""
        if cutoff_hz >= 0.45 * rate:
            return None
        return cls(sp_signal.firwin(numtaps, cutoff_hz, fs=rate))

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = sp_signal.lfilter(self._b, [1.0], x, zi=self._zi)
        result: np.ndarray = np.asarray(y, dtype=np.float32)
        return result


class _StreamResampler:
    """Streaming cubic (Catmull-Rom) resampler to exactly AUDIO_RATE.

    Integer-stride decimation can only reach AUDIO_RATE when the input
    rate happens to be an integer multiple of it; for the SDR rates in
    use it lands somewhere else (250 kS/s USB/CW came out at 62.5 kHz,
    NFM at 50 kHz) while everything downstream -- speaker, CW Decoder,
    FT4, Q65, SSTV, the MP3 recorder -- treats the samples as 48 kHz. This
    interpolates at the exact ratio instead, carrying the fractional
    position and the last three input samples from block to block so
    there is no seam at block boundaries. Works on real or complex data.

    The input must already be low-passed well below AUDIO_RATE / 2 (the
    interpolator does no anti-aliasing of its own); the demodulator does
    that just before calling this.
    """

    def __init__(self, in_rate: float, out_rate: float = float(AUDIO_RATE)) -> None:
        self._step = in_rate / out_rate  # input samples per output sample
        self._pos = 0.0  # next output's position, relative to the next block's x[0]
        self._tail: np.ndarray | None = None  # last 3 input samples

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x[:0]
        if self._tail is None:
            self._tail = np.zeros(3, dtype=x.dtype)
        arr = np.concatenate([self._tail, x])  # arr[j] holds x[j - 3]
        self._tail = arr[-3:].copy()
        # Outputs at u = pos + k*step need x[floor(u)-1 .. floor(u)+2], so
        # only those with u < n - 2 can be produced now; the rest wait for
        # the next block.
        count = int(np.ceil((n - 2 - self._pos) / self._step)) if n - 2 > self._pos else 0
        if count <= 0:
            self._pos -= n
            return x[:0]
        u = self._pos + np.arange(count) * self._step
        i = np.floor(u).astype(np.int64)
        f = (u - i).astype(np.float64)
        j = i + 3
        p0, p1, p2, p3 = arr[j - 1], arr[j], arr[j + 1], arr[j + 2]
        out = 0.5 * (
            2.0 * p1
            + (p2 - p0) * f
            + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * f**2
            + (3.0 * p1 - p0 - 3.0 * p2 + p3) * f**3
        )
        self._pos += count * self._step - n
        result: np.ndarray = out.astype(x.dtype)
        return result


class DemodMode(str, Enum):  # noqa: UP042
    """Available demodulation modes."""

    NFM = "NFM"
    USB = "USB"
    LSB = "LSB"
    CW = "CW"

    @classmethod
    def from_satnogs(cls, mode: str) -> DemodMode:
        """Map a SATNOGS mode string to the closest DemodMode."""
        m = mode.upper()
        if m in ("FM", "DIGITALVOICE", "AFSK"):
            return cls.NFM
        if m in ("SSB", "USB", "BPSK"):
            return cls.USB
        if m == "LSB":
            return cls.LSB
        if m in ("CW", "CW-R"):
            return cls.CW
        return cls.USB  # sensible default for linear transponders


class Demodulator:
    """
    Stateful I/Q → PCM demodulator.

    Usage:
        demod = Demodulator(input_rate=2.4e6)
        demod.set_mode(DemodMode.USB)
        pcm = demod.process(iq_samples)  # float32 array at AUDIO_RATE
    """

    def __init__(self, input_rate: float = 2.4e6) -> None:
        if not _SCIPY_AVAILABLE:
            raise ImportError(
                "scipy is required for SDR demodulation. "
                "Install it with: pip install 'fbsat59[sdr]'"
            )
        # Protects all filter coefficients and state from concurrent access.
        # set_mode/set_input_rate/set_* run on the Qt UI thread;
        # process() runs on the SDRPipeline QThread.  Without this lock,
        # _build_filters() can overwrite numpy arrays mid-read on Windows,
        # causing a crash in the scipy C extension.
        self._lock = threading.Lock()
        self._input_rate = input_rate
        self._mode = DemodMode.USB
        self._audio_gain: float = 1.0
        self._agc_enabled: bool = True
        self._agc_level: float = 1.0
        self._ssb_bw: float = SSB_BW_HZ
        self._cw_pitch: float = CW_PITCH_HZ
        self._fm_phase: float = 0.0  # accumulated FM demod phase
        # DC blocking IIR state (applied to I and Q separately before NFM,
        # and to real PCM output before SSB/CW to remove SDR DC offset hum)
        self._dc_zi_i: np.ndarray = np.zeros(1)
        self._dc_zi_q: np.ndarray = np.zeros(1)
        # Streaming filter state -- (re)initialised by _build_filters().
        self._nfm_if_zi: np.ndarray = np.zeros(0, dtype=np.complex128)
        self._nfm_prev: np.complex64 | None = None
        self._deemph_zi: np.ndarray = np.zeros(1)
        self._ssb_lpf_zi: np.ndarray = np.zeros(0)
        self._cw_bpf_zi: np.ndarray = np.zeros((0, 2))
        self._bfo_phase: float = 0.0
        self._build_filters()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_mode(self, mode: DemodMode) -> None:
        """Switch demodulation mode. Rebuilds filters."""
        with self._lock:
            self._mode = mode
            self._fm_phase = 0.0
            self._dc_zi_i = np.zeros(1)
            self._dc_zi_q = np.zeros(1)
            self._build_filters()

    def set_input_rate(self, rate: float) -> None:
        """Update the I/Q input sample rate and rebuild filters."""
        with self._lock:
            self._input_rate = rate
            self._fm_phase = 0.0
            self._dc_zi_i = np.zeros(1)
            self._dc_zi_q = np.zeros(1)
            self._build_filters()

    def set_audio_gain(self, gain: float) -> None:
        """Set a linear output gain (1.0 = unity)."""
        with self._lock:
            self._audio_gain = max(0.0, gain)

    def set_agc(self, enabled: bool) -> None:
        with self._lock:
            self._agc_enabled = enabled
            if not enabled:
                self._agc_level = 1.0

    def set_ssb_bandwidth(self, bw_hz: float) -> None:
        """Set SSB audio bandwidth and rebuild filters."""
        with self._lock:
            self._ssb_bw = max(500.0, min(bw_hz, 6_000.0))
            self._build_filters()

    def set_cw_pitch(self, pitch_hz: float) -> None:
        """Set CW sidetone pitch and rebuild BPF."""
        with self._lock:
            self._cw_pitch = max(200.0, min(pitch_hz, 1_500.0))
            self._build_filters()

    def process(self, iq: np.ndarray) -> np.ndarray:
        """
        Demodulate a block of complex64 I/Q samples.

        Returns float32 PCM at AUDIO_RATE.
        """
        if len(iq) == 0:
            return np.array([], dtype=np.float32)
        with self._lock:
            try:
                if self._mode == DemodMode.NFM:
                    return self._demod_nfm(iq)
                if self._mode == DemodMode.USB:
                    return self._demod_ssb(iq, upper=True)
                if self._mode == DemodMode.LSB:
                    return self._demod_ssb(iq, upper=False)
                if self._mode == DemodMode.CW:
                    return self._demod_cw(iq)
            except Exception:
                logger.exception("Demodulator.process error")
            return np.zeros(int(len(iq) * AUDIO_RATE / self._input_rate), dtype=np.float32)

    # ------------------------------------------------------------------
    # Demodulation internals
    # ------------------------------------------------------------------

    def _remove_dc(self, iq: np.ndarray) -> np.ndarray:
        """Remove DC offset from I and Q channels using a high-pass IIR filter.

        The HackRF (and most SDRs) produce a significant DC spike at the center
        frequency. Without this step, the DC component appears as a low-frequency
        hum in the audio output.
        """
        i_dc_raw, self._dc_zi_i = sp_signal.lfilter(
            self._dc_b, self._dc_a, iq.real.astype(np.float32), zi=self._dc_zi_i
        )
        q_dc_raw, self._dc_zi_q = sp_signal.lfilter(
            self._dc_b, self._dc_a, iq.imag.astype(np.float32), zi=self._dc_zi_q
        )
        i_dc = np.asarray(i_dc_raw, dtype=np.float32)
        q_dc = np.asarray(q_dc_raw, dtype=np.float32)
        return (i_dc + 1j * q_dc).astype(np.complex64)

    def _demod_nfm(self, iq: np.ndarray) -> np.ndarray:
        """Narrow FM demodulation via phase-difference method."""
        # Remove DC offset from the SDR first
        iq = self._remove_dc(iq)

        # Apply IF bandpass filter to limit bandwidth to ±(deviation + audio_bw)
        iq_if, self._nfm_if_zi = sp_signal.lfilter(self._nfm_if_b, [1.0], iq, zi=self._nfm_if_zi)

        # Downsample to intermediate rate
        iq_ds = self._fm_stride.process(iq_if)
        if len(iq_ds) == 0:
            return np.array([], dtype=np.float32)

        # Phase discriminator: arg(x[n] * conj(x[n-1])) -- the previous sample
        # of the first output comes from the end of the last block
        prev = np.empty_like(iq_ds)
        prev[0] = iq_ds[0] if self._nfm_prev is None else self._nfm_prev
        prev[1:] = iq_ds[:-1]
        self._nfm_prev = iq_ds[-1]
        discrim = np.angle(iq_ds * np.conj(prev))

        # Normalise by sample rate to get audio (deviation / rate)
        audio_raw = discrim * (self._fm_rate / (2 * np.pi * NFM_DEVIATION))

        # De-emphasis filter
        audio_de, self._deemph_zi = sp_signal.lfilter(
            self._deemph_b, self._deemph_a, audio_raw, zi=self._deemph_zi
        )

        return self._finalize(self._to_audio_rate(audio_de.real, self._fm_resampler, self._fm_aa))

    def _demod_ssb(self, iq: np.ndarray, upper: bool) -> np.ndarray:
        """
        SSB demodulation via complex mixing (Weaver / BFO injection) method.

        For USB: mix the I/Q signal by e^(-j*2π*f_bfo*t) to shift the upper
        sideband audio to baseband, apply real LPF, decimate, take real part.
        For LSB: conjugate the I/Q first to mirror the spectrum, then same.

        This correctly removes the DC spike and isolates one sideband.
        """
        # Remove DC offset (HackRF DC spike → 50 Hz hum without this)
        iq = self._remove_dc(iq)

        # Mirror spectrum for LSB (converts LSB → USB processing)
        if not upper:
            iq = np.conj(iq)

        # BFO injection: shift signal so the SSB audio sits at baseband.
        # We inject at SSB_BW/2 so the centre of the voice band lands at DC.
        # This means 300–2700 Hz voice → -1200 to +1200 Hz after mixing.
        # The oscillator's phase carries over from the previous block: restarting
        # it at every block boundary put a phase jump into the audio ~15 times a second.
        bfo_hz = self._ssb_bw / 2.0
        n = len(iq)
        step = 2.0 * np.pi * bfo_hz / self._input_rate
        mix = np.exp(-1j * (self._bfo_phase + step * np.arange(n))).astype(np.complex64)
        self._bfo_phase = float((self._bfo_phase + step * n) % (2.0 * np.pi))
        iq_mixed = iq * mix

        # Decimate to intermediate rate
        iq_ds = self._ssb_stride.process(iq_mixed)

        # Apply real LPF at SSB_BW to the real (I) channel
        audio_raw, self._ssb_lpf_zi = sp_signal.lfilter(
            self._ssb_audio_b, [1.0], iq_ds.real, zi=self._ssb_lpf_zi
        )

        # Resample to exactly AUDIO_RATE (already band-limited by the LPF above)
        return self._finalize(self._to_audio_rate(audio_raw, self._ssb_resampler, None))

    def _demod_cw(self, iq: np.ndarray) -> np.ndarray:
        """
        CW demodulation: direct decimation → wide BPF → output.

        SDR-based CW reception does NOT need envelope detection or sidetone
        synthesis.  The CW carrier sits at some audio-frequency offset from
        the SDR centre frequency.  After decimation and taking the real part,
        that carrier is already an audible tone (turns on/off with the key).
        Envelope detection of a bandpass-filtered noise floor produces a
        *constant* non-zero amplitude → AGC cranks it up → permanent hum,
        which is exactly what we want to avoid.

        We apply a moderately wide bandpass (300–3000 Hz) so the user has
        freedom to tune the satellite frequency without needing to hit an
        exact CW pitch offset.
        """
        # Remove DC offset (HackRF DC spike)
        iq = self._remove_dc(iq)

        # Decimate to the intermediate rate, then resample to exactly AUDIO_RATE
        iq_ds = self._cw_stride.process(iq)

        # Real part: CW tone appears at its natural carrier-offset frequency
        audio_raw = self._to_audio_rate(iq_ds.real, self._cw_resampler, self._cw_aa)

        # Wide BPF (300–3000 Hz) — SOS format for numerical stability
        audio_bp, self._cw_bpf_zi = sp_signal.sosfilt(
            self._cw_bpf_sos, audio_raw, zi=self._cw_bpf_zi
        )
        audio = audio_bp.astype(np.float32)
        return self._finalize(audio)

    def _to_audio_rate(
        self,
        x: np.ndarray,
        resampler: _StreamResampler,
        anti_alias: _StreamFilter | None,
    ) -> np.ndarray:
        """Low-pass *x* (if *anti_alias* is given) and resample it to AUDIO_RATE."""
        x = np.asarray(x, dtype=np.float32)
        if anti_alias is not None:
            x = anti_alias.process(x)
        return resampler.process(x).astype(np.float32)

    # ------------------------------------------------------------------
    # AGC and output
    # ------------------------------------------------------------------

    def _finalize(self, audio: np.ndarray) -> np.ndarray:
        """Apply AGC and output gain, clamp to [-1, 1]."""
        if len(audio) == 0:
            return audio
        if self._agc_enabled:
            peak = float(np.max(np.abs(audio)))
            if peak > 1e-6:
                target = 0.5
                alpha = 0.01  # slow attack/release
                self._agc_level = (1 - alpha) * self._agc_level + alpha * (target / peak)
            audio = audio * self._agc_level
        audio = audio * self._audio_gain
        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    # ------------------------------------------------------------------
    # Filter design
    # ------------------------------------------------------------------

    def _build_filters(self) -> None:
        """(Re)build all FIR/IIR filter coefficients for the current settings."""
        rate = self._input_rate

        # ---- DC blocking high-pass IIR (applied before all modes) ----
        # Single-pole HPF at 30 Hz to remove DC offset without affecting audio.
        # α = 1 - 2π * f_c / f_s  (first-order IIR high-pass)
        alpha_dc = 1.0 - (2.0 * np.pi * 30.0 / rate)
        alpha_dc = float(np.clip(alpha_dc, 0.0, 0.9999))
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -alpha_dc], dtype=np.float64)

        # Every filter below keeps its state from one block to the next (process()
        # gets ~65 ms slices). Restarting a filter from zero at each slice puts a
        # start-up transient at every block boundary, audible as a ~15 Hz buzz.
        # A filter rebuild (mode / rate / bandwidth change) starts them all afresh.
        self._nfm_if_zi = np.zeros(_FIR_TAPS - 1, dtype=np.complex128)
        self._nfm_prev = None
        self._deemph_zi = np.zeros(1, dtype=np.float64)
        self._ssb_lpf_zi = np.zeros(_FIR_TAPS - 1, dtype=np.float64)
        self._bfo_phase = 0.0

        # ---- NFM chain ----
        # Stage 1: decimate to ~200 kHz intermediate rate
        self._fm_decim = max(1, int(rate / 200_000))
        self._fm_rate = rate / self._fm_decim
        self._fm_stride = _StrideDecimator(self._fm_decim)
        # Stage 2: resample fm_rate → exactly AUDIO_RATE
        self._fm_resampler = _StreamResampler(self._fm_rate)
        self._fm_aa = _StreamFilter.lowpass(_AUDIO_AA_CUTOFF_HZ, self._fm_rate)

        # IF bandpass for NFM: pass ±(deviation + audio_bw) around centre.
        # Limits interference from strong out-of-band signals before decimation.
        nfm_if_bw = (NFM_DEVIATION + 4_000.0) / (rate / 2.0)
        nfm_if_bw = float(np.clip(nfm_if_bw, 0.001, 0.499))
        self._nfm_if_b = sp_signal.firwin(_FIR_TAPS, nfm_if_bw).astype(np.float32)

        # De-emphasis IIR (single pole low-pass, τ = 75 µs)
        dt = 1.0 / self._fm_rate
        alpha = dt / (NFM_DEEMPH_TAU + dt)
        self._deemph_b = np.array([alpha], dtype=np.float64)
        self._deemph_a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)

        # ---- SSB chain ----
        # Stage 1: decimate input to ~96 kHz
        self._ssb_decim = max(1, int(rate / 96_000))
        ssb_mid_rate = rate / self._ssb_decim
        self._ssb_stride = _StrideDecimator(self._ssb_decim)
        # Stage 2: resample to exactly AUDIO_RATE
        self._ssb_resampler = _StreamResampler(ssb_mid_rate)

        # Real LPF applied at ssb_mid_rate after BFO mixing.
        # Passes ±SSB_BW/2 (the mixed voice band) and rejects the image.
        nyq_mid = ssb_mid_rate / 2.0
        cutoff_mid = float(np.clip(self._ssb_bw / nyq_mid, 0.001, 0.499))
        self._ssb_audio_b = sp_signal.firwin(_FIR_TAPS, cutoff_mid).astype(np.float32)

        # ---- CW chain ----
        # CW uses a direct decimation path (bypasses SSB BFO injection).
        # input_rate → ~96 kHz (stride) → AUDIO_RATE (exact resample)
        self._cw_decim1 = max(1, int(rate / 96_000))
        cw_mid_rate = rate / self._cw_decim1
        self._cw_stride = _StrideDecimator(self._cw_decim1)
        self._cw_resampler = _StreamResampler(cw_mid_rate)
        self._cw_aa = _StreamFilter.lowpass(_AUDIO_AA_CUTOFF_HZ, cw_mid_rate)

        # CW BPF applied at AUDIO_RATE.
        # Wide passband (300–3000 Hz): the CW tone sits at its natural carrier
        # offset, so the user can tune freely without hitting a fixed pitch.
        # SOS format is used — b,a Butterworth at even moderate bandwidths
        # can be numerically ill-conditioned at higher filter orders.
        nyq_audio = AUDIO_RATE / 2
        self._cw_bpf_sos = sp_signal.butter(
            4, [300.0 / nyq_audio, 3000.0 / nyq_audio], btype="band", output="sos"
        )
        self._cw_bpf_zi = np.zeros((self._cw_bpf_sos.shape[0], 2), dtype=np.float64)
