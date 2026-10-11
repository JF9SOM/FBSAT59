"""Dedicated FM demodulator for the SSTV tab's SDR input.

The tab used to take the SDR pipeline's shared audio (SDRPipeline.audio_ready), which
is made by the general-purpose narrow-FM demodulator of the listening path. That is
tuned for voice, not for a picture whose tones sit at 1200-2300 Hz:

- its IF filter is a 63-tap FIR with a 9 kHz cutoff at 250 kS/s -- a transition band
  of well over 10 kHz, so a lot of out-of-channel noise reaches the discriminator,
  and the clicks of an FM receiver near its threshold grow with the IF bandwidth;
- a 75 us de-emphasis is always applied, which dulls the 2 kHz end of the picture
  tones (lower SNR on the bright pixels);
- the output is clipped to +-1.

On a real UmKA-1 Robot36 pass the picture was readable in its strong upper part and
a green speckle below; the same I/Q through the chain here gives a clean picture
(see docs/communications.md). SstvSdrDemod therefore subscribes to the pipeline's raw
(Doppler- and offset-corrected) I/Q like CwSdrDemod does, and the speaker path and
the other consumers are left alone.

Chain: (cheap decimation to ~200 kS/s when the SDR runs faster) -> 301-tap IF low-pass
at 6 kHz (Carson's rule for the +-3 kHz deviation of satellite SSTV and 2.3 kHz audio
is about +-5.3 kHz) -> decimation to ~50 kS/s -> phase discriminator. The output is
not clipped and has no de-emphasis or AGC (the decoder only reads frequencies).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

try:
    from scipy import signal as _sp

    _SCIPY: bool = True
except ImportError:
    _sp = None
    _SCIPY = False

logger = logging.getLogger(__name__)

# IF filter: cutoff and length (301 taps at ~250 kS/s: transition band ~3 kHz)
IF_CUTOFF_HZ: float = 6_000.0
IF_TAPS: int = 301
# Cheap first decimation brings a fast SDR (1-3 MS/s) down to about this rate
_STAGE1_TARGET_RATE: int = 200_000
_STAGE1_CUTOFF_HZ: float = 50_000.0
_STAGE1_TAPS: int = 63
# The discriminator output rate (decoder input rate) is the nearest to this from above/below
_OUTPUT_TARGET_RATE: int = 50_000
# Output scaling: a +-5 kHz deviation is +-1.0 (amplitude does not matter to the decoder)
_DEVIATION_HZ: float = 5_000.0

# Bound on queued I/Q blocks (SDRPipeline blocks are 16384 samples).
_QUEUE_BLOCKS = 128


def output_rate_for(input_rate: int) -> int:
    """The audio rate SstvFmDemod produces for an SDR running at *input_rate* (Hz)."""
    decim1 = max(1, int(input_rate // _STAGE1_TARGET_RATE))
    rate1 = input_rate / decim1
    decim2 = max(1, int(rate1 // _OUTPUT_TARGET_RATE))
    return int(round(rate1 / decim2))


class SstvFmDemod:
    """Streaming I/Q -> FM audio for SSTV (no threads; process() any block size)."""

    def __init__(self, input_rate: int) -> None:
        if not _SCIPY:
            raise ImportError("scipy is required for the SSTV FM demodulator")
        self._decim1 = max(1, int(input_rate // _STAGE1_TARGET_RATE))
        rate1 = input_rate / self._decim1
        self._decim2 = max(1, int(rate1 // _OUTPUT_TARGET_RATE))
        self._rate = rate1 / self._decim2
        self._pos1 = 0  # input samples seen by stage 1 (stride phase)
        self._pos2 = 0
        self._taps1 = (
            _sp.firwin(_STAGE1_TAPS, _STAGE1_CUTOFF_HZ, fs=input_rate) if self._decim1 > 1 else None
        )
        self._zi1 = np.zeros(_STAGE1_TAPS - 1, dtype=np.complex128) if self._decim1 > 1 else None
        self._taps2 = _sp.firwin(IF_TAPS, IF_CUTOFF_HZ, fs=rate1)
        self._zi2 = np.zeros(IF_TAPS - 1, dtype=np.complex128)
        self._prev = 0j
        self._scale = self._rate / (2.0 * np.pi * _DEVIATION_HZ)

    @property
    def output_rate(self) -> int:
        """Sample rate of the audio process() returns (Hz)."""
        return int(round(self._rate))

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Demodulate a block of complex I/Q; returns float32 audio at output_rate."""
        x = np.asarray(iq, dtype=np.complex128)
        if self._taps1 is not None:
            x, self._zi1 = _sp.lfilter(self._taps1, [1.0], x, zi=self._zi1)
            first = (-self._pos1) % self._decim1
            self._pos1 += len(x)
            x = x[first :: self._decim1]
        y, self._zi2 = _sp.lfilter(self._taps2, [1.0], x, zi=self._zi2)
        first = (-self._pos2) % self._decim2
        self._pos2 += len(y)
        y = y[first :: self._decim2]
        if len(y) == 0:
            return np.zeros(0, dtype=np.float32)
        prev = np.concatenate(([self._prev], y[:-1]))
        self._prev = y[-1]
        audio: np.ndarray = (np.angle(y * np.conj(prev)) * self._scale).astype(np.float32)
        return audio


class SstvSdrDemod(QThread):
    """Runs an SstvFmDemod on an SDR pipeline's raw I/Q in a private thread.

    Usage
    -----
    demod = SstvSdrDemod(sample_rate=int(pipeline._device.sample_rate))
    demod.audio_ready.connect(decoder.push_samples)   # at demod.output_rate
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()

    push_samples() only enqueues (it runs on the SDR pipeline thread and must never
    block it); the DSP runs here.
    """

    audio_ready: Signal = Signal(object)  # float32 audio at output_rate

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._demod = SstvFmDemod(int(sample_rate))
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=_QUEUE_BLOCKS)
        self._stop_event = threading.Event()
        self._drop_count: int = 0

    @property
    def output_rate(self) -> int:
        """Sample rate of the emitted audio (Hz): tell the decoder this."""
        return self._demod.output_rate

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe() (any thread; never blocks)."""
        try:
            self._q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            self._drop_count += 1
            if self._drop_count == 1 or self._drop_count % 50 == 0:
                logger.warning(
                    "SSTV demodulator queue full, dropped I/Q block (total drops=%d)",
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
            try:
                audio = self._demod.process(iq)
            except Exception:
                logger.exception("SSTV demodulator error")
                continue
            if len(audio):
                self.audio_ready.emit(audio)
