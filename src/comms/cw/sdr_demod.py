"""Dedicated CW demodulator for the CW Decoder tab's SDR input.

The tab used to listen to SDRPipeline.audio_ready, i.e. to whatever the SDR
Control tab's demodulation mode happened to produce.  That mode defaults to
USB, in which a CW carrier sitting below the tuned frequency (e.g. ARICA-2 at
-927 Hz from its Doppler-corrected nominal frequency) is rejected entirely, so
the decoder heard only interference.  CwSdrDemod instead subscribes to the
pipeline's raw (Doppler- and Offset-corrected) I/Q and runs its own CW-mode
Demodulator, so decoding no longer depends on the SDR Control mode, volume or
AGC settings, and the speaker path is left alone.

The output is exactly SDR_AUDIO_RATE (48 kHz) PCM, which is also the rate the
tab must tell the decoder the samples are at.  Structure mirrors
comms.aprs.afsk_audio_demod.AfskAudioSdrDemod: push_samples() only enqueues
(it runs on the SDR pipeline thread and must never block it), a private
QThread does the DSP.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from sdr.demodulator import AUDIO_RATE, DemodMode, Demodulator

logger = logging.getLogger(__name__)

# Rate of the PCM emitted by CwSdrDemod (the SDR Demodulator's fixed output rate).
SDR_AUDIO_RATE: int = AUDIO_RATE

# Bound on queued I/Q blocks (SDRPipeline blocks are 16384 samples).
_QUEUE_BLOCKS = 128


class CwSdrDemod(QThread):
    """Runs a CW-mode Demodulator on an SDR pipeline's raw I/Q in a thread.

    Usage
    -----
    demod = CwSdrDemod(sample_rate=int(pipeline._device.sample_rate))
    demod.audio_ready.connect(my_pcm_consumer)
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()
    """

    audio_ready: Signal = Signal(object)  # float32 PCM at SDR_AUDIO_RATE

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        # Raises ImportError when scipy is missing (Demodulator requires it).
        self._demodulator = Demodulator(input_rate=float(sample_rate))
        self._demodulator.set_mode(DemodMode.CW)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=_QUEUE_BLOCKS)
        self._stop_event = threading.Event()
        self._drop_count: int = 0

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe().

        Safe to call from any thread; drops the block if the internal queue
        is full (this thread is not keeping up) rather than blocking the SDR
        pipeline's own thread.
        """
        try:
            self._q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            self._drop_count += 1
            if self._drop_count == 1 or self._drop_count % 50 == 0:
                logger.warning(
                    "CW demodulator queue full, dropped I/Q block (total drops=%d)",
                    self._drop_count,
                )

    def stop(self) -> None:
        """Stop the thread and wait for it to finish."""
        self._stop_event.set()
        self.wait(3000)

    def run(self) -> None:
        """Demodulate queued I/Q blocks until stop() is called."""
        while not self._stop_event.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            audio = self._demodulator.process(iq)
            if len(audio):
                self.audio_ready.emit(audio)
