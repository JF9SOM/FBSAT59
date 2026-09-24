"""G3RUH 9600bps raw FM discriminator for SDR-fed Direwolf reception.

Produces the *raw* (no de-emphasis, wideband) FM-discriminator audio a
9600bps G3RUH AX.25 signal needs — the software equivalent of tapping a
radio's "DATA" port (pre-de-emphasis discriminator output) rather than its
normal speaker/mic audio path. The resulting 48kHz float32 PCM is fed to
Direwolf's stdin exactly like real soundcard audio, so Direwolf's own
built-in G3RUH decoder (MODEM 9600) does the actual demod / descramble /
clock recovery — this module only produces audio at the right bandwidth
and level for it.

The discriminator is the same phase-difference technique as
sdr/demodulator.py's NFM path; field-verified against a real recorded
9600bps G3RUH signal (JAPRS digi network) and confirmed decoding live
(2026-09-12/13). Its tuning is per baud rate (see DiscriminatorProfile): 9600 and 4800
use a narrow IF chosen from decode-rate measurements on synthetic frames
through Direwolf; other rates keep the original wide IF. All filters keep
their state across process() calls -- see G3ruhDiscriminator.

G3ruhSdrDemod subscribes to raw I/Q directly (SDRPipeline.subscribe()),
independent of the SDR Control tab's Mode combo / shared Demodulator —
the same approach comms.aprs.afsk_audio_demod.AfskAudioSdrDemod uses (the
1200bps counterpart, which adds de-emphasis) — so it can run alongside
another demod mode (e.g. CW Decoder) on the same SDR pipeline.
"""

from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

_AUDIO_RATE = 48_000
_INTERMEDIATE_RATE_TARGET = 200_000
# Legacy (pre-2026-09-19) discriminator parameters, still used for any baud
# rate without a _PROFILES entry: the assumed peak FM deviation (same order as typical
# NFM voice satellite links) and the wide IF half-bandwidth that followed
# from it. Kept separate from sdr/demodulator.py's NFM_DEVIATION so they can
# be tuned independently.
_DEVIATION_HZ = 5_000.0
_IF_HALF_BW_HZ = _DEVIATION_HZ + 8_000.0


@dataclass(frozen=True)
class DiscriminatorProfile:
    """Discriminator tuning for one baud rate.

    if_half_bw_hz   IF low-pass cutoff (one-sided) applied before the FM
                    discriminator. This sets how much noise reaches the
                    discriminator, whose output noise grows quickly once the
                    IF is much wider than the signal.
    if_taps         FIR length of that IF filter (longer = sharper skirts).
    post_lp_hz      Low-pass on the discriminator output (None = none).
    full_scale_hz   Instantaneous frequency that maps to audio +/-1.0; also
                    the clipping headroom against a carrier frequency error.
    deemph_tau_s    Single-pole de-emphasis time constant applied to the
                    discriminator output (None = flat, as a radio's DATA
                    port gives for G3RUH). Bell 202 relayed through a
                    radio's voice path needs it -- see afsk_audio_demod.py.
    clamp_hz        Limit the discriminator output to +/- this many Hz before
                    de-emphasis / the output low-pass (None = no limiting).
                    Below the FM threshold the discriminator emits sharp
                    spikes ("clicks") far larger than any real modulation;
                    limiting them near the true deviation keeps them from
                    swamping the tone detector. Only valid when the real
                    deviation is known to be small (satellite links).
    env_weight      Fade the discriminator output where the IF envelope drops
                    below ``env_weight`` x its 10 ms mean (None = off). The
                    clicks occur exactly at those envelope dips, so this
                    removes most of them without touching the clean signal.
    disc_rate_hz    Decimate to about this rate before the discriminator
                    (None = run it at the intermediate rate). A slower
                    discriminator averages the per-sample phase noise.
    """

    if_half_bw_hz: float
    if_taps: int
    post_lp_hz: float | None
    full_scale_hz: float
    deemph_tau_s: float | None = None
    clamp_hz: float | None = None
    env_weight: float | None = None
    disc_rate_hz: float | None = None


