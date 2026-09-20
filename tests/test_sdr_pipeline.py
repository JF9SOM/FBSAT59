"""Tests for SDRPipeline's digital Doppler correction (NCO).

Verifies the frequency-shifting math directly (no QThread.start()/run() —
these call the correction/FFT helpers as plain methods), following the
same synthetic-signal approach as test_g3ruh_demod.py. A QApplication is
required to construct any QObject-derived class (SDRPipeline is a
QThread), so every test takes the qtbot fixture purely to guarantee one
exists for the session — no widget is ever created or shown.

Requires scipy (skipped otherwise, matching test_g3ruh_demod.py): CI only
installs `.[dev]`, not the optional `[sdr]` extra, and SDRPipeline.__init__
always builds a Demodulator (used for the NFM/USB/CW audio-out path, not
touched by anything tested here) which itself hard-requires scipy — so
just importing SDRPipeline already needs it, before any test body runs.
The importorskip must come before that import (unlike g3ruh_demod, whose
own import is scipy-safe and only needs the guard for its later use).
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from sdr.pipeline import SDRPipeline  # noqa: E402 -- must follow importorskip above

_SAMPLE_RATE = 250_000.0
_HW_CF = 435_000_000.0


class _FakeDevice:
    """Minimal stand-in exposing just what SDRPipeline reads (center_freq,
    sample_rate) — matches the duck-typing already used for
    _FakeSdrDevice in test_rig.py's SdrRigAdapter tests."""

    def __init__(self, center_freq: float = _HW_CF, sample_rate: float = _SAMPLE_RATE) -> None:
        self.center_freq = center_freq
        self.sample_rate = sample_rate


def _make_pipeline(
    qtbot: QtBot, center_freq: float = _HW_CF, sample_rate: float = _SAMPLE_RATE
) -> SDRPipeline:
    del qtbot  # only needed to guarantee a QApplication exists
    return SDRPipeline(_FakeDevice(center_freq, sample_rate))


class TestNoDopplerTarget:
    def test_iq_passes_through_unchanged(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot)
        rng = np.random.default_rng(0)
        iq = (rng.standard_normal(100) + 1j * rng.standard_normal(100)).astype(np.complex64)
        out = pipeline._apply_doppler_correction(iq)
        assert np.array_equal(out, iq)

    def test_effective_center_freq_falls_back_to_hardware(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot, center_freq=437_000_000.0)
        assert pipeline.effective_center_freq == 437_000_000.0


class TestDopplerCorrectionShiftsSignalToBaseband:
    def test_tone_at_target_frequency_lands_at_dc(self, qtbot: QtBot) -> None:
        """A tone physically present at *target* (i.e. at target-hw_cf Hz
        in the raw baseband) must land at ~0 Hz after correction — the
        whole point of tracking a Doppler-shifted downlink."""
        target = _HW_CF + 3_000.0  # 3 kHz above the (fixed) hardware tuning
        pipeline = _make_pipeline(qtbot)
        pipeline.set_doppler_target(target)

        n = 4096
        t = np.arange(n) / _SAMPLE_RATE
        raw_offset_hz = target - _HW_CF
        iq = np.exp(1j * 2 * np.pi * raw_offset_hz * t).astype(np.complex64)

        corrected = pipeline._apply_doppler_correction(iq)
        spectrum = np.abs(np.fft.fft(corrected))
        peak_bin = int(np.argmax(spectrum))
        # Bin 0 is 0 Hz; n-1 is the adjacent negative-frequency bin (a
        # residual sub-bin-width offset can land either side of it).
        assert peak_bin in (0, 1, n - 1)

    def test_no_shift_when_target_equals_hardware_freq(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot)
        pipeline.set_doppler_target(_HW_CF)
        iq = (np.random.default_rng(1).standard_normal(50) + 1j * 0).astype(np.complex64)
        out = pipeline._apply_doppler_correction(iq)
        assert np.array_equal(out, iq)

    def test_phase_is_continuous_across_block_boundaries(self, qtbot: QtBot) -> None:
        """Correcting one long block must give the same result as
        correcting two consecutive shorter blocks — i.e. no click at the
        boundary from resetting the NCO phase every call."""
        target = _HW_CF + 1_234.0
        pipeline_whole = _make_pipeline(qtbot)
        pipeline_whole.set_doppler_target(target)
        pipeline_split = _make_pipeline(qtbot)
        pipeline_split.set_doppler_target(target)

        rng = np.random.default_rng(42)
        iq_full = (rng.standard_normal(2000) + 1j * rng.standard_normal(2000)).astype(np.complex64)

        corrected_whole = pipeline_whole._apply_doppler_correction(iq_full.copy())
        part1 = pipeline_split._apply_doppler_correction(iq_full[:800].copy())
        part2 = pipeline_split._apply_doppler_correction(iq_full[800:].copy())
        corrected_split = np.concatenate([part1, part2])

        np.testing.assert_allclose(corrected_whole, corrected_split, atol=1e-5)

    def test_changing_target_between_blocks_does_not_reset_phase(self, qtbot: QtBot) -> None:
        """Doppler drift changes the target every call in real use; the
        running phase must still carry over (only the *rate* of rotation
        changes, not a phase jump)."""
        pipeline = _make_pipeline(qtbot)
        pipeline.set_doppler_target(_HW_CF + 100.0)
        pipeline._apply_doppler_correction(np.ones(500, dtype=np.complex64))
        phase_after_first = pipeline._nco_phase

        pipeline.set_doppler_target(_HW_CF + 250.0)
        pipeline._apply_doppler_correction(np.ones(500, dtype=np.complex64))
        # The second block's starting phase must be exactly where the
        # first one left off (not reset to 0), even though the shift
        # amount changed in between.
        assert phase_after_first != 0.0
        assert pipeline._nco_phase != phase_after_first


