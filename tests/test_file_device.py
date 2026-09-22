"""Tests for sdr/file_device.py — SdrFileDevice, the file-backed pseudo
SdrDevice used for IQ recording playback (see docs/sdr.md's playback
section).

Requires scipy (skipped otherwise, matching test_sdr_pipeline.py):
SdrFileDevice.__init__ uses scipy.io.wavfile.read() to load the WAV.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")

import scipy.io.wavfile as wav  # noqa: E402 -- must follow importorskip above

from sdr.file_device import SdrFileDevice  # noqa: E402

_SAMPLE_RATE = 1000  # Hz -- small and round, keeps test durations tiny


def _write_test_wav(path: Path, num_samples: int, sample_rate: int = _SAMPLE_RATE) -> np.ndarray:
    """Write a synthetic complex64 signal as a CF32 stereo WAV, matching
    sdr.recorder.IQRecorder's own format exactly (I=left/real,
    Q=right/imag), and return the samples that were written (as
    complex64) for assertions.
    """
    t = np.arange(num_samples, dtype=np.float32) / sample_rate
    # An easily-identifiable tone rather than noise, so seeking to a known
    # sample index has a predictable value to assert against.
    samples = (np.cos(2 * np.pi * 5.0 * t) + 1j * np.sin(2 * np.pi * 5.0 * t)).astype(np.complex64)
    stereo = np.empty((num_samples, 2), dtype=np.float32)
    stereo[:, 0] = samples.real
    stereo[:, 1] = samples.imag
    wav.write(str(path), sample_rate, stereo)
    return samples


def test_sample_rate_and_duration(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=2000)  # 2s at 1000 Hz
    dev = SdrFileDevice(path)
    assert dev.sample_rate == _SAMPLE_RATE
    assert dev.duration_s == pytest.approx(2.0)


def test_center_freq_is_fixed_at_zero(tmp_path: Path) -> None:
    """See file_device.py's module docstring for why this must never move."""
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    assert dev.center_freq == 0.0
    assert dev.set_center_freq(437_505_000.0) is True
    assert dev.center_freq == 0.0  # unchanged


