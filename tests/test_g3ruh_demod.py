"""Unit tests for comms/aprs/g3ruh_demod.py — the raw FM discriminator used
to feed SDR-derived audio into Direwolf's built-in G3RUH 9600bps decoder.

Verifies the discriminator math (not Direwolf's own G3RUH decode, which
isn't reachable without the real binary) using a synthetic constant-
frequency-offset I/Q signal, whose recovered discriminator value has a
known closed form — see test_process_recovers_constant_frequency_offset.
"""

from __future__ import annotations

import numpy as np
import pytest

from comms.aprs.g3ruh_demod import _AUDIO_RATE, _DEVIATION_HZ, G3ruhDiscriminator

pytest.importorskip("scipy")

_INPUT_RATE = 960_000.0  # divides cleanly toward both ~200kHz and 48kHz stages


def _constant_offset_iq(delta_f: float, n: int, rate: float) -> np.ndarray:
    """A carrier held at a constant delta_f offset from centre — the FM
    discriminator should recover a constant value proportional to delta_f."""
    t = np.arange(n) / rate
    iq: np.ndarray = np.exp(1j * 2.0 * np.pi * delta_f * t).astype(np.complex64)
    return iq


def test_process_empty_input_returns_empty() -> None:
    disc = G3ruhDiscriminator(input_rate=_INPUT_RATE)
    out = disc.process(np.array([], dtype=np.complex64))
    assert len(out) == 0


def test_process_output_dtype_and_range() -> None:
    disc = G3ruhDiscriminator(input_rate=_INPUT_RATE)
    iq = _constant_offset_iq(1000.0, 20_000, _INPUT_RATE)
    out = disc.process(iq)
    assert len(out) > 0
    assert out.dtype == np.float32
    assert np.all(out >= -1.0) and np.all(out <= 1.0)


def test_process_recovers_constant_frequency_offset() -> None:
    """A carrier offset by delta_f (well within the IF passband) should
    settle to a discriminator output of approximately delta_f / DEVIATION_HZ
    once the DC-block / IF-filter transients have decayed."""
    disc = G3ruhDiscriminator(input_rate=_INPUT_RATE)
    delta_f = 2_500.0  # < _DEVIATION_HZ, safely inside the IF passband
    iq = _constant_offset_iq(delta_f, 96_000, _INPUT_RATE)
    out = disc.process(iq)
    assert len(out) > 0

    # Discard the settling region (filter transients from zero initial
    # state) and check the steady-state value.
    steady = out[len(out) // 2 :]
    expected = delta_f / _DEVIATION_HZ
    assert steady.mean() == pytest.approx(expected, abs=0.05)


def test_process_negative_offset_gives_negative_output() -> None:
    disc = G3ruhDiscriminator(input_rate=_INPUT_RATE)
    iq = _constant_offset_iq(-2_500.0, 96_000, _INPUT_RATE)
    out = disc.process(iq)
    steady = out[len(out) // 2 :]
    assert steady.mean() < 0


def test_output_rate_matches_audio_rate() -> None:
    """decim1/decim2 should bring the input rate down to _AUDIO_RATE, so the
    number of output samples should scale accordingly (within rounding)."""
    disc = G3ruhDiscriminator(input_rate=_INPUT_RATE)
    n_in = 96_000
    iq = _constant_offset_iq(1000.0, n_in, _INPUT_RATE)
    out = disc.process(iq)
    expected_n_out = n_in * _AUDIO_RATE / _INPUT_RATE
    # Naive stride decimation in two integer stages — allow some slack.
    assert abs(len(out) - expected_n_out) < expected_n_out * 0.2
