"""G3RUH 9600bps raw FM discriminator for SDR-fed Direwolf reception.

Produces the *raw* (no de-emphasis, wideband) FM-discriminator audio a
9600bps G3RUH AX.25 signal needs — the software equivalent of tapping a
radio's "DATA" port (pre-de-emphasis discriminator output) rather than its
normal speaker/mic audio path. The resulting 48kHz float32 PCM is fed to
Direwolf's stdin exactly like real soundcard audio, so Direwolf's own
built-in G3RUH decoder (MODEM 9600) does the actual demod / descramble /
clock recovery — this module only produces audio at the right bandwidth
and level for it.

Filter tuning (IF bandwidth, deviation constant) is a first pass based on
the same phase-difference discriminator technique as sdr/demodulator.py's
NFM path, not yet field-verified against a real 9600bps G3RUH satellite
signal.

G3ruhSdrDemod subscribes to raw I/Q directly (SDRPipeline.subscribe()),
independent of the SDR Control tab's Mode combo / shared Demodulator —
the same approach afsk_demod.py's AfskDemodulator uses — so it can run
alongside another demod mode (e.g. CW Decoder) on the same SDR pipeline.
"""

from __future__ import annotations

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
# Assumed peak FM deviation — same order as typical NFM voice satellite
# links. Kept separate from sdr/demodulator.py's NFM_DEVIATION so it can be
# tuned independently once verified against a real signal.
_DEVIATION_HZ = 5_000.0
# IF half-bandwidth: wider padding than NFM's voice-oriented ~4kHz, since a
# 9600 baud G3RUH signal needs more baseband bandwidth than 300-3000Hz voice.
_IF_HALF_BW_HZ = _DEVIATION_HZ + 8_000.0


class G3ruhDiscriminator:
    """Stateful raw-discriminator DSP.

    Mirrors sdr/demodulator.py's Demodulator._demod_nfm() (DC removal → IF
    bandpass → decimate → phase-difference FM discriminator → decimate to
    48kHz), but skips the de-emphasis stage NFM applies for voice — 9600bps
    G3RUH needs the flat, wideband discriminator output instead.
    """

    def __init__(self, input_rate: float) -> None:
        self._input_rate = input_rate
        self._dc_zi_i = np.zeros(1, dtype=np.float32)
        self._dc_zi_q = np.zeros(1, dtype=np.float32)
        self._build_filters()

    def _build_filters(self) -> None:
        rate = self._input_rate

        alpha_dc = float(np.clip(1.0 - (2.0 * np.pi * 30.0 / rate), 0.0, 0.9999))
        self._dc_b = np.array([1.0, -1.0], dtype=np.float64)
        self._dc_a = np.array([1.0, -alpha_dc], dtype=np.float64)

        self._decim1 = max(1, int(rate / _INTERMEDIATE_RATE_TARGET))
        self._mid_rate = rate / self._decim1
        self._decim2 = max(1, int(self._mid_rate / _AUDIO_RATE))

        if_bw = float(np.clip(_IF_HALF_BW_HZ / (rate / 2.0), 0.001, 0.499))
        self._if_b = sp_signal.firwin(63, if_bw).astype(np.float32) if _SCIPY_AVAILABLE else None

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

        # No de-emphasis here (unlike NFM voice) — 9600bps G3RUH needs the
        # raw, flat discriminator output, same as a radio's DATA port.
        audio_raw = discrim * (self._mid_rate / (2 * np.pi * _DEVIATION_HZ))
        audio = self._decimate(audio_raw, self._decim2)
        result: np.ndarray = np.clip(audio, -1.0, 1.0).astype(np.float32)
        return result

    @staticmethod
    def _decimate(x: np.ndarray, factor: int) -> np.ndarray:
        """Simple decimation by integer factor — anti-aliasing is handled
        by the preceding IF bandpass filter, same rationale as
        sdr/demodulator.py's Demodulator._decimate()."""
        if factor <= 1:
            return x
        return x[::factor]


class G3ruhSdrDemod(QThread):
    """Runs G3ruhDiscriminator on an SDR pipeline's raw I/Q in a background
    thread, emitting ready-to-play 48kHz float32 PCM for Direwolf's stdin.

    Usage
    -----
    demod = G3ruhSdrDemod(sample_rate=int(pipeline._device.sample_rate))
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
        self._discriminator = G3ruhDiscriminator(input_rate=sample_rate)
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
            audio = self._discriminator.process(iq)
            if len(audio):
                self.audio_ready.emit(audio)
