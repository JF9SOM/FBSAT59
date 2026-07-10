"""Independent-thread trigger for Doppler correction cycles.

comms.ft4.rx_capture.Ft4RxCaptureWorker proved this pattern for RX audio
timing: a plain threading.Thread with its own time.sleep()-based wake
loop, completely decoupled from whatever the Qt main thread (world map
redraw, satellite list rendering, etc.) is doing. This applies the same
idea to Doppler correction.

The actual CAT communication was already backgrounded — MainWindow spawns
a short-lived thread per rig send, guarded by a busy-lock, so it never
blocked the old MainWindow._on_tick() itself. What was missing was the
TRIGGER: a new Doppler cycle previously only started once per _on_tick()
call, so a slow tick (e.g. the world map redraw fixed in the same
investigation) stretched out the gap between Doppler updates. This
worker fixes that by triggering new cycles on its own precise schedule
instead (2026-07-10).

Unlike Ft4RxCaptureWorker, this worker doesn't own any data of its own —
it just calls back into MainWindow._doppler_cycle() at a precise
interval. That callback only reads plain (non-Qt-widget) MainWindow
attributes and is safe to call from a background thread; it must not
touch Qt widgets directly (see main_window.py's _doppler_cycle
docstring).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class DopplerWorker:
    """Calls `on_cycle` at a precise interval on its own background thread."""

    def __init__(self, on_cycle: Callable[[], None], interval_s: float = 1.0) -> None:
        self._on_cycle = on_cycle
        self._interval_s = max(0.01, interval_s)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    def set_interval(self, interval_s: float) -> None:
        """Change the cycle interval. Takes effect from the next cycle."""
        with self._lock:
            self._interval_s = max(0.01, interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop. Does not block/join — the
        thread is a daemon and will exit on its own next wake-up, checking
        `_running` before calling back into anything else."""
        self._running = False
        self._thread = None

    def _run(self) -> None:
        next_time = time.monotonic()
        while self._running:
            with self._lock:
                interval = self._interval_s
            next_time += interval
            sleep_s = next_time - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                # Fell behind (a cycle took longer than the interval, or the
                # process was suspended) — resync to "now" instead of firing
                # a burst of catch-up cycles.
                next_time = time.monotonic()
            if not self._running:
                return
            try:
                self._on_cycle()
            except Exception:
                logger.exception("DopplerWorker on_cycle failed")
