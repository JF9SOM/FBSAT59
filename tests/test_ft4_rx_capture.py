"""Unit tests for comms/ft4/rx_capture.py — the Qt-independent, thread-based
FT4 RX audio capture/period-slicing worker.

No Qt event loop or real audio hardware is involved: push_audio() and the
on_period callback are exercised directly against a real background
thread, with _PERIOD_S patched down so the tests run in a couple of
seconds instead of needing real 6s periods.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

import comms.ft4.rx_capture as rx_capture
from comms.ft4.rx_capture import Ft4RxCaptureWorker


@pytest.fixture(autouse=True)
def _fast_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real FT4 periods are 6s — far too slow for a test loop. All tests in
    this file share a shortened period via this autouse fixture."""
    monkeypatch.setattr(rx_capture, "_PERIOD_S", 0.3)


def _push_zeros_until(worker: Ft4RxCaptureWorker, stop: threading.Event, chunk: int = 480) -> None:
    """Simulate an audio callback thread feeding small chunks continuously."""
    while not stop.is_set():
        worker.push_audio(np.zeros(chunk, dtype=np.float32))
        time.sleep(0.01)


class TestFt4RxCaptureWorker:
    def test_fires_on_period_repeatedly(self) -> None:
        received: list[int] = []
        done = threading.Event()

        def on_period(audio: np.ndarray) -> None:
            received.append(len(audio))
            if len(received) >= 3:
                done.set()

        worker = Ft4RxCaptureWorker(on_period)
        worker.start()
        stop_pushing = threading.Event()
        pusher = threading.Thread(
            target=_push_zeros_until, args=(worker, stop_pushing), daemon=True
        )
        pusher.start()
        try:
            assert done.wait(timeout=5.0), "on_period did not fire 3 times in time"
        finally:
            stop_pushing.set()
            worker.stop()

        assert len(received) >= 3
        # Each period's slice should contain a plausible amount of audio for
        # a ~0.3s period at the pusher's ~48000 samples/s push rate — not
        # empty, not wildly larger than one period's worth.
        for n in received[:3]:
            assert 5_000 < n < 60_000

    def test_no_callback_when_no_audio_pushed(self) -> None:
        """An empty period (no push_audio() calls) must not invoke on_period
        with an empty/garbage array — see the `if not chunks: continue`
        guard in _run()."""
        calls: list[int] = []
        worker = Ft4RxCaptureWorker(lambda audio: calls.append(len(audio)))
        worker.start()
        try:
            time.sleep(0.7)
        finally:
            worker.stop()
        assert calls == []

    def test_stop_prevents_further_callbacks(self) -> None:
        calls: list[int] = []
        worker = Ft4RxCaptureWorker(lambda audio: calls.append(len(audio)))
        worker.start()
        stop_pushing = threading.Event()
        pusher = threading.Thread(
            target=_push_zeros_until, args=(worker, stop_pushing), daemon=True
        )
        pusher.start()
        time.sleep(0.5)  # let at least one period fire
        worker.stop()
        stop_pushing.set()
        count_at_stop = len(calls)
        time.sleep(0.7)  # long enough for 2+ more periods, if still running
        assert len(calls) == count_at_stop

    def test_start_is_idempotent(self) -> None:
        worker = Ft4RxCaptureWorker(lambda audio: None)
        worker.start()
        first_thread = worker._thread
        worker.start()
        assert worker._thread is first_thread
        worker.stop()

    def test_push_audio_is_thread_safe_under_concurrent_access(self) -> None:
        """Multiple threads pushing concurrently must not crash or corrupt
        the internal chunk list (guarded by a lock)."""
        received: list[int] = []
        done = threading.Event()

        def on_period(audio: np.ndarray) -> None:
            received.append(len(audio))
            if len(received) >= 2:
                done.set()

        worker = Ft4RxCaptureWorker(on_period)
        worker.start()
        stop_pushing = threading.Event()
        pushers = [
            threading.Thread(target=_push_zeros_until, args=(worker, stop_pushing), daemon=True)
            for _ in range(4)
        ]
        for p in pushers:
            p.start()
        try:
            assert done.wait(timeout=5.0)
        finally:
            stop_pushing.set()
            worker.stop()
        assert all(n > 0 for n in received)
