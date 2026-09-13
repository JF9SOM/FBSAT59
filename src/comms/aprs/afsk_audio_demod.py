"""Bell 202 1200bps AFSK audio recovery for SDR-fed Direwolf reception.

Produces properly demodulated, de-emphasized NFM audio -- the software
equivalent of a real radio's normal speaker/mic audio output, which is
exactly what Direwolf's built-in Bell 202 AFSK decoder (MODEM 1200) already
expects and already decodes correctly via a real radio + sound card. The
resulting 48kHz float32 PCM is fed to Direwolf's stdin exactly like real
soundcard audio, so Direwolf's own decoder does the actual demod / bit sync
/ HDLC framing -- this module only produces audio at the right bandwidth,
de-emphasis, and level for it.

This deliberately mirrors comms.aprs.g3ruh_demod's G3ruhDiscriminator /
G3ruhSdrDemod pair (same DC removal -> IF bandpass -> decimate -> phase-
difference discriminator -> rational resample to exactly 48kHz structure),
with two differences that matter for 1200 baud Bell 202 specifically:

  - De-emphasis IS applied here (G3RUH's 9600bps needs the raw, flat
    discriminator output instead -- see g3ruh_demod.py's docstring). Bell
    202 tones travel through a radio's normal voice audio path, which
    always includes de-emphasis, so the software path must match that.
  - The IF bandpass is sized for normal NBFM voice bandwidth (the same
    NFM_DEVIATION + 4kHz half-bandwidth src/sdr/demodulator.py's
    Demodulator._demod_nfm() already uses), not G3RUH's wider one.

Earlier attempts (2026-09-12/13) to decode 1200bps SDR reception with a
custom, from-scratch Python tone-detector + PLL + HDLC implementation
(afsk_demod.py, since removed) never reliably decoded a real signal
despite multiple DSP fixes, while the exact same tones decode correctly
via a real radio + sound card through Direwolf. Rather than continue
re-implementing what Direwolf's own AFSK decoder already does reliably,
this module instead gets the *audio* right and lets Direwolf do the
actual decoding, exactly like the already-working 9600bps G3RUH path
does -- confirmed decoding a real 1200bps signal live (2026-09-13).
"""

from __future__ import annotations

import math
import queue
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

_AUDIO_RATE = 48_000
_INTERMEDIATE_RATE_TARGET = 200_000
# Same NBFM voice deviation/IF-bandwidth assumption as
# sdr.demodulator.Demodulator._demod_nfm() (NFM_DEVIATION + 4kHz audio
# headroom) -- Bell 202 tones are relayed over a radio's normal voice
# channel, so the same channel bandwidth applies.
_DEVIATION_HZ = 5_000.0
_IF_HALF_BW_HZ = _DEVIATION_HZ + 4_000.0
# De-emphasis time constant, 75us (US standard) -- same as
# sdr.demodulator.NFM_DEEMPH_TAU. Unlike G3RUH's raw discriminator tap,
# Bell 202 audio must have this applied to match what a real radio's
# speaker/mic output (and thus Direwolf's already-working decoder) expects.
_DEEMPH_TAU_S = 75e-6


class AfskAudioDiscriminator:
    """Stateful de-emphasized NFM audio recovery, tuned for Bell 202 relay.

    Mirrors g3ruh_demod.py's G3ruhDiscriminator (DC removal -> IF bandpass
    -> decimate -> phase-difference FM discriminator -> exact-48kHz
    rational resample), adding the de-emphasis stage G3RUH's raw wideband
    tap deliberately skips.
    """

    def __init__(self, input_rate: float) -> None:
        self._input_rate = input_rate
        self._dc_zi_i = np.zeros(1, dtype=np.float32)
        self._dc_zi_q = np.zeros(1, dtype=np.float32)
        self._deemph_zi = np.zeros(1, dtype=np.float64)
        self._build_filters()

    def _build_filters(self) -> None:
        rate = self._input_rate

        alpha_dc = float(np.clip(1.0 - (2.0 * np.pi * 30.0 / rate), 0.0, 0.9999))
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -alpha_dc], dtype=np.float64)

        self._decim1 = max(1, int(rate / _INTERMEDIATE_RATE_TARGET))
        self._mid_rate = rate / self._decim1

        # Rational resampler to exactly _AUDIO_RATE -- see g3ruh_demod.py's
        # _build_filters() comment: naive integer-stride decimation lands on
        # whatever self._mid_rate/_AUDIO_RATE happens to round to (not
        # exactly 48000), which is a real-signal-confirmed ARATE mismatch
        # that breaks Direwolf's bit-clock recovery.
        mid_rate_int = max(1, int(round(self._mid_rate)))
        gcd = math.gcd(_AUDIO_RATE, mid_rate_int)
        self._resample_up = _AUDIO_RATE // gcd
        self._resample_down = mid_rate_int // gcd

        if_bw = float(np.clip(_IF_HALF_BW_HZ / (rate / 2.0), 0.001, 0.499))
        self._if_b = sp_signal.firwin(63, if_bw).astype(np.float32) if _SCIPY_AVAILABLE else None

        dt = 1.0 / self._mid_rate
        deemph_alpha = dt / (_DEEMPH_TAU_S + dt)
        self._deemph_b = np.array([deemph_alpha], dtype=np.float64)
        self._deemph_a = np.array([1.0, -(1.0 - deemph_alpha)], dtype=np.float64)

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

        iq_if = sp_signal.lfilter(self._if_b, [1.0], iq_dc)
        iq_ds = self._decimate(iq_if, self._decim1)
        if len(iq_ds) < 2:
            return np.array([], dtype=np.float32)

        prev = np.empty_like(iq_ds)
        prev[0] = iq_ds[0]
        prev[1:] = iq_ds[:-1]
        discrim = np.angle(iq_ds * np.conj(prev))

        audio_raw = discrim * (self._mid_rate / (2 * np.pi * _DEVIATION_HZ))
        audio_de, self._deemph_zi = sp_signal.lfilter(
            self._deemph_b, self._deemph_a, audio_raw, zi=self._deemph_zi
        )
        audio = sp_signal.resample_poly(audio_de, self._resample_up, self._resample_down)
        result: np.ndarray = np.clip(audio, -1.0, 1.0).astype(np.float32)
        return result

    @staticmethod
    def _decimate(x: np.ndarray, factor: int) -> np.ndarray:
        """Simple decimation by integer factor -- anti-aliasing is handled
        by the preceding IF bandpass filter, same rationale as
        sdr/demodulator.py's Demodulator._decimate()."""
        if factor <= 1:
            return x
        return x[::factor]


class AfskAudioSdrDemod(QThread):
    """Runs AfskAudioDiscriminator on an SDR pipeline's raw I/Q in a
    background thread, emitting ready-to-play 48kHz float32 PCM for
    Direwolf's stdin.

    Usage
    -----
    demod = AfskAudioSdrDemod(sample_rate=int(pipeline._device.sample_rate))
    demod.audio_ready.connect(my_pcm_consumer)
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()
    """

    audio_ready: Signal = Signal(object)

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._discriminator = AfskAudioDiscriminator(input_rate=sample_rate)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()
        # Diagnostic-only (see sdr.diag_log): counts blocks dropped because
        # this thread wasn't draining the queue fast enough.
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
                    "afsk_audio_demod queue full, dropped block (total drops=%d)",
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

                get_sdr_diag_logger().exception("AfskAudioSdrDemod.run(): process() raised")
                raise
            if len(audio):
                self.audio_ready.emit(audio)