_LEGACY_PROFILE = DiscriminatorProfile(_IF_HALF_BW_HZ, 63, None, _DEVIATION_HZ)
# 9600 baud G3RUH FSK/GMSK (occupied bandwidth roughly +/-6 kHz). Measured on
# synthetic 9600 baud AX.25 frames through Direwolf's own decoder (see
# docs/communications.md, "9600bps G3RUH の感度改善"): narrowing the IF from
# +/-13 kHz to +/-7.5 kHz turns a 12 dB SNR (11 kHz band) signal from 0/40
# frames decoded into ~38/40, while still tolerating a +/-2 kHz frequency
# error.
# 4800 baud GMSK/FSK (occupied bandwidth roughly +/-3.5..5 kHz depending on the
# deviation): the same measurement on synthetic 4800 baud frames showed the old
# +/-13 kHz IF needed ~16 dB SNR (5.5 kHz band); +/-4.5 kHz plus a 3.5 kHz
# post-discriminator low-pass decodes down to ~12 dB and still tolerates a
# +/-1.5 kHz frequency error (a narrower IF is a little more sensitive but
# loses that tolerance).
_PROFILES: dict[int, DiscriminatorProfile] = {
    9600: DiscriminatorProfile(7_500.0, 255, 6_500.0, 8_000.0),
    4800: DiscriminatorProfile(4_500.0, 255, 3_500.0, 8_000.0),
}


def _profile_for(baud: int) -> DiscriminatorProfile:
    return _PROFILES.get(baud, _LEGACY_PROFILE)


