"""Sideband-correct USB audio at 12 kHz from an SDR's complex baseband.

FT4 and Q65 are audio-tone modes: a station's signal is a set of tones
between roughly 200 and 3000 Hz above the *dial* frequency, and their
decoders (WSJT-X's libft4wsjt / libq65) take 12 kHz real audio in which a
tone sits at exactly that audio frequency. A radio's USB demodulator makes
such audio; the SDR pipeline's own demodulator (sdr/demodulator.py) does
not serve here -- its audio is at the wrong sample rate for these decoders,
and its SSB mode moves everything down by half the SSB bandwidth (a 2000 Hz
tone comes out at 650 Hz).

This module takes the SDR's I/Q instead (SDRPipeline.subscribe(), after
Doppler correction, so 0 Hz is the tuned dial frequency) and produces the
audio a receiver in USB mode would:

    I/Q  --anti-alias + decimate-->  ~50 kS/s
         --mix -1750 Hz, low-pass +-1900 Hz-->  the 0..3500 Hz sideband, centred
         --resample-->  12 kS/s
         --mix +1750 Hz, take the real part-->  audio, tone at its true frequency

The complex low-pass rejects the opposite sideband, so unlike taking
``iq.real`` there is no mirror image. Every filter, oscillator and the
resampler keep their state between blocks, so block boundaries leave no seams.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

import numpy as np

from sdr.demodulator import (  # noqa: F401 -- helpers re-exported for the tests
    SidebandExtractor,
    _decimation_factors,
    _smooth_decimation,
)

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

logger = logging.getLogger(__name__)

OUT_RATE: int = 12_000  # what libft4wsjt / libq65 require

# The wanted sideband is 0..~3500 Hz. It is mixed down by this much so it sits
# symmetrically around 0 Hz for the low-pass, then back up afterwards.
_BAND_CENTRE_HZ: float = 1_750.0
_BAND_HALF_WIDTH_HZ: float = 1_900.0


# Output level: the SDR's I/Q amplitude depends on its gain (typically a few
# 1e-3 of full scale), while the decoders want audio at a usable level. A slow
# AGC brings the average power to _TARGET_RMS.
_TARGET_RMS: float = 0.1
_AGC_TAU_S: float = 4.0
_MAX_GAIN: float = 1e6


class UsbAudio12k:
    """Stateful I/Q -> 12 kHz USB audio converter (see the module docstring)."""

    def __init__(self, input_rate: float) -> None:
        if not _SCIPY_AVAILABLE:
            raise ImportError("scipy is required for SDR USB audio extraction")
        self._extractor = SidebandExtractor(
            float(input_rate), float(OUT_RATE), _BAND_CENTRE_HZ, _BAND_HALF_WIDTH_HZ
        )
        self._agc_power: float | None = None

    @property
    def mid_rate(self) -> float:
        """Sample rate of the intermediate stream (for tests)."""
        return self._extractor.mid_rate

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Convert one block of complex baseband; returns float32 audio at OUT_RATE."""
        audio = self._extractor.process(iq)
        if len(audio) == 0:
            return audio
        return self._apply_agc(audio)

    def _apply_agc(self, audio: np.ndarray) -> np.ndarray:
        power = float(np.mean(audio.astype(np.float64) ** 2))
        alpha = min(1.0, (len(audio) / OUT_RATE) / _AGC_TAU_S)
        self._agc_power = (
            power if self._agc_power is None else ((1.0 - alpha) * self._agc_power + alpha * power)
        )
        gain = min(_MAX_GAIN, _TARGET_RMS / (np.sqrt(self._agc_power) + 1e-12))
        result: np.ndarray = np.clip(audio * gain, -1.0, 1.0).astype(np.float32)
        return result


class SdrUsbAudioTap:
    """Feeds an SDR pipeline's I/Q through UsbAudio12k on a worker thread.

    Usage::

        tap = SdrUsbAudioTap(sample_rate, on_audio)   # on_audio(chunk: float32 @ 12 kHz)
        tap.start()
        pipeline.subscribe(tap.push_samples)
        ...
        pipeline.unsubscribe(tap.push_samples)
        tap.stop()

    The pipeline thread only enqueues a block; the DSP and *on_audio* run on
    the tap's own thread so a slow consumer can never hold up the SDR read
    loop (blocks are dropped instead, like the other SDR taps).
    """

    def __init__(self, sample_rate: float, on_audio: Callable[[np.ndarray], None]) -> None:
        self._converter = UsbAudio12k(sample_rate)
        self._on_audio = on_audio
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_blocks = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sdr-usb-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def push_samples(self, iq: np.ndarray) -> None:
        """SDRPipeline.subscribe() callback -- called on the pipeline thread."""
        try:
            self._q.put_nowait(np.array(iq, dtype=np.complex64))
        except queue.Full:
            self.dropped_blocks += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                audio = self._converter.process(iq)
                if len(audio):
                    self._on_audio(audio)
            except Exception:
                logger.exception("SdrUsbAudioTap: processing failed")