class TestHardwareRetuneResetsPhaseReference:
    def test_phase_resets_when_device_center_freq_changes(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot)
        pipeline.set_doppler_target(_HW_CF + 1_000.0)
        pipeline._apply_doppler_correction(np.ones(100, dtype=np.complex64))
        assert pipeline._nco_phase != 0.0

        pipeline._device.center_freq = 437_000_000.0  # simulate a real hardware retune
        # An empty block still runs the reset check but contributes zero
        # phase advance of its own, isolating "did the reset happen" from
        # this call's own (nonzero, at the new huge shift) contribution.
        pipeline._apply_doppler_correction(np.zeros(0, dtype=np.complex64))
        assert pipeline._nco_phase == 0.0


class TestEffectiveCenterFreqAndFftAxis:
    def test_effective_center_freq_returns_target_while_tracking(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot)
        pipeline.set_doppler_target(_HW_CF + 2_500.0)
        assert pipeline.effective_center_freq == _HW_CF + 2_500.0

    def test_fft_axis_is_centered_on_the_doppler_target_not_hardware_freq(
        self, qtbot: QtBot
    ) -> None:
        """The waterfall/spectrum axis must reflect what's actually being
        tracked (the target) so a correctly-tracked signal shows up
        centered, not offset by however far the (rarely-retuned) hardware
        frequency has drifted from it."""
        target = _HW_CF + 10_000.0
        pipeline = _make_pipeline(qtbot)
        pipeline.set_doppler_target(target)

        spectrum = pipeline._compute_fft(np.zeros(2048, dtype=np.complex64))
        freqs = [f for f, _ in spectrum]
        center_freq = freqs[len(freqs) // 2]
        assert center_freq == pytest.approx(target)


# ---------------------------------------------------------------------------
# Speaker playback must not slow the SDR read loop
# ---------------------------------------------------------------------------


class _SlowStream:
    """sounddevice.OutputStream stand-in whose write() is paced like real audio."""

    def __init__(self, write_s: float = 0.15) -> None:
        self.write_s = write_s
        self.writes = 0
        self.closed = False

    def start(self) -> None:
        pass

    def write(self, pcm: np.ndarray) -> None:
        time.sleep(self.write_s)
        self.writes += 1

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _install_fake_sounddevice(monkeypatch: pytest.MonkeyPatch, stream: _SlowStream) -> None:
    fake = types.ModuleType("sounddevice")
    fake.OutputStream = lambda **kwargs: stream  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", fake)


class TestAudioPlaybackIsOffTheReadLoop:
    """Regression: OutputStream.write() blocks at audio real-time speed, and
    calling it from the pipeline thread made each iteration ~82 ms for a
    65.5 ms block, so the SDR's buffer overflowed and ~20% of all samples
    (live decoding and IQ recordings alike) were dropped."""

    def test_play_audio_never_blocks_the_caller(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _SlowStream(write_s=0.15)
        _install_fake_sounddevice(monkeypatch, stream)
        pipeline = _make_pipeline(qtbot)
        pipeline.set_audio_enabled(True)
        pcm = np.zeros(3_146, dtype=np.float32)

        start = time.monotonic()
        for _ in range(40):
            pipeline._play_audio(pcm)
        elapsed = time.monotonic() - start

        # 40 blocks x 150 ms of blocking writes would take 6 s inline.
        assert elapsed < 0.5
        # The writer can't keep up, so the oldest queued blocks are dropped
        # rather than piling up (or slowing the caller).
        assert pipeline._diag_audio_dropped > 0
        qtbot.waitUntil(lambda: stream.writes >= 1, timeout=3_000)

        pipeline.set_audio_enabled(False)

    def test_disabling_audio_stops_the_writer_and_closes_the_stream(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _SlowStream(write_s=0.01)
        _install_fake_sounddevice(monkeypatch, stream)
        pipeline = _make_pipeline(qtbot)
        pipeline.set_audio_enabled(True)
        pipeline._play_audio(np.zeros(3_146, dtype=np.float32))
        qtbot.waitUntil(lambda: stream.writes >= 1, timeout=3_000)
        writer = pipeline._audio_thread
        assert writer is not None and writer.is_alive()

        pipeline.set_audio_enabled(False)

        assert not writer.is_alive()
        assert stream.closed
        assert pipeline._audio_queue.empty()

    def test_audio_can_be_switched_on_again(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _SlowStream(write_s=0.01)
        _install_fake_sounddevice(monkeypatch, stream)
        pipeline = _make_pipeline(qtbot)
        pcm = np.zeros(3_146, dtype=np.float32)
        pipeline.set_audio_enabled(True)
        pipeline._play_audio(pcm)
        qtbot.waitUntil(lambda: stream.writes >= 1, timeout=3_000)
        pipeline.set_audio_enabled(False)
        before = stream.writes

        pipeline.set_audio_enabled(True)
        pipeline._play_audio(pcm)

        qtbot.waitUntil(lambda: stream.writes > before, timeout=3_000)
        pipeline.set_audio_enabled(False)

    def test_play_audio_after_disable_starts_no_writer(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _SlowStream()
        _install_fake_sounddevice(monkeypatch, stream)
        pipeline = _make_pipeline(qtbot)

        pipeline._play_audio(np.zeros(3_146, dtype=np.float32))  # audio never enabled

        assert pipeline._audio_thread is None


# ---------------------------------------------------------------------------
# Stall watchdog
# ---------------------------------------------------------------------------


class _StallingDevice:
    """A device that stops delivering samples until it is 'healed'."""

    def __init__(self, heals_on: str | None) -> None:
        self.sample_rate = _SAMPLE_RATE
        self.center_freq = _HW_CF
        self.heals_on = heals_on  # "restart", "reopen" or None (never)
        self.stalled = True
        self.restart_calls = 0
        self.reopen_calls = 0

    def start_stream(self) -> bool:
        return True

    def stop_stream(self) -> None:
        pass

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        time.sleep(0.005)
        if self.stalled:
            return None
        return np.zeros(num_samples, dtype=np.complex64)

    def restart_stream(self) -> bool:
        self.restart_calls += 1
        if self.heals_on == "restart":
            self.stalled = False
        return True

    def reopen(self) -> bool:
        self.reopen_calls += 1
        if self.heals_on in ("restart", "reopen"):
            self.stalled = False
        return True


class _SilentDevice:
    """Returns nothing and defines no recovery methods, like a paused SdrFileDevice."""

    sample_rate = _SAMPLE_RATE
    center_freq = _HW_CF

    def start_stream(self) -> bool:
        return True

    def stop_stream(self) -> None:
        pass

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        time.sleep(0.005)
        return None


def _run_pipeline(
    qtbot: QtBot, device: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[SDRPipeline, list[int]]:
    del qtbot
    monkeypatch.setattr("sdr.pipeline._STALL_TIMEOUT_S", 0.2)
    pipeline = SDRPipeline(device)
    blocks: list[int] = []
    pipeline.subscribe(lambda iq: blocks.append(len(iq)))
    pipeline.start()
    return pipeline, blocks


def _stop(pipeline: SDRPipeline) -> None:
    pipeline.stop()
    assert pipeline.wait(3_000)


class TestStallWatchdog:
    def test_a_stalled_stream_is_restarted_first(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = _StallingDevice(heals_on="restart")
        pipeline, blocks = _run_pipeline(qtbot, device, monkeypatch)
        try:
            qtbot.waitUntil(lambda: len(blocks) > 0, timeout=5_000)
        finally:
            _stop(pipeline)

        assert device.restart_calls == 1
        assert device.reopen_calls == 0

    def test_a_device_that_stays_silent_is_reopened(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = _StallingDevice(heals_on="reopen")
        pipeline, blocks = _run_pipeline(qtbot, device, monkeypatch)
        try:
            qtbot.waitUntil(lambda: len(blocks) > 0, timeout=5_000)
        finally:
            _stop(pipeline)

        assert device.restart_calls == 1  # tried the cheap fix first
        assert device.reopen_calls >= 1

    def test_no_recovery_while_samples_keep_arriving(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = _StallingDevice(heals_on=None)
        device.stalled = False
        pipeline, blocks = _run_pipeline(qtbot, device, monkeypatch)
        try:
            qtbot.waitUntil(lambda: len(blocks) > 5, timeout=5_000)
            time.sleep(0.5)  # well past the (patched) 0.2 s stall timeout
        finally:
            _stop(pipeline)

        assert device.restart_calls == 0
        assert device.reopen_calls == 0

    def test_devices_without_recovery_support_are_left_alone(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A paused or finished IQ recording (SdrFileDevice) legitimately
        returns nothing; the watchdog must not touch it."""
        device = _SilentDevice()
        pipeline, blocks = _run_pipeline(qtbot, device, monkeypatch)
        try:
            time.sleep(0.7)
            assert pipeline.isRunning()
        finally:
            _stop(pipeline)

        assert blocks == []


class _NoiseDevice:
    """Delivers noise blocks at roughly real-time pace, like a live SDR."""

    sample_rate = _SAMPLE_RATE
    center_freq = _HW_CF

    def __init__(self) -> None:
        self._rng = np.random.default_rng(0)

    def start_stream(self) -> bool:
        return True

    def stop_stream(self) -> None:
        pass

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        time.sleep(0.01)
        z = self._rng.standard_normal(num_samples) + 1j * self._rng.standard_normal(num_samples)
        return z.astype(np.complex64)


class _RealTimeNoiseDevice(_NoiseDevice):
    """Noise delivered at exactly the pace the samples would arrive live."""

    def read_samples(self, num_samples: int = 1024) -> np.ndarray | None:
        time.sleep(num_samples / self.sample_rate)
        z = self._rng.standard_normal(num_samples) + 1j * self._rng.standard_normal(num_samples)
        return z.astype(np.complex64)


class TestBurstDetectionSwitch:
    def test_off_by_default(self, qtbot: QtBot) -> None:
        assert _make_pipeline(qtbot)._burst_detector is None

    def test_switching_on_keeps_state_and_off_discards_it(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot)
        pipeline.set_burst_detection(True)
        detector = pipeline._burst_detector
        assert detector is not None
        pipeline.set_burst_detection(True)  # already on: keep baseline and counter
        assert pipeline._burst_detector is detector
        pipeline.set_burst_detection(False)
        assert pipeline._burst_detector is None

    def test_rows_are_emitted_only_while_switched_on(self, qtbot: QtBot) -> None:
        pipeline = SDRPipeline(_NoiseDevice())
        rows: list[Any] = []
        pipeline.burst_row_ready.connect(rows.append)
        pipeline.start()
        try:
            with qtbot.waitSignal(pipeline.spectrum_ready, timeout=3_000):
                pass
            assert rows == []  # switched off: nothing is computed or emitted

            pipeline.set_burst_detection(True)
            qtbot.waitUntil(lambda: len(rows) >= 2, timeout=3_000)
        finally:
            _stop(pipeline)

        row = rows[0]
        assert len(row.power_dbfs) == len(row.freqs_hz) == 1024
        assert row.warming_up is True


class _PositionedDevice:
    """A file-like device that reports how far into its data it has read."""

    sample_rate = _SAMPLE_RATE
    center_freq = _HW_CF
    position_s = 471.5


class TestBurstTimeReference:
    def test_wall_clock_for_a_live_device(self, qtbot: QtBot) -> None:
        pipeline = _make_pipeline(qtbot)
        before = time.time()
        reference = pipeline._burst_time_reference()
        assert before <= reference <= time.time()

    def test_file_position_while_playing_back_a_recording(self, qtbot: QtBot) -> None:
        del qtbot
        pipeline = SDRPipeline(_PositionedDevice())
        assert pipeline._burst_time_reference() == 471.5

    def test_a_running_iq_recording_does_not_change_the_live_reference(self, qtbot: QtBot) -> None:
        """Live burst times are clock times whether or not an IQ recording runs."""
        pipeline = _make_pipeline(qtbot)
        pipeline._recorder._recording = True
        before = time.time()
        assert pipeline._burst_time_reference() >= before

    def test_rows_carry_increasing_times(self, qtbot: QtBot) -> None:
        # Real-time pace: a row's start time is "now" minus the signal it
        # covers, so a device running faster than real time would make the
        # (longer) later rows start earlier.
        pipeline = SDRPipeline(_RealTimeNoiseDevice())
        rows: list[Any] = []
        pipeline.burst_row_ready.connect(rows.append)
        pipeline.set_burst_detection(True)
        pipeline.start()
        try:
            qtbot.waitUntil(lambda: len(rows) >= 3, timeout=3_000)
        finally:
            _stop(pipeline)
        assert rows[0].time_s >= 0.0
        assert rows[1].time_s > rows[0].time_s
        assert rows[2].time_s > rows[1].time_s
