"""RX-side audio Doppler correction (ft4 ADC RX): dial track + frequency-shift maths."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.ft4.rx_doppler import (
    SAMPLE_RATE,
    DialTrack,
    apply_rx_correction,
    shift_profile_hz,
)

FS = SAMPLE_RATE


def _tone(freq_hz: NDArray[np.float64]) -> NDArray[np.float32]:
    """Real tone whose instantaneous frequency follows *freq_hz* (one value per sample)."""
    return np.sin(2 * np.pi * np.cumsum(freq_hz) / FS).astype(np.float32)


def _freq_near(y: NDArray[np.float32], t_s: float, width_s: float = 0.5) -> float:
    i0 = int((t_s - width_s / 2) * FS)
    seg = y[i0 : i0 + int(width_s * FS)] * np.hanning(int(width_s * FS))
    spec = np.abs(np.fft.rfft(seg, 8 * len(seg)))
    return float(np.argmax(spec) * FS / (8 * len(seg)))


def test_dial_track_is_a_step_function() -> None:
    track = DialTrack()
    assert track.value_at(10.0) is None
    track.add_write(100.0, 435_620_000.0)
    track.add_write(107.5, 435_618_000.0)
    assert track.value_at(99.0) == 435_620_000.0  # before the first write: its value
    assert track.value_at(100.0) == 435_620_000.0
    assert track.value_at(107.4) == 435_620_000.0
    assert track.value_at(107.5) == 435_618_000.0
    assert len(track) == 2


def test_confirm_adopts_the_radios_value_only_when_it_differs() -> None:
    track = DialTrack()
    track.add_write(1.0, 435_620_000.0)
    assert track.confirm(435_620_004.0) is False  # the radio's own rounding: not worth a warning
    assert track.value_at(2.0) == 435_620_004.0  # ...but it is what the dial really is
    assert track.confirm(435_619_000.0) is True  # the write was dropped: dial stayed put
    assert track.value_at(2.0) == 435_619_000.0


def test_shift_profile_is_dial_minus_target() -> None:
    track = DialTrack()
    track.add_write(0.0, 1000.0)
    # target falls 40 Hz/s; the audio ends at t=8 s and is 7.5 s long
    prof = shift_profile_hz(int(7.5 * FS), 8.0, track, lambda t: 1000.0 - 40.0 * (t - 0.5))
    assert prof is not None
    assert prof[0] == pytest.approx(0.0, abs=0.01)  # t=0.5: target equals the dial
    assert prof[-1] == pytest.approx(40.0 * 7.5, rel=1e-3)


def test_unknown_dial_or_target_leaves_audio_alone() -> None:
    audio = np.zeros(int(7.5 * FS), dtype=np.float32)
    out, rng = apply_rx_correction(audio, 8.0, DialTrack(), lambda t: 1.0)
    assert out is audio and rng is None
    track = DialTrack()
    track.add_write(0.0, 1.0)
    out, rng = apply_rx_correction(audio, 8.0, track, lambda t: None)
    assert out is audio and rng is None


def test_a_sliding_tone_is_straightened() -> None:
    """A signal that should sit at 1500 Hz but slides because the dial stayed put
    ends up at 1500 Hz all through the period once corrected."""
    n = int(7.5 * FS)
    t = np.arange(n) / FS
    t_end = 7.5
    dial_hz = 435_000_000.0

    def target(abs_t: float) -> float:  # ideal dial: rises 55 Hz/s over the period
        return dial_hz + 55.0 * (abs_t - 3.75)

    # with the dial fixed, the audio drifts by target(t) - dial
    slide = np.array([target(x) - dial_hz for x in t])
    audio = _tone(1500.0 + slide)
    assert abs(_freq_near(audio, 0.5) - 1500.0) > 100  # ~-190 Hz away at the start
    track = DialTrack()
    track.add_write(-1.0, dial_hz)
    fixed, rng = apply_rx_correction(audio, t_end, track, target)
    assert rng is not None and rng[0] == pytest.approx(-55.0 * 3.75, abs=1.0)
    for at in (0.5, 3.75, 7.0):
        assert _freq_near(fixed, at) == pytest.approx(1500.0, abs=3.0)


def test_the_wrong_direction_makes_it_worse() -> None:
    n = int(7.5 * FS)
    t = np.arange(n) / FS
    dial_hz = 435_000_000.0
    slide = 55.0 * (t - 3.75)
    audio = _tone(1500.0 + slide)
    track = DialTrack()
    track.add_write(-1.0, dial_hz)
    # a target that moves the opposite way would double the slide instead of removing it
    wrong, _ = apply_rx_correction(audio, 7.5, track, lambda a: dial_hz - 55.0 * (a - 3.75))
    assert abs(_freq_near(wrong, 0.5) - 1500.0) > 300
