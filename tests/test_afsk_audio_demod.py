"""Unit tests for comms/aprs/afsk_audio_demod.py -- the de-emphasized NFM
audio recovery that feeds Direwolf's Bell 202 (1200 baud) decoder.

Shares its streaming DSP with G3ruhDiscriminator (see test_g3ruh_demod.py for
the block-boundary regression rationale); these tests cover what is specific
to the AFSK profile: the IF width and the de-emphasis stage.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from comms.aprs.afsk_audio_demod import _AFSK_PROFILE, AfskAudioDiscriminator

pytest.importorskip("scipy")

_RATE = 250_000.0


def _carrier(delta_f: float, n: int) -> np.ndarray:
    iq: np.ndarray = np.exp(2j * np.pi * delta_f * np.arange(n) / _RATE).astype(np.complex64)
    return iq


def _blockwise(disc: AfskAudioDiscriminator, iq: np.ndarray, block: int = 16_384) -> np.ndarray:
    return np.concatenate([disc.process(iq[i : i + block]) for i in range(0, len(iq), block)])


def test_block_boundaries_do_not_disturb_the_output() -> None:
    """The IF filter must carry its state across SDRPipeline blocks -- a
    per-block restart glitches the discriminator at every boundary."""
    disc = AfskAudioDiscriminator(input_rate=_RATE)
    out = _blockwise(disc, _carrier(1_500.0, 16_384 * 8))
    steady = out[len(out) // 4 :]
    # De-emphasis is DC-transparent, so a steady offset reads delta_f / full scale.
    err = np.abs(steady - 1_500.0 / _AFSK_PROFILE.full_scale_hz)
    assert np.max(err) < 0.3
    assert np.mean(err) < 0.005


def test_decimating_input_rate_keeps_state_across_blocks() -> None:
    rate = 2_400_000.0
    disc = AfskAudioDiscriminator(input_rate=rate)
    iq = np.exp(2j * np.pi * 1_500.0 * np.arange(16_384 * 8) / rate).astype(np.complex64)
    out = _blockwise(disc, iq)
    err = np.abs(out[len(out) // 4 :] - 1_500.0 / _AFSK_PROFILE.full_scale_hz)
    assert np.max(err) < 0.3
    assert np.mean(err) < 0.005


def test_if_rejects_a_strong_out_of_band_interferer() -> None:
    """A carrier 15 kHz off-centre, 10 dB stronger than the wanted one, must
    not capture the discriminator once the IF has removed it."""
    n = 65_536
    iq = (_carrier(1_000.0, n) + 3.0 * _carrier(15_000.0, n)).astype(np.complex64)
    out = AfskAudioDiscriminator(input_rate=_RATE).process(iq)
    reading_hz = float(np.mean(out[len(out) // 2 :]) * _AFSK_PROFILE.full_scale_hz)
    assert reading_hz == pytest.approx(1_000.0, abs=300.0)


def _fm_audio_amplitude(
    audio_hz: float, *, de_emphasis: bool, deviation_hz: float = 3_000.0
) -> float:
    """Fundamental of the recovered audio for an FM carrier modulated by a tone."""
    n = 65_536
    t = np.arange(n) / _RATE
    phase = 2 * np.pi * np.cumsum(deviation_hz * np.sin(2 * np.pi * audio_hz * t)) / _RATE
    iq = np.exp(1j * phase).astype(np.complex64)
    profile = _AFSK_PROFILE if de_emphasis else replace(_AFSK_PROFILE, deemph_tau_s=None)
    out = AfskAudioDiscriminator(input_rate=_RATE, profile=profile).process(iq)
    settled = out[len(out) // 3 :]
    tt = np.arange(len(settled)) / 48_000.0
    return float(2 * abs(np.mean(settled * np.exp(-2j * np.pi * audio_hz * tt))))


def test_de_emphasis_is_applied() -> None:
    """The 75 us de-emphasis stage attenuates a 2200 Hz tone (theory: x0.69)
    but leaves 300 Hz alone (x0.99). Measured against the same chain without
    the stage, so the discriminator's own response cancels out."""
    high = _fm_audio_amplitude(2_200.0, de_emphasis=True) / _fm_audio_amplitude(
        2_200.0, de_emphasis=False
    )
    low = _fm_audio_amplitude(300.0, de_emphasis=True) / _fm_audio_amplitude(
        300.0, de_emphasis=False
    )
    assert low == pytest.approx(1.0, abs=0.05)
    assert high < 0.85
