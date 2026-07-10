"""Unit tests for core/doppler_worker.py — the Qt-independent, thread-based
precise interval trigger used for Doppler correction cycles.

No Qt event loop or real rig hardware is involved: DopplerWorker only
calls a plain callback at a precise interval, exercised directly against
a real background thread.
"""

from __future__ import annotations

import threading
import time

from core.doppler_worker import DopplerWorker


class TestDopplerWorker:
    def test_fires_repeatedly_at_interval(self) -> None:
        calls: list[float] = []
        done = threading.Event()

        def on_cycle() -> None:
            calls.append(time.monotonic())
            if len(calls) >= 5:
                done.set()

        worker = DopplerWorker(on_cycle, interval_s=0.15)
        worker.start()
        try:
            assert done.wait(timeout=5.0), "on_cycle did not fire 5 times in time"
        finally:
            worker.stop()

        deltas = [calls[i + 1] - calls[i] for i in range(len(calls) - 1)]
        for d in deltas:
            assert 0.1 < d < 0.25, f"interval drifted too far: {d:.3f}s"

    def test_stop_prevents_further_calls(self) -> None:
        calls: list[float] = []
        worker = DopplerWorker(lambda: calls.append(time.monotonic()), interval_s=0.1)
        worker.start()
        time.sleep(0.35)  # let a few cycles fire
        worker.stop()
        count_at_stop = len(calls)
        time.sleep(0.4)  # long enough for 3+ more cycles, if still running
        assert len(calls) == count_at_stop

    def test_start_is_idempotent(self) -> None:
        worker = DopplerWorker(lambda: None, interval_s=1.0)
        worker.start()
        first_thread = worker._thread
        worker.start()
        assert worker._thread is first_thread
        worker.stop()

    def test_set_interval_takes_effect(self) -> None:
        calls: list[float] = []
        done = threading.Event()

        def on_cycle() -> None:
            calls.append(time.monotonic())
            if len(calls) >= 6:
                done.set()

        worker = DopplerWorker(on_cycle, interval_s=0.3)
        worker.start()
        time.sleep(0.05)
        worker.set_interval(0.1)  # speed up shortly after starting
        try:
            assert done.wait(timeout=5.0)
        finally:
            worker.stop()
        # The last few deltas should reflect the faster interval, not the
        # original slower one.
        deltas = [calls[i + 1] - calls[i] for i in range(len(calls) - 1)]
        assert deltas[-1] < 0.25

    def test_exception_in_callback_does_not_kill_the_loop(self) -> None:
        calls: list[int] = []
        done = threading.Event()

        def on_cycle() -> None:
            calls.append(1)
            if len(calls) == 2:
                done.set()
            if len(calls) == 1:
                raise RuntimeError("boom")

        worker = DopplerWorker(on_cycle, interval_s=0.1)
        worker.start()
        try:
            assert done.wait(timeout=3.0), "loop stopped after the callback raised"
        finally:
            worker.stop()
        assert len(calls) >= 2

    def test_negative_interval_is_clamped(self) -> None:
        worker = DopplerWorker(lambda: None, interval_s=-5.0)
        assert worker._interval_s > 0
        worker.set_interval(-1.0)
        assert worker._interval_s > 0
