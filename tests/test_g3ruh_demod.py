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

from comms.aprs.g3ruh_demod import _AUDIO_RATE, G3ruhDiscriminator

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
    settle to a discriminator output of approximately delta_f / full_scale_hz
    once the DC-block / IF-filter transients have decayed."""
    disc = G3ruhDiscriminator(input_rate=_INPUT_RATE)
    delta_f = 2_500.0  # below the full-scale frequency, safely inside the IF passband
    iq = _constant_offset_iq(delta_f, 96_000, _INPUT_RATE)
    out = disc.process(iq)
    assert len(out) > 0

    # Discard the settling region (filter transients from zero initial
    # state) and check the steady-state value.
    steady = out[len(out) // 2 :]
    expected = delta_f / disc.full_scale_hz
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


def _blockwise(disc: G3ruhDiscriminator, iq: np.ndarray, block: int) -> np.ndarray:
    return np.concatenate([disc.process(iq[i : i + block]) for i in range(0, len(iq), block)])


# A steady carrier read back through 16384-sample blocks. The per-block
# rational resampler still leaves a small edge ripple on a couple of output
# samples per block (max error ~0.1); the bug this guards against -- IF
# filter state reset at every block -- put errors of ~0.9 there.
_BOUNDARY_TOLERANCE = 0.3


@pytest.mark.parametrize("baud", [9600, 4800])
def test_block_boundaries_do_not_disturb_the_output(baud: int) -> None:
    """Regression: the IF filter used to restart from zero state on every
    SDRPipeline block (16384 samples, ~65 ms), so each boundary put a
    start-up glitch into the discriminator output -- enough to break any
    frame longer than a block (measured: 40 dB SNR synthetic 9600 baud
    frames decoded 8/40 through Direwolf instead of 40/40)."""
    rate = 250_000.0
    disc = G3ruhDiscriminator(input_rate=rate, baud=baud)
    block = 16_384
    iq = _constant_offset_iq(1_500.0, block * 8, rate)
    out = _blockwise(disc, iq, block)

    steady = out[len(out) // 4 :]  # skip the first block's settling
    err = np.abs(steady - 1_500.0 / disc.full_scale_hz)
    assert np.max(err) < _BOUNDARY_TOLERANCE
    assert np.mean(err) < 0.005


def test_block_size_does_not_change_the_result() -> None:
    """Chunking the same input differently must give (nearly) the same audio."""
    rate = 250_000.0
    iq = _constant_offset_iq(-2_000.0, 16_384 * 4, rate)
    a = _blockwise(G3ruhDiscriminator(input_rate=rate), iq, 16_384)
    b = _blockwise(G3ruhDiscriminator(input_rate=rate), iq, 5_000)
    n = min(len(a), len(b))
    assert abs(len(a) - len(b)) < 30
    diff = np.abs(a[n // 4 : n - 200] - b[n // 4 : n - 200])
    assert np.mean(diff) < 0.01


def test_decimating_input_rate_keeps_state_across_blocks() -> None:
    """Same boundary check through the decimating (stage-1 filter) path,
    with a block size that is not a multiple of the decimation factor."""
    rate = 1_024_000.0
    disc = G3ruhDiscriminator(input_rate=rate)
    iq = _constant_offset_iq(1_000.0, 16_384 * 8, rate)
    out = _blockwise(disc, iq, 16_384)
    err = np.abs(out[len(out) // 4 :] - 1_000.0 / disc.full_scale_hz)
    assert np.max(err) < _BOUNDARY_TOLERANCE
    assert np.mean(err) < 0.005


def _capture(baud: int, wanted_hz: float, interferer_hz: float) -> float:
    """Mean discriminator reading (in Hz) with a wanted carrier and a 10 dB
    stronger interferer -- FM captures the stronger one unless the IF filter
    removes it first."""
    rate = 250_000.0
    n = 65_536
    iq = _constant_offset_iq(wanted_hz, n, rate) + 3.0 * _constant_offset_iq(interferer_hz, n, rate)
    disc = G3ruhDiscriminator(input_rate=rate, baud=baud)
    out = disc.process(iq.astype(np.complex64))
    return float(np.mean(out[len(out) // 2 :]) * disc.full_scale_hz)


def test_9600_profile_rejects_an_out_of_band_interferer() -> None:
    """The narrow 9600 IF removes a strong carrier 12 kHz off-centre so the
    in-band signal (+1 kHz) is what the discriminator follows, whereas the
    wide legacy IF (still used for 4800) lets the interferer capture it."""
    assert _capture(9600, 1_000.0, 12_000.0) == pytest.approx(1_000.0, abs=300.0)
    assert _capture(4800, 1_000.0, 12_000.0) > 4_000.0


@pytest.mark.parametrize("tone_hz", [4_000.0, -4_000.0])
def test_9600_profile_passes_the_signal_band(tone_hz: float) -> None:
    """A carrier at +/-4 kHz (inside the FSK signal band) still reads correctly."""
    assert _capture(9600, tone_hz, tone_hz) == pytest.approx(tone_hz, rel=0.02)
