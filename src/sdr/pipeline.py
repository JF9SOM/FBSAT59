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
  burst_row_ready(BurstRow) — averaged spectrum row + burst verdict; only
                              emitted while set_burst_detection(True) is active
  audio_ready(ndarray)   — float32 PCM block at AUDIO_RATE
  status_changed(str)    — human-readable status message
  error_occurred(str)    — error message
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from sdr.burst_detector import BurstDetector
from sdr.demodulator import AUDIO_RATE, DemodMode, Demodulator
from sdr.diag_log import get_sdr_diag_logger
from sdr.recorder import IQRecorder

logger = logging.getLogger(__name__)

# Number of samples per pipeline block
_BLOCK_SIZE: int = 16_384

# FFT update interval (seconds)
_FFT_INTERVAL: float = 0.1  # 10 fps

# Speaker playback is queued to its own thread (see _play_audio()); this is
# how many demodulated blocks (~65 ms each) may wait there before the oldest
# is dropped, i.e. about half a second of audio.
_AUDIO_QUEUE_BLOCKS: int = 8

# SDR stall watchdog (see SDRPipeline._recover_stalled_device()): how long the
# device may deliver no samples at all before the pipeline tries to restart
# its stream, and the minimum spacing between successive recovery attempts.
_STALL_TIMEOUT_S: float = 3.0

# Per-sample exponential-smoothing coefficient for the Doppler NCO's
# frequency itself (not just its phase) — see _apply_doppler_correction().
# Matches SatDump's own DopplerCorrectBlock default (doppler_alpha=0.01 in
# src-core/pipeline/modules/demod/module_demod_base.h), confirmed by reading
# its source directly rather than assuming: SatDump blends
# curr_freq = curr_freq*(1-alpha) + targ_freq*alpha once per sample.
_DOPPLER_SMOOTH_ALPHA: float = 0.01

# FFT resolution
_FFT_SIZE: int = 1024


class SdrDeviceLike(Protocol):
    """Structural contract SDRPipeline actually needs from its device.

    SdrDevice (real SoapySDR hardware) and SdrFileDevice (recorded .iq.wav
    playback, see sdr/file_device.py) both satisfy this without either
    inheriting from the other -- SDRPipeline only ever touches these five
    members (grep self._device. in this file to confirm).
    """

    @property
    def sample_rate(self) -> float: ...

    @property
    def center_freq(self) -> float: ...

    def start_stream(self) -> bool: ...

    def read_samples(self, num_samples: int = ...) -> np.ndarray | None: ...

    def stop_stream(self) -> None: ...


