"""
SDR I/Q pipeline — QThread pub/sub hub.

SDRPipeline runs in a dedicated QThread and continuously reads I/Q samples
from an SdrDevice.  It distributes the samples to:

  - FFT computation → spectrum_ready Signal  (≈10 fps)
  - Demodulator → audio_ready Signal         (each block)
  - IQRecorder                               (each block)
  - Future plugin hooks via subscribe()

The pipeline is designed so that plugin authors never need to touch this file.
New consumers simply call subscribe(callback) to receive each numpy block.

Signals emitted on the Qt main thread (via QMetaObject / queued connection):
  spectrum_ready(list)   — [(freq_hz, power_dbfs), …] for spectrum display
  audio_ready(ndarray)   — float32 PCM block at AUDIO_RATE
  status_changed(str)    — human-readable status message
  error_occurred(str)    — error message
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from sdr.demodulator import AUDIO_RATE, DemodMode, Demodulator
from sdr.device import SdrDevice
from sdr.diag_log import get_sdr_diag_logger
from sdr.recorder import IQRecorder

logger = logging.getLogger(__name__)

# Number of samples per pipeline block
_BLOCK_SIZE: int = 16_384

# FFT update interval (seconds)
_FFT_INTERVAL: float = 0.1  # 10 fps

# FFT resolution
_FFT_SIZE: int = 1024


class SDRPipeline(QThread):
    """
    I/Q acquisition and distribution thread.

    Instantiate with an open SdrDevice, then call start().
    Stop by calling stop() followed by wait().
    """

    spectrum_ready: Signal = Signal(list)  # [(freq_hz, power_dbfs), …]
    center_freq_changed: Signal = Signal(float)  # current centre frequency (Hz)
    audio_ready: Signal = Signal(object)  # np.ndarray float32 PCM
    status_changed: Signal = Signal(str)
    error_occurred: Signal = Signal(str)

    def __init__(self, device: SdrDevice, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._device = device
        self._demodulator = Demodulator(input_rate=device.sample_rate)
        self._recorder = IQRecorder()
        self._stop_flag = threading.Event()

        # Subscriber callbacks (called from pipeline thread — must be thread-safe)
        self._subscribers: list[Callable[[np.ndarray], None]] = []
        self._subscribers_lock = threading.Lock()

        # Audio output
        self._audio_enabled: bool = False
        self._sounddevice_stream: Any = None
        # Lock protecting _sounddevice_stream: both the pipeline thread (writes
        # PCM) and the main Qt thread (stop/disable) access the stream object.
        self._audio_lock = threading.Lock()

        # Consumers that need demodulated audio_ready data (CW/FT4/Q65/SSTV
        # decoders) but not necessarily speaker playback — see
        # request_audio()/release_audio(). A plain set is fine without its
        # own lock: it's only ever added-to/removed-from by name (atomic
        # under the GIL) and read as a single `bool(...)` check in run(),
        # the same threading assumption _audio_enabled itself already makes.
        self._demod_requesters: set[str] = set()

        # FFT timing
        self._last_fft_time: float = 0.0

        # Diagnostic-only (see sdr.diag_log): duration of the most recent
        # _play_audio() write() call, read by run()'s per-second summary.
        # Written and read from the pipeline thread only — no lock needed.
        self._diag_last_audio_write_dur: float = 0.0
        # Diagnostic-only: the OutputStream's blocksize is fixed to
        # whatever the first _play_audio() call's PCM length happened to
        # be (see _play_audio()) — tracked here to log if a later call
        # ever passes a differently-sized block, a plausible cause of
        # audible stutter if the PortAudio backend doesn't tolerate it.
        self._diag_audio_blocksize: int | None = None

    # ------------------------------------------------------------------
    # Public API (safe to call from any thread)
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[np.ndarray], None]) -> None:
        """Register a callback to receive each I/Q block (complex64 numpy array)."""
        with self._subscribers_lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[np.ndarray], None]) -> None:
        with self._subscribers_lock:
            self._subscribers = [c for c in self._subscribers if c is not callback]

    def stop(self) -> None:
        """Signal the thread to stop."""
        self._stop_flag.set()

    # -- Demodulator control --

    def set_demod_mode(self, mode: DemodMode) -> None:
        self._demodulator.set_mode(mode)

    def set_audio_gain(self, gain: float) -> None:
        self._demodulator.set_audio_gain(gain)

    def set_agc(self, enabled: bool) -> None:
        self._demodulator.set_agc(enabled)

    def set_audio_enabled(self, enabled: bool) -> None:
        self._audio_enabled = enabled
        if not enabled:
            # Close stream from whichever thread calls this; lock prevents
            # concurrent access with the pipeline thread's _play_audio().
            with self._audio_lock:
                self._close_audio_stream_locked()

    def request_audio(self, owner: str) -> None:
        """Register `owner`'s interest in demodulated audio (audio_ready).

        Decoder tabs (CW/FT4/Q65/SSTV) that subscribe to audio_ready need
        run() to actually call the demodulator and emit the signal — but
        that was previously gated entirely behind _audio_enabled, which
        only SdrControlWidget's own "Start Audio" button (speaker
        playback) ever set. Without pressing that *separate*, easy-to-miss
        button in a different tab first, a decoder's own "Start" did
        nothing at all: audio_ready simply never fired (GitHub Issue #12
        follow-up — CW Decoder's Level meter stuck at "-- dB" even with a
        strong signal visible on the spectrum). request_audio() lets a
        decoder ask for the data it needs independent of whether the user
        also wants to hear it out loud; reference-counted (by owner name,
        same pattern as AudioDeviceManager/AprsEngine) so multiple
        decoders — or a decoder plus SdrControlWidget's own toggle — never
        step on each other.
        """
        self._demod_requesters.add(owner)

    def release_audio(self, owner: str) -> None:
        """Release `owner`'s interest registered via request_audio()."""
        self._demod_requesters.discard(owner)

    # -- Recorder control --

    @property
    def recorder(self) -> IQRecorder:
        return self._recorder

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop: read samples, distribute to consumers."""
        logger.info("SDRPipeline started (rate=%.0f Hz)", self._device.sample_rate)
        self.status_changed.emit("SDR streaming")

        if not self._device.start_stream():
            self.error_occurred.emit("Failed to start SDR stream")
            return

        self._stop_flag.clear()
        self._demodulator.set_input_rate(self._device.sample_rate)

        # Diagnostic-only (see sdr.diag_log): one aggregated summary line
        # per wall-clock second, so a run of several minutes doesn't
        # produce tens of thousands of per-block lines. Added 2026-07-25
        # to check whether this loop keeps up with real time when a
        # second SDR consumer (e.g. Telemetry's AX.25 reception) is also
        # active — see sdr/diag_log.py's module docstring.
        diag_logger = get_sdr_diag_logger()
        diag_window_start = time.monotonic()
        diag_iters = 0
        diag_partial = 0
        diag_lag_sum = 0.0
        diag_lag_max = 0.0

        while not self._stop_flag.is_set():
            iter_start = time.monotonic()
            iq = self._device.read_samples(_BLOCK_SIZE)
            if iq is None or len(iq) == 0:
                # Timeout or error — brief sleep to avoid spin-loop
                time.sleep(0.005)
                continue

            # Distribute to plugin subscribers
            with self._subscribers_lock:
                subs = list(self._subscribers)
            for cb in subs:
                try:
                    cb(iq)
                except Exception:
                    logger.exception("SDR subscriber callback error")

            # IQ recorder
            self._recorder.put_samples(iq)

            # Demodulate → audio_ready (needed by any decoder tab that
            # requested it, independent of whether the user also wants
            # speaker playback) → speaker playback (only if the user
            # actually turned that on via SdrControlWidget's Start Audio).
            if self._audio_enabled or self._demod_requesters:
                try:
                    pcm = self._demodulator.process(iq)
                    if len(pcm) > 0:
                        self.audio_ready.emit(pcm)
                        if self._audio_enabled:
                            self._play_audio(pcm)
                except Exception:
                    logger.exception("Demodulator error")

            # FFT → spectrum + centre frequency overlay
            now = time.monotonic()
            if now - self._last_fft_time >= _FFT_INTERVAL:
                self._last_fft_time = now
                try:
                    spectrum = self._compute_fft(iq)
                    self.spectrum_ready.emit(spectrum)
                    self.center_freq_changed.emit(self._device.center_freq)
                except Exception:
                    logger.exception("FFT error")

            # Diagnostic aggregation (see comment above the loop). Positive
            # lag means this iteration took longer than the real-time
            # duration of the samples it processed — i.e. the loop is
            # falling behind the SDR hardware. A rising partial-read count
            # is the more direct symptom: read_samples() returning fewer
            # than _BLOCK_SIZE samples means its 50ms timeout was hit
            # because the driver's buffer hadn't filled, itself a sign
            # this loop isn't draining it fast enough between reads.
            sr = self._device.sample_rate
            expected_s = (len(iq) / sr) if sr else 0.0
            lag_s = (time.monotonic() - iter_start) - expected_s
            diag_iters += 1
            if len(iq) < _BLOCK_SIZE:
                diag_partial += 1
            diag_lag_sum += lag_s
            diag_lag_max = max(diag_lag_max, lag_s)
            diag_now = time.monotonic()
            if diag_now - diag_window_start >= 1.0:
                diag_logger.info(
                    "pipeline iters=%d partial=%d avg_lag=%.4fs max_lag=%.4fs "
                    "max_audio_write=%.4fs audio_enabled=%s demod_requesters=%d",
                    diag_iters,
                    diag_partial,
                    diag_lag_sum / diag_iters if diag_iters else 0.0,
                    diag_lag_max,
                    self._diag_last_audio_write_dur,
                    self._audio_enabled,
                    len(self._demod_requesters),
                )
                diag_window_start = diag_now
                diag_iters = 0
                diag_partial = 0
                diag_lag_sum = 0.0
                diag_lag_max = 0.0
                self._diag_last_audio_write_dur = 0.0

        self._device.stop_stream()
        with self._audio_lock:
            self._close_audio_stream_locked()
        logger.info("SDRPipeline stopped")
        self.status_changed.emit("SDR stopped")

    # ------------------------------------------------------------------
    # FFT
    # ------------------------------------------------------------------

    def _compute_fft(self, iq: np.ndarray) -> list[tuple[float, float]]:
        """Compute power spectrum.  Returns [(freq_hz, power_dbfs), …]."""
        n = min(_FFT_SIZE, len(iq))
        window = np.blackman(n).astype(np.float32)
        block = iq[:n] * window
        fft = np.fft.fftshift(np.fft.fft(block, n=_FFT_SIZE))
        power_db = 20.0 * np.log10(np.abs(fft) / n + 1e-12)
        cf = self._device.center_freq
        sr = self._device.sample_rate
        freqs = cf + np.fft.fftshift(np.fft.fftfreq(_FFT_SIZE, d=1.0 / sr))
        return list(zip(freqs.tolist(), power_db.tolist(), strict=False))

    # ------------------------------------------------------------------
    # Audio output (sounddevice)
    # ------------------------------------------------------------------

    def _play_audio(self, pcm: np.ndarray) -> None:
        """Write PCM to sounddevice output stream, opening it on first call.

        Must only be called from the pipeline thread.
        """
        with self._audio_lock:
            try:
                import sounddevice as sd

                if self._sounddevice_stream is None:
                    self._sounddevice_stream = sd.OutputStream(
                        samplerate=AUDIO_RATE,
                        channels=1,
                        dtype="float32",
                        blocksize=len(pcm),
                    )
                    self._sounddevice_stream.start()
                    self._diag_audio_blocksize = len(pcm)
                elif len(pcm) != self._diag_audio_blocksize:
                    get_sdr_diag_logger().info(
                        "pipeline audio_write blocksize_mismatch stream_blocksize=%d pcm_len=%d",
                        self._diag_audio_blocksize,
                        len(pcm),
                    )
                write_start = time.monotonic()
                self._sounddevice_stream.write(pcm)
                self._diag_last_audio_write_dur = time.monotonic() - write_start
            except Exception:
                logger.exception("Audio output error")
                self._sounddevice_stream = None

    def _close_audio_stream_locked(self) -> None:
        """Close sounddevice stream. Caller must hold _audio_lock."""
        if self._sounddevice_stream is not None:
            try:
                self._sounddevice_stream.stop()
                self._sounddevice_stream.close()
            except Exception:
                pass
            self._sounddevice_stream = None
