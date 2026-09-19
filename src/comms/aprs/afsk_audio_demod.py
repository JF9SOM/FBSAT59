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
  - The IF low-pass is sized for a Bell 202 NBFM signal (narrower than the
    voice-oriented NFM_DEVIATION + 4kHz src/sdr/demodulator.py's
    Demodulator._demod_nfm() uses -- see _AFSK_PROFILE).

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

import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from comms.aprs.g3ruh_demod import DiscriminatorProfile, G3ruhDiscriminator

# NBFM voice-style deviation assumption (same as
# sdr.demodulator.Demodulator._demod_nfm()): audio full-scale is +/-5 kHz.
_DEVIATION_HZ = 5_000.0
# De-emphasis time constant, 75us (US standard) -- same as
# sdr.demodulator.NFM_DEEMPH_TAU. Unlike G3RUH's raw discriminator tap,
# Bell 202 audio must have this applied to match what a real radio's
# speaker/mic output (and thus Direwolf's already-working decoder) expects.
_DEEMPH_TAU_S = 75e-6
# IF half-bandwidth: measured on synthetic Bell 202 AX.25 frames (FM deviation
# +/-3 and +/-5 kHz) through Direwolf's own decoder (see docs/communications.md,
# "1200bps AFSK の感度改善"). The former +/-9 kHz (a 63-tap filter whose skirts
# were much wider still) let in far more noise than the signal needs; +/-6 kHz
# is the most sensitive but fails for a +/-5 kHz deviation carrier that is
# 2 kHz off frequency, while +/-7 kHz tolerates +/-2 kHz for both deviations
# and is ~2 dB more sensitive than +/-9 kHz. A post-discriminator low-pass and
# a larger audio full-scale made no measurable difference here.
_AFSK_PROFILE = DiscriminatorProfile(
    if_half_bw_hz=7_000.0,
    if_taps=255,
    post_lp_hz=None,
    full_scale_hz=_DEVIATION_HZ,
    deemph_tau_s=_DEEMPH_TAU_S,
)


class AfskAudioDiscriminator(G3ruhDiscriminator):
    """Stateful de-emphasized NFM audio recovery, tuned for Bell 202 relay.

    The same streaming chain as G3ruhDiscriminator (DC removal -> IF
    low-pass -> decimate -> phase-difference FM discriminator -> exact-48kHz
    rational resample, every filter keeping its state across blocks), plus
    the de-emphasis stage G3RUH's raw wideband tap deliberately skips.
    """

    def __init__(self, input_rate: float, profile: DiscriminatorProfile | None = None) -> None:
        super().__init__(input_rate, profile=profile if profile is not None else _AFSK_PROFILE)


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
