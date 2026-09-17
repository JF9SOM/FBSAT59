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