def test_read_samples_returns_none_before_start_stream(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    assert dev.read_samples(10) is None


def test_read_samples_advances_position_and_matches_recorded_data(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    samples = _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    dev.start_stream()
    block = dev.read_samples(30)
    assert block is not None
    assert len(block) == 30
    np.testing.assert_allclose(block, samples[:30], atol=1e-6)
    assert dev.position_s == pytest.approx(30 / _SAMPLE_RATE)


def test_read_samples_returns_none_at_end_of_file(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=50)
    dev = SdrFileDevice(path)
    dev.start_stream()
    dev.read_samples(50)
    assert dev.at_end is True
    assert dev.read_samples(10) is None


def test_read_samples_returns_partial_block_at_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    samples = _write_test_wav(path, num_samples=50)
    dev = SdrFileDevice(path)
    dev.start_stream()
    dev.read_samples(45)
    block = dev.read_samples(20)  # only 5 samples remain
    assert block is not None
    assert len(block) == 5
    np.testing.assert_allclose(block, samples[45:50], atol=1e-6)


def test_seek_jumps_read_position(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    samples = _write_test_wav(path, num_samples=1000)
    dev = SdrFileDevice(path)
    dev.start_stream()
    dev.seek(0.5)  # 500 samples in at 1000 Hz
    assert dev.position_s == pytest.approx(0.5)
    block = dev.read_samples(10)
    assert block is not None
    np.testing.assert_allclose(block, samples[500:510], atol=1e-6)


def test_seek_clamps_to_valid_range(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    dev.seek(-5.0)
    assert dev.position_s == 0.0
    dev.seek(1000.0)  # far beyond the 0.1s recording
    assert dev.position_s == pytest.approx(dev.duration_s)


def test_stop_stream_makes_read_samples_return_none(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    dev.start_stream()
    dev.read_samples(10)
    dev.stop_stream()
    assert dev.read_samples(10) is None
    # Position is unaffected by stopping -- resuming continues where it
    # left off (SdrControlWidget's "Stop" == pause, not reset).
    assert dev.position_s == pytest.approx(10 / _SAMPLE_RATE)


def test_close_frees_the_buffer(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    dev.close()
    assert dev.duration_s == 0.0


def test_start_time_comes_from_the_file_name(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    path = tmp_path / "0_unknown_20260920T065551Z.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    assert dev.start_time_utc == datetime(2026, 9, 20, 6, 55, 51, tzinfo=UTC)
    assert dev.start_time_confirmed


def test_start_time_is_none_without_a_time_in_the_name_and_can_be_set(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=100)
    dev = SdrFileDevice(path)
    assert dev.start_time_utc is None
    assert not dev.start_time_confirmed

    dev.set_start_time_utc(datetime(2026, 9, 20, 1, 2, 3))  # naive -> taken as UTC
    assert dev.start_time_utc == datetime(2026, 9, 20, 1, 2, 3, tzinfo=UTC)
    assert dev.start_time_confirmed  # set by the user
    dev.set_start_time_utc(datetime(2026, 9, 20, 0, 0, 0), confirmed=False)  # a placeholder
    assert not dev.start_time_confirmed
    dev.set_start_time_utc(None)
    assert dev.start_time_utc is None
    assert not dev.start_time_confirmed


class _FakeClock:
    """A controllable clock for pacing tests -- see the tests below.

    Patched in for both time.monotonic() (what read_samples() checks its
    target against) and time.sleep() (which advances the clock instead of
    actually blocking, so these tests run instantly).
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        if seconds > 0:
            self.now += seconds

    def advance(self, seconds: float) -> None:
        """Simulate wall-clock time passing outside of read_samples() --
        e.g. the rest of SDRPipeline.run() doing Doppler correction,
        demodulation and IQ recording between calls."""
        self.now += seconds


def test_read_samples_paces_each_block_to_real_time(tmp_path: Path, monkeypatch) -> None:
    """With no external delay, each block still sleeps out its own nominal duration."""
    import sdr.file_device as file_device_module

    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=1000)  # 1s at 1000 Hz
    dev = SdrFileDevice(path)

    clock = _FakeClock()
    monkeypatch.setattr(file_device_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(file_device_module.time, "sleep", clock.sleep)

    dev.start_stream()
    for _ in range(10):
        dev.read_samples(100)  # 100 samples = 0.1s at 1000 Hz

    assert clock.now == pytest.approx(1000.0 + 1.0)  # 10 blocks * 0.1s
    assert sum(clock.sleep_calls) == pytest.approx(1.0)


def test_read_samples_catches_up_after_slow_processing(tmp_path: Path, monkeypatch) -> None:
    """A slow iteration must shorten the *next* sleep instead of the delay
    just adding onto the total -- this is the whole point of pacing off a
    fixed reference instead of sleep(block_duration) every call (see
    read_samples()'s docstring: this is what was causing SDRPipeline's
    audio output queue to run dry during file playback).
    """
    import sdr.file_device as file_device_module

    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=1000)  # 1s at 1000 Hz
    dev = SdrFileDevice(path)

    clock = _FakeClock()
    monkeypatch.setattr(file_device_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(file_device_module.time, "sleep", clock.sleep)

    dev.start_stream()
    dev.read_samples(100)  # nominal: sleeps 0.1s
    clock.advance(0.05)  # simulate 50ms of processing elsewhere in the loop
    dev.read_samples(100)  # should only sleep ~0.05s to stay on schedule, not 0.1s

    assert clock.sleep_calls[0] == pytest.approx(0.1)
    assert clock.sleep_calls[1] == pytest.approx(0.05, abs=1e-9)
    # The 50ms external delay was fully absorbed by shortening the next
    # sleep (it was less than that block's own 0.1s budget), so the clock
    # lands exactly on the fixed reference's schedule for 200 samples --
    # no drift accumulated, unlike the old sleep(block_duration) every call.
    assert clock.now == pytest.approx(1000.0 + 0.2)


def test_read_samples_never_sleeps_negative_when_badly_behind(tmp_path: Path, monkeypatch) -> None:
    """If external processing eats more than a block's own duration, the
    next read must not sleep at all (not a negative/zero-clamped call) --
    it should just return immediately to help the pipeline catch up.
    """
    import sdr.file_device as file_device_module

    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=1000)
    dev = SdrFileDevice(path)

    clock = _FakeClock()
    monkeypatch.setattr(file_device_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(file_device_module.time, "sleep", clock.sleep)

    dev.start_stream()
    clock.advance(5.0)  # already far behind before the first read
    dev.read_samples(100)

    assert clock.sleep_calls == [0.0] or clock.sleep_calls == []


def test_seek_resets_the_pacing_reference(tmp_path: Path, monkeypatch) -> None:
    """Without a reset, seeking backward would make read_samples() try to
    sleep out the (now huge, stale) gap back to the old reference; seeking
    forward would make it sleep 0 for a long stretch. Neither should happen.
    """
    import sdr.file_device as file_device_module

    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=1000)  # 1s at 1000 Hz
    dev = SdrFileDevice(path)

    clock = _FakeClock()
    monkeypatch.setattr(file_device_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(file_device_module.time, "sleep", clock.sleep)

    dev.start_stream()
    dev.read_samples(500)  # halfway through, clock now at +0.5s
    dev.seek(0.1)  # jump back near the start
    clock.sleep_calls.clear()
    dev.read_samples(100)  # should pace like a fresh 0.1s block, not -0.4s

    assert clock.sleep_calls == [pytest.approx(0.1)]


def test_is_streaming_follows_start_and_stop_and_keeps_the_position(tmp_path: Path) -> None:
    path = tmp_path / "test.iq.wav"
    _write_test_wav(path, num_samples=2000)
    dev = SdrFileDevice(path)
    assert not dev.is_streaming
    dev.start_stream()
    assert dev.is_streaming
    dev.read_samples(500)
    dev.stop_stream()
    assert not dev.is_streaming
    assert dev.position_s == pytest.approx(0.5)  # a pause keeps the place
    dev.start_stream()  # ... and playing again continues from it
    block = dev.read_samples(500)
    assert block is not None
    assert dev.position_s == pytest.approx(1.0)
