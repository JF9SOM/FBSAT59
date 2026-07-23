"""Precise, Qt-independent FT4 RX audio capture and period slicing.

Runs its own background thread with a time.sleep()-based wake loop (not a
QTimer), and owns the RX audio accumulation buffer directly — completely
decoupled from whatever the Qt main thread (satellite tracking, map
rendering, MainWindow._on_tick(), etc.) happens to be doing. This decides
"the last 6s of audio is ready", not Ft4Scheduler. Ft4Scheduler remains
QTimer-based and keeps driving the TX-turn decision and the countdown/
TX-RX indicator display — neither of those is timing-critical the way RX
audio capture is (PTT already has slack built in via lead/tail delays),
so there was no need to move them too.

Added 2026-07-10 after ft4_decode.log showed audio_len oscillating
5.1s-7.0s (instead of a steady ~6.05s) on a ~60s beat, traced to
MainWindow._on_tick()'s 1Hz heartbeat (Doppler calc, world map redraw,
etc.) sharing the same Qt event loop as Ft4Scheduler's QTimer — a slow
_on_tick() could delay Ft4Scheduler's own boundary detection by however
long it ran. Moving RX capture off the Qt event loop entirely removes
that dependency; only the final UI update (decoded table, waterfall
image) still needs to cross back onto the Qt main thread, which doesn't
affect audio buffer correctness — see Ft4Tab.period_skipped / the
_RxDecodeWorker.done signal, both ordinary cross-thread Qt signals.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

from comms.ft4.decode_log import get_ft4_decode_logger
from core.clock_offset import corrected_time

_PERIOD_S = 6.0

AudioPeriodCallback = Callable[[NDArray[np.float32]], None]


class Ft4RxCaptureWorker:
    """Owns the RX audio buffer and hands off one period's audio at a time.

    push_audio() is called from the audio callback thread (soundcard or
    SDR) — safe to call from any thread, protected by an internal lock.

    `on_period` is called from this worker's own background thread once
    per completed 6s UTC-aligned period. The callback must not touch Qt
    widgets directly (it isn't running on the Qt main thread) — Ft4Tab's
    callback only does thread-safe bookkeeping and hands off to Qt signals
    for anything that needs the main thread.
    """

    def __init__(self, on_period: AudioPeriodCallback) -> None:
        self._on_period = on_period
        self._lock = threading.Lock()
        self._chunks: list[NDArray[np.float32]] = []
        self._thread: threading.Thread | None = None
        self._running = False

    def push_audio(self, chunk: NDArray[np.float32]) -> None:
        """Call from the audio callback thread (soundcard or SDR)."""
        with self._lock:
            self._chunks.append(chunk)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        with self._lock:
            self._chunks = []
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop. Does not block/join — the
        thread is a daemon and will exit on its own next wake-up, checking
        `_running` before touching anything else (see _run())."""
        self._running = False
        self._thread = None

    def _run(self) -> None:
        logger = get_ft4_decode_logger()
        # Align to the next UTC 6s boundary before accumulating anything, so
        # the first period isn't a partial slice starting at a random offset.
        now = corrected_time()
        next_boundary = (int(now / _PERIOD_S) + 1) * _PERIOD_S
        time.sleep(max(0.0, next_boundary - corrected_time()))
        with self._lock:
            self._chunks = []

        while self._running:
            next_boundary += _PERIOD_S
            sleep_s = next_boundary - corrected_time()
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                # This thread itself fell behind (e.g. the process was
                # suspended, or the OS starved it) — resync to "now" instead
                # of firing a burst of catch-up callbacks for missed slots.
                next_boundary = (int(corrected_time() / _PERIOD_S) + 1) * _PERIOD_S
            if not self._running:
                return

            lag = corrected_time() % _PERIOD_S
            logger.info("capture boundary_lag=%.3fs", lag)

            with self._lock:
                chunks, self._chunks = self._chunks, []
            if not chunks:
                continue
            audio = np.concatenate(chunks)
            self._on_period(audio)
