"""Tests for feeding played-back recordings to the decoder tabs' audio subscribers."""

from __future__ import annotations

import numpy as np
import pytest

from comms.audio_device_manager import (
    _HW_SAMPLE_RATE,
    AudioDeviceManager,
    DeviceFeedSink,
    _SharedInputStream,
)


def _stream_with_subscribers(
    manager: AudioDeviceManager, received: dict[str, list[int]], owners: list[str]
) -> _SharedInputStream:
    """A shared stream with fake subscribers (no hardware opened) registered on device 3."""
    stream = _SharedInputStream(3, lambda: manager._feed_state(3))
    for owner in owners:
        stream._subscribers[owner] = (
            _HW_SAMPLE_RATE,
            lambda chunk, o=owner: received.setdefault(o, []).append(len(chunk)),
        )
    manager._inputs[3] = stream
    return stream


class TestManagerFeed:
    def test_live_audio_reaches_everyone_when_not_feeding(self) -> None:
        mgr = AudioDeviceManager()
        got: dict[str, list[int]] = {}
        stream = _stream_with_subscribers(mgr, got, ["aprs", "rec"])
        stream._on_audio(np.zeros((100, 1), dtype="float32"), 100, None, None)
        assert set(got) == {"aprs", "rec"}

    def test_feeding_withholds_live_audio_except_from_keep_live_owner(self) -> None:
        mgr = AudioDeviceManager()
        got: dict[str, list[int]] = {}
        stream = _stream_with_subscribers(mgr, got, ["aprs", "rec"])
        mgr.begin_feed(3, keep_live_owner="rec")
        stream._on_audio(np.zeros((100, 1), dtype="float32"), 100, None, None)
        assert set(got) == {"rec"}

        mgr.end_feed(3)
        got.clear()
        stream._on_audio(np.zeros((100, 1), dtype="float32"), 100, None, None)
        assert set(got) == {"aprs", "rec"}

    def test_feed_reaches_subscribers_except_the_excluded_owner(self) -> None:
        mgr = AudioDeviceManager()
        got: dict[str, list[int]] = {}
        _stream_with_subscribers(mgr, got, ["aprs", "sstv", "rec"])
        mgr.feed(3, np.zeros(480, dtype="float32"), _HW_SAMPLE_RATE, exclude_owner="rec")
        assert set(got) == {"aprs", "sstv"}

    def test_feed_resamples_to_each_subscribers_rate(self) -> None:
        mgr = AudioDeviceManager()
        stream = _SharedInputStream(3)
        got: list[int] = []
        stream._subscribers["aprs"] = (24_000, lambda chunk: got.append(len(chunk)))
        mgr._inputs[3] = stream
        mgr.feed(3, np.zeros(480, dtype="float32"), 48_000)
        assert got == [240]

    def test_feed_without_subscribers_is_a_no_op(self) -> None:
        AudioDeviceManager().feed(7, np.zeros(10, dtype="float32"), 48_000)

    def test_begin_and_end_nest(self) -> None:
        mgr = AudioDeviceManager()
        mgr.begin_feed(3)
        mgr.begin_feed(3)
        mgr.end_feed(3)
        assert mgr._feed_state(3)[0] is True
        mgr.end_feed(3)
        assert mgr._feed_state(3)[0] is False
        mgr.end_feed(3)  # extra end is harmless
        assert mgr._feed_state(3)[0] is False

    def test_device_feed_sink_drives_the_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mgr = AudioDeviceManager()
        monkeypatch.setattr("comms.audio_device_manager.get_audio_device_manager", lambda: mgr)
        got: dict[str, list[int]] = {}
        _stream_with_subscribers(mgr, got, ["aprs", "rec"])
        sink = DeviceFeedSink(3, exclude_owner="rec")
        sink.begin()
        assert mgr._feed_state(3) == (True, "rec")
        sink.push(np.zeros(48, dtype="float32"), _HW_SAMPLE_RATE)
        assert set(got) == {"aprs"}
        sink.end()
        assert mgr._feed_state(3)[0] is False
