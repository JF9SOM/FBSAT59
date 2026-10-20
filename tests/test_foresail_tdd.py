"""The satellite's transmit windows (comms/foresail/tdd.py)."""

from __future__ import annotations

import pytest

from comms.foresail.tdd import TddTracker


def _beacon(t: TddTracker, base: float) -> None:
    # Frames heard in a real recording (2026-10-18): one window, then the next 1.02 s later.
    t.observe(base + 0.000, 250, 248)
    t.observe(base + 0.104, 250, 140)
    t.observe(base + 0.242, 250, 2)
    t.observe(base + 1.026, 250, 244)


def test_window_end_is_frame_start_plus_remaining() -> None:
    t = TddTracker()
    t.observe(100.000, 250, 248)
    t.observe(100.104, 250, 140)
    t.observe(100.242, 250, 2)
    assert t.window_end == pytest.approx(100.246, abs=0.005)
    assert t.in_satellite_window(100.1)
    assert not t.in_satellite_window(100.5)


def test_cycle_between_consecutive_windows() -> None:
    t = TddTracker()
    _beacon(t, 50.0)
    assert t.cycle_s == pytest.approx(1.02, abs=0.02)


def test_no_frames_heard_means_go() -> None:
    advice = TddTracker().advice(10.0, 0.15)
    assert advice.go and advice.reason == "no satellite frames heard"


def test_wait_for_the_window_and_the_guard() -> None:
    t = TddTracker()
    t.observe(100.0, 250, 100)  # window ends at 100.1
    advice = t.advice(100.05, 0.15, guard_s=0.3)
    assert not advice.go and advice.wait_s == pytest.approx(0.35, abs=0.001)
    assert advice.reason == "satellite window"


def test_between_windows_of_a_beacon_goes_when_it_fits() -> None:
    t = TddTracker()
    t.observe(50.0, 250, 248)
    t.observe(50.242, 250, 2)  # only the first window has been heard so far (ends ~50.244)
    # 0.35 s after it ended, past the 0.3 s guard: 0.15 s of audio fits before the next one.
    assert t.advice(50.244 + 0.35, 0.15, guard_s=0.3).go


def test_waits_out_the_next_window_when_the_frame_would_run_into_it() -> None:
    t = TddTracker()
    t.observe(50.0, 250, 248)
    t.observe(50.242, 250, 2)  # first window ends ~50.244
    t.observe(51.026, 250, 244)  # next window ends ~51.27: cycle 1.02 s
    # At 52.0 the third window (starts ~52.04) is about to begin.
    advice = t.advice(52.0, 0.15, guard_s=0.3)
    assert not advice.go and advice.reason == "next satellite window"
    assert advice.wait_s > 0.3


def test_quiet_long_after_the_beacon() -> None:
    t = TddTracker()
    _beacon(t, 50.0)
    advice = t.advice(80.0, 0.15)
    assert advice.go and advice.reason == "satellite quiet"


def test_reset_forgets_the_windows() -> None:
    t = TddTracker()
    _beacon(t, 50.0)
    t.reset()
    assert t.window_end is None and t.cycle_s is None
