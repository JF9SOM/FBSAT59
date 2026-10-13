"""Tests for comms.cw.codec.centre_carrier (moves an out-of-band CW carrier into the model band)."""

from __future__ import annotations

import numpy as np
import pytest

from comms.cw.codec import SAMPLE_RATE, centre_carrier


def _keyed_tone(freq_hz: float, seconds: float = 8.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    key = (np.floor(t / 0.2) % 2 == 0).astype(np.float64)
    rng = np.random.default_rng(1)
    return (np.sin(2 * np.pi * freq_hz * t) * key + 0.02 * rng.standard_normal(len(t))).astype(
        np.float32
    )


def _peak_hz(audio: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    return float(np.fft.rfftfreq(len(audio), 1.0 / SAMPLE_RATE)[int(np.argmax(spectrum))])


@pytest.mark.parametrize("freq", [250.0, 300.0, 1450.0])
def test_out_of_band_carrier_is_moved_to_target(freq: float) -> None:
    out = centre_carrier(_keyed_tone(freq))
    assert abs(_peak_hz(out) - 700.0) < 5.0


@pytest.mark.parametrize("freq", [500.0, 700.0, 1100.0])
def test_in_band_carrier_is_left_alone(freq: float) -> None:
    audio = _keyed_tone(freq)
    assert centre_carrier(audio) is audio


def test_noise_only_is_left_alone() -> None:
    noise = np.random.default_rng(2).standard_normal(8 * SAMPLE_RATE).astype(np.float32)
    assert centre_carrier(noise) is noise