class SDRPipeline(QThread):
    """
    I/Q acquisition and distribution thread.

    Instantiate with an open SdrDevice (or an SdrFileDevice for recorded
    IQ playback -- see SdrDeviceLike), then call start().
    Stop by calling stop() followed by wait().
    """

    spectrum_ready: Signal = Signal(list)  # [(freq_hz, power_dbfs), …]
    burst_row_ready: Signal = Signal(object)  # sdr.burst_detector.BurstRow
    center_freq_changed: Signal = Signal(float)  # current centre frequency (Hz)
    audio_ready: Signal = Signal(object)  # np.ndarray float32 PCM
    status_changed: Signal = Signal(str)
    error_occurred: Signal = Signal(str)

    def __init__(self, device: SdrDeviceLike, parent: QObject | None = None) -> None:
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
        # Playback thread: the OutputStream write blocks at audio real-time
        # speed, so it must not run on the pipeline thread -- see _play_audio().
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=_AUDIO_QUEUE_BLOCKS)
        self._audio_thread: threading.Thread | None = None
        self._audio_writer_stop = threading.Event()
        # Diagnostic-only: blocks dropped because playback couldn't keep up.
        self._diag_audio_dropped: int = 0

        # Consumers that need demodulated audio_ready data (CW/FT4/Q65/SSTV
        # decoders) but not necessarily speaker playback — see
        # request_audio()/release_audio(). A plain set is fine without its
        # own lock: it's only ever added-to/removed-from by name (atomic
        # under the GIL) and read as a single `bool(...)` check in run(),
        # the same threading assumption _audio_enabled itself already makes.
        self._demod_requesters: set[str] = set()

        # FFT timing
        self._last_fft_time: float = 0.0

        # Burst detection for the waterfall (see sdr/burst_detector.py). None
        # while switched off, which costs nothing: run() only touches the
        # detector when it is set. Assigned from the UI thread and read once
        # per loop iteration on the pipeline thread (an atomic reference
        # swap, so no lock is needed).
        self._burst_detector: BurstDetector | None = None

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

        # Digital Doppler correction (NCO) — see _apply_doppler_correction().
        # When set (via set_doppler_target()), every I/Q block is frequency-
        # shifted in software so *target* lands at baseband 0 Hz, without
        # ever retuning the SDR's actual hardware center frequency. This is
        # the same architecture real SatNOGS ground stations use (gr-satnogs'
        # doppler_correction_cc block): the hardware stays parked on one
        # frequency for the whole pass, and all Doppler tracking happens as
        # a continuous per-sample phase rotation, which has no PLL-relock
        # latency and no glitch even when updated tens of times a second —
        # unlike physically retuning the device, which is what this
        # replaces (see SdrRigAdapter.set_frequency() in rig/controller.py).
        # _nco_freq_hz additionally tracks the *actual* (smoothed) shift
        # currently being applied, separately from _doppler_target_hz (what
        # _sdr_doppler_cycle() most recently asked for) — the two only
        # coincide once the per-sample ramp in _apply_doppler_correction()
        # has converged.
        self._doppler_target_hz: float | None = None
        self._nco_phase: float = 0.0
        self._nco_freq_hz: float = 0.0
        self._last_hw_cf: float | None = None

    # ------------------------------------------------------------------
    # Public API (safe to call from any thread)
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[np.ndarray], None]) -> None:
        """Register a callback to receive each I/Q block (complex64 numpy array)."""
        with self._subscribers_lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[np.ndarray], None]) -> None:
        """Remove a callback registered via subscribe().

        Compares by equality (==), not identity (is): subscribe()'s own
        duplicate check uses `callback not in self._subscribers`, i.e.
        equality, and a bound method (e.g. `demod.push_samples`) is a new
        wrapper object on every attribute access -- equal to another access
        of the same method on the same instance, but never identical to it.
        Filtering by identity here meant unsubscribe() could never actually
        remove a bound-method callback: confirmed live, a demodulator kept
        receiving I/Q blocks for minutes after its owning session was torn
        down and a different mechanism started, because every unsubscribe()
        call across the whole SDR consumer set (AfskAudioSdrDemod,
        G3ruhSdrDemod, and any other pipeline.subscribe() user) was
        silently a no-op.
        """
        with self._subscribers_lock:
            self._subscribers = [c for c in self._subscribers if c != callback]

    def set_doppler_target(self, freq_hz: float | None) -> None:
        """Set (or clear, with None) the RF frequency to digitally track.

        Safe to call frequently from any thread (a single float attribute
        write/read, no lock needed — same assumption run() already makes
        for _audio_enabled). run() applies the correction on its own
        thread using whatever value is current at the start of each block.
        """
        self._doppler_target_hz = freq_hz

    @property
    def effective_center_freq(self) -> float:
        """The RF frequency this pipeline currently reports as "centered".

        This is the Doppler target while digital correction is active
        (the whole point: after correction, the tracked signal really is
        centered there, unlike the SDR's own rarely-retuned hardware
        frequency) — otherwise the SDR's actual hardware-tuned frequency.
        Used for the waterfall/spectrum frequency axis and by
        SdrRigAdapter.get_frequency(), so both reflect what's actually
        being tracked rather than a fixed hardware register value that
        may not move for an entire pass.
        """
        target = self._doppler_target_hz
        return target if target is not None else self._device.center_freq

    def _apply_doppler_correction(self, iq: np.ndarray) -> np.ndarray:
        """Shift *iq* so effective_center_freq lands at baseband 0 Hz.

        Phase-continuous across blocks (the running phase is carried over,
        not reset each call). On top of that, the shift *frequency* itself
        is per-sample exponentially smoothed toward _doppler_target_hz
        (see _DOPPLER_SMOOTH_ALPHA) rather than snapping to it the instant
        _sdr_doppler_cycle() writes a new target — matching SatDump's own
        DopplerCorrectBlock::work(), which blends curr_freq toward
        targ_freq every sample instead of stepping. Computed in closed
        form (geometric decay of the frequency, integrated via cumsum for
        phase) rather than a literal per-sample Python loop, since
        _doppler_target_hz is constant for the whole block: the recursion
        curr_freq[k] = targ + (curr_freq[k-1] - targ)*(1-alpha) has the
        closed form curr_freq[k] = targ + (curr_freq[0] - targ)*(1-alpha)**k,
        which numpy evaluates as vectorized array ops — no more expensive
        per block than the plain-shift version this replaced.

        Resets both the running phase and the smoothed frequency whenever
        the device's actual hardware center frequency changes underneath
        us (a real retune breaks both references, so continuing from the
        old values would be meaningless) — this self-heals without any
        caller needing to coordinate with us.
        """
        hw_cf = self._device.center_freq
        if hw_cf != self._last_hw_cf:
            self._nco_phase = 0.0
            self._nco_freq_hz = 0.0
            self._last_hw_cf = hw_cf

        if len(iq) == 0:
            return iq

        target = self._doppler_target_hz
        if target is None:
            self._nco_freq_hz = 0.0
            return iq

        sr = self._device.sample_rate
        if not sr:
            return iq

        targ_shift = target - hw_cf
        prev_freq = self._nco_freq_hz
        if targ_shift == 0.0 and prev_freq == 0.0:
            return iq

        n = len(iq)
        idx = np.arange(n, dtype=np.float64)
        decay = (1.0 - _DOPPLER_SMOOTH_ALPHA) ** idx
        freq_hz = targ_shift + (prev_freq - targ_shift) * decay
        cum_incl = np.cumsum(freq_hz)
        cum_excl = cum_incl - freq_hz
        phase = self._nco_phase + (2.0 * np.pi / sr) * cum_excl
        corrected: np.ndarray = iq * np.exp(-1j * phase).astype(np.complex64)
        self._nco_phase = float(
            (self._nco_phase + (2.0 * np.pi / sr) * cum_incl[-1]) % (2.0 * np.pi)
        )
        self._nco_freq_hz = float(
            targ_shift + (prev_freq - targ_shift) * (1.0 - _DOPPLER_SMOOTH_ALPHA) ** n
        )
        return corrected

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
            self._stop_audio_writer()

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

    # -- Burst detection --

    def set_burst_detection(self, enabled: bool) -> None:
        """Switch burst detection for the waterfall on or off.

        While on, every I/Q block also feeds a BurstDetector and each FFT
        tick emits burst_row_ready with the averaged spectrum row and the
        detector's verdict. Switching on when already on keeps the detector's
        state (baseline and counter); switching off discards it.
        """
        if enabled:
            if self._burst_detector is None:
                self._burst_detector = BurstDetector(self._device.sample_rate)
        else:
            self._burst_detector = None

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

        # Stall watchdog state -- see _recover_stalled_device().
        last_data_time = time.monotonic()
        last_recovery_time = 0.0
        recovery_attempts = 0

        while not self._stop_flag.is_set():
            iter_start = time.monotonic()
            iq = self._device.read_samples(_BLOCK_SIZE)
            if iq is None or len(iq) == 0:
                # Timeout or error — brief sleep to avoid spin-loop
                time.sleep(0.005)
                now = time.monotonic()
                if (
                    now - last_data_time >= _STALL_TIMEOUT_S
                    and now - last_recovery_time >= _STALL_TIMEOUT_S
                ):
                    last_recovery_time = now
                    recovery_attempts += 1
                    self._recover_stalled_device(recovery_attempts, now - last_data_time)
                continue
            if recovery_attempts:
                logger.warning(
                    "SDR stream recovered after %d attempt(s), %.1fs without samples",
                    recovery_attempts,
                    time.monotonic() - last_data_time,
                )
                self.status_changed.emit("SDR streaming")
                recovery_attempts = 0
            last_data_time = time.monotonic()

            # Digital Doppler correction, applied before anything else
            # touches the samples — every consumer below (subscribers,
            # recorder, demodulator, FFT) sees the already-corrected
            # stream, with no per-consumer wiring needed.
            iq = self._apply_doppler_correction(iq)

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

            # Burst detector: every block is averaged into the current row
            # (the single-FFT spectrum below only ever looks at 1024 samples
            # per tick, far too little to see a 0.2 s burst).
            burst_detector = self._burst_detector
            if burst_detector is not None:
                try:
                    burst_detector.feed(iq)
                except Exception:
                    logger.exception("Burst detector feed error")

            # FFT → spectrum + centre frequency overlay
            now = time.monotonic()
            if now - self._last_fft_time >= _FFT_INTERVAL:
                self._last_fft_time = now
                try:
                    spectrum = self._compute_fft(iq)
                    self.spectrum_ready.emit(spectrum)
                    self.center_freq_changed.emit(self.effective_center_freq)
                except Exception:
                    logger.exception("FFT error")
                if burst_detector is not None:
                    try:
                        burst_row = burst_detector.finish_row(self.effective_center_freq)
                        if burst_row is not None:
                            self.burst_row_ready.emit(burst_row)
                    except Exception:
                        logger.exception("Burst detector row error")

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
                    "max_audio_write=%.4fs audio_enabled=%s demod_requesters=%d "
                    "audio_dropped=%d",
                    diag_iters,
                    diag_partial,
                    diag_lag_sum / diag_iters if diag_iters else 0.0,
                    diag_lag_max,
                    self._diag_last_audio_write_dur,
                    self._audio_enabled,
                    len(self._demod_requesters),
                    self._diag_audio_dropped,
                )
                diag_window_start = diag_now
                diag_iters = 0
                diag_partial = 0
                diag_lag_sum = 0.0
                diag_lag_max = 0.0
                self._diag_last_audio_write_dur = 0.0

        self._device.stop_stream()
        self._stop_audio_writer()
        logger.info("SDRPipeline stopped")
        self.status_changed.emit("SDR stopped")

    # ------------------------------------------------------------------
    # Stall recovery
    # ------------------------------------------------------------------

    def _recover_stalled_device(self, attempt: int, stalled_s: float) -> None:
        """Try to revive a device that has stopped delivering samples.

        Called from run() once read_samples() has produced nothing for
        _STALL_TIMEOUT_S, and again every _STALL_TIMEOUT_S while it
        still doesn't. The first attempt only restarts the stream; later
        ones close and reopen the whole device (SdrDevice.reopen()).

        Observed 2026-09-19: an RTL-SDR stopped delivering samples for
        ~53 s in the middle of a pass and the pipeline thread just kept
        polling (read_samples() timing out) until the operator reconnected
        by hand -- losing the rest of the pass and, on that reconnect, the
        device (see SdrDevice.open()'s not-ready check).

        Devices that cannot be restarted (SdrFileDevice: a paused or
        finished recording legitimately returns nothing) simply don't
        define restart_stream(), which switches this off for them.
        """
        restart = getattr(self._device, "restart_stream", None)
        if restart is None:
            return
        reopen = getattr(self._device, "reopen", None)
        if attempt == 1 or reopen is None:
            logger.warning("SDR delivered no samples for %.1fs -- restarting stream", stalled_s)
            self.status_changed.emit("SDR stalled - restarting stream")
            action = restart
        else:
            logger.warning(
                "SDR still silent after %.1fs -- reopening device (attempt %d)", stalled_s, attempt
            )
            self.status_changed.emit("SDR stalled - reopening device")
            action = reopen
        try:
            ok = bool(action())
        except Exception:
            logger.exception("SDR stall recovery raised")
            return
        if not ok:
            logger.warning("SDR stall recovery attempt %d did not succeed", attempt)

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
        cf = self.effective_center_freq
        sr = self._device.sample_rate
        freqs = cf + np.fft.fftshift(np.fft.fftfreq(_FFT_SIZE, d=1.0 / sr))
        return list(zip(freqs.tolist(), power_db.tolist(), strict=False))

    # ------------------------------------------------------------------
    # Audio output (sounddevice)
    # ------------------------------------------------------------------

    def _play_audio(self, pcm: np.ndarray) -> None:
        """Queue *pcm* for speaker playback. Never blocks.

        Must only be called from the pipeline thread.

        sounddevice's OutputStream.write() returns only once the audio
        device has room for the data, i.e. it is paced at audio real-time
        speed. Called straight from run() it made every loop iteration take
        the demodulator + FFT time *on top of* a full block of playback
        (~82 ms for a 65.5 ms block at 250 kS/s), so the loop fell behind
        the SDR: the driver's buffer overflowed and ~20% of all samples
        were silently dropped -- from live decoding and IQ recordings alike
        (observed 2026-09-19 on three ARICA-2 / OrigamiSat-2 pass
        recordings, and a stream that stalled outright afterwards). A
        separate writer thread absorbs that pacing; if playback still falls
        behind, the oldest queued block is dropped -- a brief audio glitch
        instead of lost I/Q.
        """
        if not self._audio_enabled:
            return  # switched off since the caller checked -- don't start a writer
        thread = self._audio_thread
        if thread is None or not thread.is_alive():
            stop = threading.Event()
            self._audio_writer_stop = stop
            thread = threading.Thread(
                target=self._audio_writer_loop, args=(stop,), name="sdr-audio-out", daemon=True
            )
            self._audio_thread = thread
            thread.start()
        try:
            self._audio_queue.put_nowait(pcm)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._audio_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._audio_queue.put_nowait(pcm)
            self._diag_audio_dropped += 1

    def _audio_writer_loop(self, stop: threading.Event) -> None:
        """Playback thread body: drain the queue into the OutputStream."""
        while not stop.is_set():
            try:
                pcm = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._write_audio_block(pcm)
        with self._audio_lock:
            self._close_audio_stream_locked()

    def _write_audio_block(self, pcm: np.ndarray) -> None:
        """Write one block to the sounddevice output stream, opening it on first use."""
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

    def _stop_audio_writer(self) -> None:
        """Stop the playback thread (if any), discard queued audio, close the stream.

        Safe from any thread and idempotent: called when speaker playback is
        switched off and when the pipeline stops.
        """
        self._audio_writer_stop.set()
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        thread = self._audio_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._audio_thread = None
        with self._audio_lock:
            self._close_audio_stream_locked()

    def _close_audio_stream_locked(self) -> None:
        """Close sounddevice stream. Caller must hold _audio_lock."""
        if self._sounddevice_stream is not None:
            try:
                self._sounddevice_stream.stop()
                self._sounddevice_stream.close()
            except Exception:
                pass
            self._sounddevice_stream = None
