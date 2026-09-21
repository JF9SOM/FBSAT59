"""Tests for comms/signal_clock.py -- the time of the data being decoded."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from comms.signal_clock import replay_time, signal_time

START = datetime(2026, 9, 20, 6, 55, 51, tzinfo=UTC)


def _replay_pipeline(position: float, confirmed: bool = True) -> MagicMock:
    pipeline = MagicMock()
    pipeline._device.start_time_utc = START
    pipeline._device.position_s = position
    pipeline._device.start_time_confirmed = confirmed
    return pipeline


def test_a_recording_is_timed_by_its_start_plus_the_playback_position() -> None:
    got = replay_time(_replay_pipeline(357.5))
    assert got == (datetime(2026, 9, 20, 7, 1, 48, 500000, tzinfo=UTC), True)


def test_a_placeholder_start_time_is_reported_as_unconfirmed() -> None:
    got = replay_time(_replay_pipeline(10.0, confirmed=False))
    assert got is not None
    assert got[1] is False


def test_a_device_that_is_not_a_recording_is_not_a_replay() -> None:
    live = MagicMock()  # a live SDR: no start_time_utc / position_s (auto-mocks are not datetimes)
    assert replay_time(live) is None
    assert replay_time(None) is None


def test_live_input_uses_the_wall_clock_and_is_trusted() -> None:
    when, reliable = signal_time(None)
    assert reliable
    assert abs((when - datetime.now(UTC)).total_seconds()) < 5.0


def test_signal_time_of_a_recording() -> None:
    assert signal_time(_replay_pipeline(0.0)) == (START, True)