class G3ruhDiscriminator:
    """Stateful raw-discriminator DSP.

    Mirrors sdr/demodulator.py's Demodulator._demod_nfm() (DC removal → IF
    bandpass → decimate → phase-difference FM discriminator → decimate to
    48kHz), but skips the de-emphasis stage NFM applies for voice — 9600bps
    G3RUH needs the flat, wideband discriminator output instead.

    Every filter carries its state from one process() call to the next.
    Blocks arrive from SDRPipeline as independent 16384-sample slices
    (~65 ms), and a filter restarted from zero at each slice puts a
    start-up transient at every block boundary -- which corrupts any frame
    longer than the block (a 9600 baud burst of ~170 ms spans two or three
    boundaries).
    """

    def __init__(
        self,
        input_rate: float,
        baud: int = 9600,
        profile: DiscriminatorProfile | None = None,
    ) -> None:
        self._input_rate = input_rate
        self._profile = profile if profile is not None else _profile_for(baud)
        self._dc_zi_i = np.zeros(1, dtype=np.float32)
        self._dc_zi_q = np.zeros(1, dtype=np.float32)
        self._build_filters()
        self._reset_stream_state()

    @property
    def full_scale_hz(self) -> float:
        """Instantaneous frequency that maps to audio +/-1.0."""
        return self._profile.full_scale_hz

    def _reset_stream_state(self) -> None:
        self._aa_zi: np.ndarray | None = None
        self._if_zi: np.ndarray | None = None
        self._post_zi: np.ndarray | None = None
        self._deemph_zi = np.zeros(1, dtype=np.float64)
        self._decim_phase = 0
        self._decim2_phase = 0
        self._env_zi: np.ndarray | None = None
        self._last_if: np.complex64 | None = None

    def _build_filters(self) -> None:
        rate = self._input_rate
        prof = self._profile

        alpha_dc = float(np.clip(1.0 - (2.0 * np.pi * 30.0 / rate), 0.0, 0.9999))
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -alpha_dc], dtype=np.float64)

        self._decim1 = max(1, int(rate / _INTERMEDIATE_RATE_TARGET))
        self._mid_rate = rate / self._decim1

        # Final stage to exactly _AUDIO_RATE via a rational resampler,
        # rather than the naive integer-stride decimation this used to do
        # (self._mid_rate / _AUDIO_RATE, e.g. 250000/1 / 48000 = 5.2,
        # truncated to a stride of 5 -- landing on an *actual* output rate
        # of 50000 Hz while direwolf.py's config still declares ARATE 48000
        # to Direwolf. That's a ~4% clock/timebase lie fed straight into
        # Direwolf's own G3RUH bit-clock recovery, for every SDR sample
        # rate this constant combination could ever produce -- confirmed
        # against a real captured 9600bps G3RUH signal, where Direwolf's
        # own reference decoder could not lock at all until this was
        # fixed). math.gcd() picks the smallest up/down pair that lands on
        # _AUDIO_RATE exactly, whatever self._mid_rate rounds to.
        # Optional extra decimation after the IF filter (see disc_rate_hz).
        self._post_decim = (
            max(1, int(self._mid_rate / prof.disc_rate_hz)) if prof.disc_rate_hz else 1
        )
        self._disc_rate = self._mid_rate / self._post_decim
        self._env_len = max(1, int(round(0.010 * self._disc_rate)))
        mid_rate_int = max(1, int(round(self._disc_rate)))
        gcd = math.gcd(_AUDIO_RATE, mid_rate_int)
        self._resample_up = _AUDIO_RATE // gcd
        self._resample_down = mid_rate_int // gcd

        self._deemph_b: np.ndarray | None = None
        if not _SCIPY_AVAILABLE:
            self._aa_b = self._if_b = self._post_b = None
            self._deemph_a = np.array([1.0], dtype=np.float64)
            return
        # Stage 1 (only when decimating): cheap anti-alias filter at the
        # input rate. The sharp IF filter below then runs at the much lower
        # intermediate rate, so a long FIR stays affordable even at 2.4 Msps.
        self._aa_b = (
            sp_signal.firwin(63, 0.25 * self._mid_rate, fs=rate).astype(np.float32)
            if self._decim1 > 1
            else None
        )
        # Stage 2: the IF filter proper, at the intermediate rate.
        if_bw = float(np.clip(prof.if_half_bw_hz / (self._mid_rate / 2.0), 0.001, 0.499))
        self._if_b = sp_signal.firwin(prof.if_taps, if_bw).astype(np.float32)
        self._post_b = (
            sp_signal.firwin(101, prof.post_lp_hz, fs=self._disc_rate).astype(np.float32)
            if prof.post_lp_hz is not None
            else None
        )
        if prof.deemph_tau_s is not None:
            dt = 1.0 / self._disc_rate
            alpha = dt / (prof.deemph_tau_s + dt)
            self._deemph_b = np.array([alpha], dtype=np.float64)
            self._deemph_a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
        else:
            self._deemph_b = None
            self._deemph_a = np.array([1.0], dtype=np.float64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Demodulate one I/Q block. Returns float32 PCM at 48kHz (possibly empty)."""
        if len(iq) == 0 or not _SCIPY_AVAILABLE or self._if_b is None:
            return np.array([], dtype=np.float32)

        i_dc_raw, self._dc_zi_i = sp_signal.lfilter(
            self._dc_b, self._dc_a, iq.real.astype(np.float32), zi=self._dc_zi_i
        )
        q_dc_raw, self._dc_zi_q = sp_signal.lfilter(
            self._dc_b, self._dc_a, iq.imag.astype(np.float32), zi=self._dc_zi_q
        )
        iq_dc = (
            np.asarray(i_dc_raw, dtype=np.float32) + 1j * np.asarray(q_dc_raw, dtype=np.float32)
        ).astype(np.complex64)

        if self._aa_b is not None:
            if self._aa_zi is None:
                self._aa_zi = np.zeros(len(self._aa_b) - 1, dtype=np.complex64)
            iq_dc, self._aa_zi = sp_signal.lfilter(self._aa_b, [1.0], iq_dc, zi=self._aa_zi)
        iq_ds = self._decimate(iq_dc, self._decim1)
        if len(iq_ds) == 0:
            return np.array([], dtype=np.float32)

        if self._if_zi is None:
            self._if_zi = np.zeros(len(self._if_b) - 1, dtype=np.complex64)
        iq_if, self._if_zi = sp_signal.lfilter(self._if_b, [1.0], iq_ds, zi=self._if_zi)
        iq_if = self._decimate_post(iq_if)
        if len(iq_if) == 0:
            return np.array([], dtype=np.float32)

        prev = np.empty_like(iq_if)
        prev[0] = iq_if[0] if self._last_if is None else self._last_if
        prev[1:] = iq_if[:-1]
        self._last_if = iq_if[-1]
        discrim = np.angle(iq_if * np.conj(prev))

        # No de-emphasis here (unlike NFM voice) — 9600bps G3RUH needs the
        # raw, flat discriminator output, same as a radio's DATA port.
        audio_raw = discrim * (self._disc_rate / (2 * np.pi * self._profile.full_scale_hz))
        if self._profile.env_weight:
            audio_raw = audio_raw * self._envelope_weight(iq_if)
        if self._profile.clamp_hz:
            limit = self._profile.clamp_hz / self._profile.full_scale_hz
            audio_raw = np.clip(audio_raw, -limit, limit)
        if self._deemph_b is not None:
            audio_raw, self._deemph_zi = sp_signal.lfilter(
                self._deemph_b, self._deemph_a, audio_raw, zi=self._deemph_zi
            )
        if self._post_b is not None:
            if self._post_zi is None:
                self._post_zi = np.zeros(len(self._post_b) - 1, dtype=np.float64)
            audio_raw, self._post_zi = sp_signal.lfilter(
                self._post_b, [1.0], audio_raw, zi=self._post_zi
            )
        audio = sp_signal.resample_poly(audio_raw, self._resample_up, self._resample_down)
        result: np.ndarray = np.clip(audio, -1.0, 1.0).astype(np.float32)
        return result

    def _envelope_weight(self, iq_if: np.ndarray) -> np.ndarray:
        """Per-sample weight in [0, 1] that fades the discriminator at envelope dips.

        ``weight = min(1, (|x| / (env_weight * mean|x| over ~10 ms))^2)``; the
        mean carries its filter state across blocks.
        """
        env = np.abs(iq_if).astype(np.float64)
        if self._env_zi is None:
            self._env_zi = np.zeros(self._env_len - 1, dtype=np.float64)
        mean, self._env_zi = sp_signal.lfilter(
            np.full(self._env_len, 1.0 / self._env_len), [1.0], env, zi=self._env_zi
        )
        weight = np.minimum(
            1.0, (env / (float(self._profile.env_weight or 1.0) * mean + 1e-9)) ** 2
        )
        return np.asarray(weight)

    def _decimate_post(self, x: np.ndarray) -> np.ndarray:
        """Second integer decimation (after the IF filter), phase kept across blocks."""
        factor = self._post_decim
        if factor <= 1:
            return x
        start = self._decim2_phase
        self._decim2_phase = (start - len(x)) % factor
        return x[start::factor]

    def _decimate(self, x: np.ndarray, factor: int) -> np.ndarray:
        """Integer-factor decimation that keeps its sample phase across blocks.

        Anti-aliasing is handled by the preceding stage-1 filter, same
        rationale as sdr/demodulator.py's Demodulator._decimate(). Blocks
        rarely hold a multiple of *factor* samples, so a plain ``x[::factor]``
        per block would drop or repeat a sample at every block edge.
        """
        if factor <= 1:
            return x
        start = self._decim_phase
        self._decim_phase = (start - len(x)) % factor
        return x[start::factor]


class G3ruhSdrDemod(QThread):
    """Runs G3ruhDiscriminator on an SDR pipeline's raw I/Q in a background
    thread, emitting ready-to-play 48kHz float32 PCM for Direwolf's stdin.

    Usage
    -----
    demod = G3ruhSdrDemod(sample_rate=int(pipeline._device.sample_rate), baud=9600)
    demod.audio_ready.connect(my_pcm_consumer)
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()
    """

    audio_ready: Signal = Signal(object)

    def __init__(self, sample_rate: int, parent: Any = None, baud: int = 9600) -> None:
        super().__init__(parent)
        self._discriminator = G3ruhDiscriminator(input_rate=sample_rate, baud=baud)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()
        # Diagnostic-only (see sdr.diag_log): counts blocks dropped because
        # this thread wasn't draining the queue fast enough. Logged on the
        # first drop and every 50th thereafter so a sustained backlog is
        # still visible without flooding the log.
        self._diag_drop_count: int = 0

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe().

        Safe to call from any thread; drops the block if the internal queue
        is full (i.e. this thread is not keeping up) rather than blocking
        the SDR pipeline's own thread.
        """
        try:
            self._q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            self._diag_drop_count += 1
            if self._diag_drop_count == 1 or self._diag_drop_count % 50 == 0:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().info(
                    "g3ruh_demod queue full, dropped block (total drops=%d)",
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
                audio = self._discriminator.process(iq)
            except Exception:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().exception("G3ruhSdrDemod.run(): process() raised")
                raise
            if len(audio):
                self.audio_ready.emit(audio)
