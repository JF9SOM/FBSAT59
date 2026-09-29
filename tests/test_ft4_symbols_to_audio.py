"""
Unit tests for comms/ft4/codec.py's symbols_to_audio() — specifically the
freq_offset_hz parameter used for FT4's TX-time residual Doppler correction
(see docs/hamlib.md "FT4 送信中ドップラー残差補正"). Pure Python, no ft8_lib
dependency (unlike test_ft4_codec.py, which needs the shared library for
encode/decode round-trips) — symbols_to_audio() only needs a raw tone array.
"""

from __future__ import annotations

import numpy as np

from comms.ft4.codec import FT4_SAMPLES_PER_SYM, FT4_TONE_SPACING, SAMPLE_RATE, symbols_to_audio


def _dominant_freq_hz(segment: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    """Estimate the dominant tone frequency of a single-symbol audio segment."""
    spectrum = np.abs(np.fft.rfft(segment))
    freqs = np.fft.rfftfreq(len(segment), d=1.0 / sample_rate)
    return float(freqs[int(np.argmax(spectrum))])


def test_no_offset_matches_previous_behavior() -> None:
    """freq_offset_hz=None (the default) must reproduce the pre-existing output exactly."""
    tones = bytes([0, 1, 2, 3, 0, 1])
    audio_default = symbols_to_audio(tones, base_freq=1000.0)
    audio_explicit_none = symbols_to_audio(tones, base_freq=1000.0, freq_offset_hz=None)
    np.testing.assert_array_equal(audio_default, audio_explicit_none)


def test_constant_offset_shifts_every_symbols_tone() -> None:
    """A constant offset must shift every symbol's tone by that many Hz."""
    tones = bytes([0, 0, 0, 0])
    base_freq = 1000.0
    offset_hz = 50.0
    audio = symbols_to_audio(tones, base_freq=base_freq, freq_offset_hz=lambda _t: offset_hz)
    spf = FT4_SAMPLES_PER_SYM
    for i in range(len(tones)):
        segment = audio[i * spf : (i + 1) * spf]
        measured = _dominant_freq_hz(segment)
        assert abs(measured - (base_freq + offset_hz)) < 25.0  # within one FFT bin


def test_time_varying_offset_ramps_the_tone() -> None:
    """A linear ramp must produce a measurably higher tone late in the burst than early."""
    tones = bytes([0] * 20)
    base_freq = 1000.0

    def _ramp(t: float) -> float:
        return 100.0 * t  # 100 Hz/s -- well above the ~21 Hz FFT bin resolution
        # of a single 48 ms symbol over this test's ~1 s burst.

    audio = symbols_to_audio(tones, base_freq=base_freq, freq_offset_hz=_ramp)
    spf = FT4_SAMPLES_PER_SYM
    first = _dominant_freq_hz(audio[0:spf])
    last = _dominant_freq_hz(audio[19 * spf : 20 * spf])
    assert last - first > 50.0


def test_offset_respects_tone_spacing() -> None:
    """Offset adds to the per-symbol tone (base_freq + tone*spacing), not in place of it."""
    tones = bytes([2])
    base_freq = 1000.0
    offset_hz = 30.0
    audio = symbols_to_audio(tones, base_freq=base_freq, freq_offset_hz=lambda _t: offset_hz)
    measured = _dominant_freq_hz(audio)
    expected = base_freq + 2 * FT4_TONE_SPACING + offset_hz
    assert abs(measured - expected) < 25.0
