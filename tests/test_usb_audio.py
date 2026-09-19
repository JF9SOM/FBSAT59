"""Tests for sdr/usb_audio.py: SDR I/Q -> 12 kHz USB audio for FT4 / Q65."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

pytest.importorskip("scipy")

from sdr.usb_audio import (  # noqa: E402 -- must follow importorskip above
    OUT_RATE,
    SdrUsbAudioTap,
    UsbAudio12k,
    _decimation_factors,
    _smooth_decimation,
)

_BLOCK = 16_384


def _tone_iq(freq_hz: float, n: int, rate: float, amplitude: float = 0.01) -> np.ndarray:
    """A complex tone at *freq_hz* from the tuned frequency: +f is upper sideband."""
    iq: np.ndarray = (amplitude * np.exp(2j * np.pi * freq_hz * np.arange(n) / rate)).astype(
        np.complex64
    )
    return iq


def _run(conv: UsbAudio12k, iq: np.ndarray, block: int = _BLOCK) -> np.ndarray:
    return np.concatenate([conv.process(iq[i : i + block]) for i in range(0, len(iq), block)])


def _tone_hz(audio: np.ndarray) -> float:
    x = audio[len(audio) // 4 :]
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)), 1 << 18))
    return float(np.argmax(spectrum) * OUT_RATE / (1 << 18))


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


@pytest.mark.parametrize("rate", [250_000.0, 960_000.0, 1_024_000.0, 2_400_000.0, 3_200_000.0])
def test_output_is_exactly_12khz(rate: float) -> None:
    conv = UsbAudio12k(rate)
    n_blocks = 30
    out = sum(len(conv.process(np.zeros(_BLOCK, dtype=np.complex64))) for _ in range(n_blocks))
    assert out / (n_blocks * _BLOCK / rate) == pytest.approx(OUT_RATE, rel=3e-4)


@pytest.mark.parametrize("rate", [250_000.0, 2_400_000.0])
@pytest.mark.parametrize("tone", [300.0, 1_000.0, 2_500.0, 3_200.0])
def test_tones_keep_their_true_audio_frequency(rate: float, tone: float) -> None:
    """A signal `tone` Hz above the tuned frequency must be a `tone` Hz audio tone
    (the SDR's own SSB demodulator moved it down by 1350 Hz)."""
    conv = UsbAudio12k(rate)
    audio = _run(conv, _tone_iq(tone, _BLOCK * 30, rate))
    assert _tone_hz(audio) == pytest.approx(tone, abs=2.0)


@pytest.mark.parametrize("tone", [1_000.0, 2_500.0])
def test_the_opposite_sideband_is_rejected(tone: float) -> None:
    """A signal *below* the tuned frequency (lower sideband) must not turn into
    a mirror-image tone in the audio."""
    rate = 250_000.0
    wanted = _run(UsbAudio12k(rate), _tone_iq(tone, _BLOCK * 30, rate))
    image = _run(UsbAudio12k(rate), _tone_iq(-tone, _BLOCK * 30, rate))
    # Both are AGC-levelled, so compare the raw filter response instead: feed
    # the image together with a wanted tone of equal strength elsewhere and see
    # that the image adds nothing at its mirror frequency.
    conv = UsbAudio12k(rate)
    both = _tone_iq(1_800.0, _BLOCK * 30, rate) + _tone_iq(-tone, _BLOCK * 30, rate)
    mixed = _run(conv, both)[len(image) // 4 :]
    spectrum = np.abs(np.fft.rfft(mixed * np.hanning(len(mixed)), 1 << 18))
    freqs = np.arange(len(spectrum)) * OUT_RATE / (1 << 18)
    peak = spectrum.max()
    at = lambda f: spectrum[np.argmin(np.abs(freqs - f))]  # noqa: E731
    assert at(tone) < 0.01 * peak  # < -40 dB where a mirror image would sit
    assert wanted is not None


def test_block_size_does_not_change_the_audio() -> None:
    rate = 250_000.0
    rng = np.random.default_rng(2)
    n = _BLOCK * 20
    iq = (
        _tone_iq(1_500.0, n, rate) + 0.005 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    ).astype(np.complex64)
    a = _run(UsbAudio12k(rate), iq, _BLOCK)
    b = _run(UsbAudio12k(rate), iq, 5_003)
    m = min(len(a), len(b))
    a, b = a[600 : m - 600], b[600 : m - 600]
    # The slow AGC's gain follows the chunking slightly, so compare the
    # waveforms with the level taken out.
    assert np.max(np.abs(a / _rms(a) - b / _rms(b))) < 2e-2


def test_output_level_is_normalised_whatever_the_sdr_gain() -> None:
    rate = 250_000.0
    for amplitude in (1e-4, 1e-2, 0.5):
        audio = _run(UsbAudio12k(rate), _tone_iq(1_000.0, _BLOCK * 60, rate, amplitude))
        assert _rms(audio[-12_000:]) == pytest.approx(0.1, rel=0.15)


def test_decimation_helpers() -> None:
    assert _smooth_decimation(5.0) == 5
    assert _smooth_decimation(19.2) == 18  # 19 is prime
    assert _smooth_decimation(0.4) == 1
    for total in (1, 5, 18, 20, 48, 64):
        factors = _decimation_factors(total)
        assert int(np.prod(factors)) == total
        assert all(f <= 8 for f in factors)


def test_tap_delivers_audio_on_its_own_thread_and_stops() -> None:
    received: list[np.ndarray] = []
    thread_names: list[str] = []

    def on_audio(chunk: np.ndarray) -> None:
        received.append(chunk)
        thread_names.append(threading.current_thread().name)

    rate = 250_000.0
    tap = SdrUsbAudioTap(rate, on_audio)
    tap.start()
    iq = _tone_iq(1_000.0, _BLOCK * 10, rate)
    for i in range(0, len(iq), _BLOCK):
        tap.push_samples(iq[i : i + _BLOCK])
    deadline = time.monotonic() + 5.0
    while sum(len(c) for c in received) < 5_000 and time.monotonic() < deadline:
        time.sleep(0.01)
    tap.stop()

    assert sum(len(c) for c in received) > 5_000
    assert set(thread_names) == {"sdr-usb-audio"}
    assert tap._thread is None


def test_tap_never_blocks_the_caller_when_the_consumer_is_slow() -> None:
    def slow(chunk: np.ndarray) -> None:
        time.sleep(0.5)

    tap = SdrUsbAudioTap(250_000.0, slow)
    tap.start()
    iq = _tone_iq(1_000.0, _BLOCK, 250_000.0)
    start = time.monotonic()
    for _ in range(400):
        tap.push_samples(iq)
    elapsed = time.monotonic() - start
    tap.stop()

    assert elapsed < 1.0
    assert tap.dropped_blocks > 0


# ---------------------------------------------------------------------------
# End to end: a real FT4 transmission, delivered as SDR I/Q, must decode.
# ---------------------------------------------------------------------------


def _ft4_codec_or_skip():  # type: ignore[no-untyped-def]
    from comms.ft4.codec import Ft4Codec

    codec = Ft4Codec()
    if not codec.decode_available or codec.encode_audio("CQ JF9SOM PM95", 1500.0) is None:
        pytest.skip("FT4 codec library not installed")
    return codec


@pytest.mark.parametrize("audio_hz", [400.0, 1_500.0, 2_800.0])
def test_ft4_transmission_decodes_from_sdr_iq(audio_hz: float) -> None:
    from fractions import Fraction

    import scipy.signal as sg

    codec = _ft4_codec_or_skip()
    message = "CQ JF9SOM PM95"
    period = int(7.5 * OUT_RATE)
    tx = codec.encode_audio(message, base_freq=audio_hz)
    audio = np.zeros(period, dtype=np.float32)
    start = int(0.5 * OUT_RATE)
    audio[start : start + len(tx)] = tx[: period - start]

    # What an SDR tuned to the dial frequency sees of a USB signal: the
    # analytic signal of the audio, at the SDR's sample rate.
    rate = 250_000
    ratio = Fraction(rate, OUT_RATE)
    iq = sg.resample_poly(sg.hilbert(audio.astype(np.float64)), ratio.numerator, ratio.denominator)
    iq = (iq * 0.005).astype(np.complex64)  # a typical, low SDR I/Q level

    out = _run(UsbAudio12k(float(rate)), iq)
    decoded = codec.decode_audio(
        np.pad(out, (0, max(0, period - len(out))))[:period], OUT_RATE, "JF9SOM"
    )

    assert message in [m.text for m in decoded]
