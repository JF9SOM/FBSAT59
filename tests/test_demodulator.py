"""Tests for sdr/demodulator.py's audio-rate handling.

The demodulator used to decimate by integer strides only, so its output was
never actually at AUDIO_RATE (48 kHz): 250 kS/s USB/CW came out at 62.5 kHz,
NFM at 50 kHz, while the speaker, CW Decoder, FT4, Q65, SSTV and the MP3
recorder all treated the samples as 48 kHz -- wrong pitch, and (with the
speaker on) audio produced faster than it plays.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")

from sdr.demodulator import (  # noqa: E402 -- must follow importorskip above
    AUDIO_RATE,
    DemodMode,
    Demodulator,
    _StreamResampler,
    _StrideDecimator,
)

_BLOCK = 16_384  # SDRPipeline's block size


def _run(demod: Demodulator, iq: np.ndarray, block: int = _BLOCK) -> np.ndarray:
    return np.concatenate([demod.process(iq[i : i + block]) for i in range(0, len(iq), block)])


def _tone_hz(audio: np.ndarray) -> float:
    """Frequency of the strongest tone, taking the samples to be at AUDIO_RATE."""
    x = audio[len(audio) // 4 :]
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)), 1 << 18))
    return float(np.argmax(spectrum) * AUDIO_RATE / (1 << 18))


@pytest.mark.parametrize("rate", [250_000.0, 960_000.0, 1_024_000.0, 2_400_000.0])
@pytest.mark.parametrize("mode", list(DemodMode))
def test_output_rate_is_exactly_the_audio_rate(rate: float, mode: DemodMode) -> None:
    demod = Demodulator(input_rate=rate)
    demod.set_mode(mode)
    n_blocks = 30
    out = sum(len(demod.process(np.zeros(_BLOCK, dtype=np.complex64))) for _ in range(n_blocks))
    seconds = n_blocks * _BLOCK / rate
    assert out / seconds == pytest.approx(AUDIO_RATE, rel=2e-4)


@pytest.mark.parametrize("rate", [250_000.0, 1_024_000.0])
def test_cw_tone_keeps_its_pitch(rate: float) -> None:
    n = _BLOCK * 30
    iq = np.exp(2j * np.pi * 1_000.0 * np.arange(n) / rate).astype(np.complex64)
    demod = Demodulator(input_rate=rate)
    demod.set_mode(DemodMode.CW)
    demod.set_agc(False)
    assert _tone_hz(_run(demod, iq)) == pytest.approx(1_000.0, abs=3.0)


@pytest.mark.parametrize("rate", [250_000.0, 1_024_000.0])
def test_nfm_audio_tone_keeps_its_pitch(rate: float) -> None:
    n = _BLOCK * 30
    t = np.arange(n) / rate
    phase = 2 * np.pi * np.cumsum(3_000.0 * np.sin(2 * np.pi * 1_000.0 * t)) / rate
    demod = Demodulator(input_rate=rate)
    demod.set_mode(DemodMode.NFM)
    demod.set_agc(False)
    out = _run(demod, np.exp(1j * phase).astype(np.complex64))
    assert _tone_hz(out) == pytest.approx(1_000.0, abs=3.0)


def test_resampler_output_matches_the_ideal_samples() -> None:
    """A 1 kHz sine at 250 kS/s must come out as that same sine sampled at 48 kHz."""
    rate = 250_000.0
    n = 100_000
    x = np.sin(2 * np.pi * 1_000.0 * np.arange(n) / rate).astype(np.float32)
    resampler = _StreamResampler(rate)
    out = np.concatenate([resampler.process(x[i : i + 7_777]) for i in range(0, n, 7_777)])

    ideal = np.sin(2 * np.pi * 1_000.0 * np.arange(len(out)) / AUDIO_RATE)
    assert np.max(np.abs(out[10:] - ideal[10:])) < 5e-3


def test_resampler_is_independent_of_block_size() -> None:
    """No seams: chunking the input differently gives the same samples."""
    rate = 250_000.0
    x = np.random.default_rng(1).standard_normal(60_000).astype(np.float32)

    def run(block: int) -> np.ndarray:
        r = _StreamResampler(rate)
        return np.concatenate([r.process(x[i : i + block]) for i in range(0, len(x), block)])

    a, b = run(16_384), run(5_003)
    n = min(len(a), len(b))
    assert n > 10_000
    assert np.allclose(a[:n], b[:n], atol=1e-5)


def test_resampler_handles_complex_input() -> None:
    r = _StreamResampler(250_000.0)
    out = r.process(np.ones(5_000, dtype=np.complex64) * (1 + 2j))
    assert np.iscomplexobj(out)
    assert np.allclose(out[5:], 1 + 2j, atol=1e-4)


def test_stride_decimator_keeps_phase_across_blocks() -> None:
    x = np.arange(1_000, dtype=np.float32)
    dec = _StrideDecimator(10)
    out = np.concatenate([dec.process(x[i : i + 137]) for i in range(0, len(x), 137)])
    assert np.array_equal(out, x[::10])


def _noisy_tone(rate: float, n: int, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    tone = np.exp(2j * np.pi * 1_200.0 * np.arange(n) / rate)
    return (tone + 0.3 * noise).astype(np.complex64)


def _noisy_fm(rate: float, n: int, seed: int = 4) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n) / rate
    phase = 2 * np.pi * np.cumsum(3_000.0 * np.sin(2 * np.pi * 1_000.0 * t)) / rate
    noise = 0.05 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return (np.exp(1j * phase) + noise).astype(np.complex64)


@pytest.mark.parametrize("mode", list(DemodMode))
def test_chunking_does_not_change_the_output(mode: DemodMode) -> None:
    """Regression: the BFO phase and every IF / audio filter used to restart at
    each 16384-sample block, putting a glitch at every boundary (~15 a second).
    Processed in blocks or in one go, the audio must be the same."""
    rate = 250_000.0
    n = _BLOCK * 20
    if mode is DemodMode.NFM:
        iq = _noisy_fm(rate, n)
    elif mode is DemodMode.LSB:
        iq = np.conj(_noisy_tone(rate, n))
    else:
        iq = _noisy_tone(rate, n)

    def render(block: int) -> np.ndarray:
        demod = Demodulator(input_rate=rate)
        demod.set_mode(mode)
        demod.set_agc(False)
        return _run(demod, iq, block)

    blocked, whole = render(_BLOCK), render(n)
    m = min(len(blocked), len(whole))
    a, b = blocked[500 : m - 500], whole[500 : m - 500]
    assert np.max(np.abs(a - b)) / np.sqrt(np.mean(b**2)) < 1e-3


@pytest.mark.parametrize("rate", [250_000.0, 2_400_000.0])
def test_ssb_tone_stays_pure_across_blocks(rate: float) -> None:
    """A USB carrier 2000 Hz above the tuned frequency must come out as a clean
    2000 Hz tone. (With the BFO phase restarting every block the tone was
    smeared; before 2026-09-19 it also came out at 650 Hz -- see below.)"""
    n = _BLOCK * 40
    iq = np.exp(2j * np.pi * 2_000.0 * np.arange(n) / rate).astype(np.complex64)
    demod = Demodulator(input_rate=rate)
    demod.set_mode(DemodMode.USB)
    demod.set_agc(False)
    out = _run(demod, iq)[2_000:].astype(np.float64)

    t = np.arange(len(out)) / AUDIO_RATE
    carrier = np.exp(-2j * np.pi * 2_000.0 * t)
    amplitude = 2 * np.mean(out * carrier)
    residual = out - np.real(amplitude * np.conj(carrier))
    assert np.sqrt(np.mean(residual**2)) < 0.02 * abs(amplitude) / np.sqrt(2)


def _ssb_tone(
    mode: DemodMode, offset_hz: float, rate: float = 250_000.0, ssb_bw: float | None = None
) -> np.ndarray:
    """Demodulated audio for a unit-amplitude carrier *offset_hz* from the tuned frequency."""
    n = _BLOCK * 30
    iq = np.exp(2j * np.pi * offset_hz * np.arange(n) / rate).astype(np.complex64)
    demod = Demodulator(input_rate=rate)
    demod.set_mode(mode)
    demod.set_agc(False)
    if ssb_bw is not None:
        demod.set_ssb_bandwidth(ssb_bw)
    return _run(demod, iq)


def _level(audio: np.ndarray) -> float:
    """RMS level of the settled audio."""
    return float(np.sqrt(np.mean(audio[len(audio) // 3 :].astype(np.float64) ** 2)))


@pytest.mark.parametrize("offset", [300.0, 1_000.0, 1_350.0, 1_700.0, 2_000.0])
def test_usb_keeps_the_true_audio_pitch(offset: float) -> None:
    """A signal f Hz above the tuned frequency is heard as f Hz. The old
    demodulator moved everything down by 1350 Hz and folded what was left,
    so 1000 Hz and 1700 Hz both came out as 350 Hz."""
    assert _tone_hz(_ssb_tone(DemodMode.USB, offset)) == pytest.approx(offset, abs=2.0)


@pytest.mark.parametrize("offset", [-500.0, -1_000.0, -2_000.0])
def test_lsb_keeps_the_true_audio_pitch(offset: float) -> None:
    """LSB is the mirror image: a signal f Hz *below* the tuned frequency is f Hz."""
    assert _tone_hz(_ssb_tone(DemodMode.LSB, offset)) == pytest.approx(-offset, abs=2.0)


@pytest.mark.parametrize("offset", [-500.0, -1_000.0, -2_000.0])
def test_usb_rejects_the_lower_sideband(offset: float) -> None:
    wanted = _level(_ssb_tone(DemodMode.USB, 1_000.0))
    assert _level(_ssb_tone(DemodMode.USB, offset)) < wanted * 10 ** (-40 / 20)


@pytest.mark.parametrize("offset", [500.0, 1_000.0, 2_000.0])
def test_lsb_rejects_the_upper_sideband(offset: float) -> None:
    wanted = _level(_ssb_tone(DemodMode.LSB, -1_000.0))
    assert _level(_ssb_tone(DemodMode.LSB, offset)) < wanted * 10 ** (-40 / 20)


def test_ssb_bandwidth_setting_moves_the_upper_edge() -> None:
    passband = _level(_ssb_tone(DemodMode.USB, 1_000.0))
    narrow = _level(_ssb_tone(DemodMode.USB, 3_500.0, ssb_bw=2_700.0))
    wide = _level(_ssb_tone(DemodMode.USB, 3_500.0, ssb_bw=4_500.0))
    assert narrow < passband * 10 ** (-30 / 20)  # beyond a 2.7 kHz filter
    assert wide == pytest.approx(passband, rel=0.1)  # inside a 4.5 kHz one


@pytest.mark.parametrize("rate", [960_000.0, 2_400_000.0])
def test_usb_pitch_is_true_at_other_sample_rates(rate: float) -> None:
    assert _tone_hz(_ssb_tone(DemodMode.USB, 2_000.0, rate=rate)) == pytest.approx(2_000.0, abs=2.0)
