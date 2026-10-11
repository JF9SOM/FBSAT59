"""Tests for comms/sstv/sdr_demod.py: the SSTV tab's private FM demodulator for SDR input."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("pysstv")

from PIL import Image, ImageDraw  # noqa: E402
from pysstv.color import Robot36  # noqa: E402
from scipy import signal as sp_signal  # noqa: E402

from comms.sstv.decoder import SstvDecoder  # noqa: E402
from comms.sstv.sdr_demod import SstvFmDemod, output_rate_for  # noqa: E402

_SDR_RATE = 250_000


def _picture() -> Image.Image:
    im = Image.new("RGB", (320, 240))
    d = ImageDraw.Draw(im)
    for x in range(320):
        d.line([(x, 0), (x, 240)], fill=(int(255 * x / 320), int(255 * (1 - x / 320)), 128))
    d.rectangle((40, 30, 110, 110), fill=(230, 40, 40))
    d.ellipse((160, 80, 280, 210), fill=(30, 60, 230))
    return im


def _fm_iq(
    audio: np.ndarray, audio_rate: int, deviation_hz: float, noise: float = 0.0
) -> np.ndarray:
    """FM-modulate *audio* (+-1 = +-deviation) onto a 0 Hz carrier, sampled at _SDR_RATE."""
    up = sp_signal.resample_poly(audio, _SDR_RATE, audio_rate)
    phase = 2 * np.pi * deviation_hz * np.cumsum(up) / _SDR_RATE
    iq = np.exp(1j * phase)
    if noise:
        rng = np.random.default_rng(2)
        iq = iq + noise * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
    return iq.astype(np.complex64)


def _sstv_audio() -> np.ndarray:
    rate = 48_000
    samples = np.array(list(Robot36(_picture(), rate, 16).gen_samples()), dtype=np.float32)
    # the SSTV tones as a +-1 signal: 1500..2300 Hz of tone is carried as audio, not as a
    # frequency offset -- pySSTV's samples are the audio waveform itself
    audio = samples / 32768.0
    return np.concatenate([np.zeros(rate // 2, np.float32), audio, np.zeros(rate // 2, np.float32)])


def _decode(iq: np.ndarray, chunk: int = 16384) -> tuple[np.ndarray | None, int]:
    demod = SstvFmDemod(_SDR_RATE)
    dec = SstvDecoder(demod.output_rate)
    images: list = []
    dec.image_complete.connect(lambda q, _m: images.append(q))
    dec.start()
    for i in range(0, len(iq), chunk):
        dec.push_samples(demod.process(iq[i : i + chunk]))
    dec.stop()
    if not images:
        return None, demod.output_rate
    q = images[0]
    buf = q.constBits().tobytes()
    got = np.frombuffer(buf, dtype=np.uint8).reshape(q.height(), q.bytesPerLine())[
        :, : q.width() * 3
    ]
    return got.reshape(q.height(), q.width(), 3).astype(float), demod.output_rate


def test_output_rate_follows_the_sdr_rate() -> None:
    assert output_rate_for(250_000) == 50_000
    assert output_rate_for(2_400_000) == 50_000
    assert output_rate_for(1_024_000) == 51_200
    assert output_rate_for(48_000) == 48_000
    assert SstvFmDemod(250_000).output_rate == output_rate_for(250_000)


def test_block_size_does_not_change_the_output() -> None:
    iq = _fm_iq(_sstv_audio()[:60_000], 48_000, 3_000.0)
    a = np.concatenate([SstvFmDemod(_SDR_RATE).process(iq)])
    demod = SstvFmDemod(_SDR_RATE)
    b = np.concatenate([demod.process(iq[i : i + 3_333]) for i in range(0, len(iq), 3_333)])
    assert len(a) == len(b)
    assert np.abs(a - b).max() < 1e-4


def test_recovers_the_modulating_tone() -> None:
    """A 1900 Hz tone at +-3 kHz deviation comes out as a 1900 Hz tone of the right size."""
    t = np.arange(48_000) / 48_000
    iq = _fm_iq(np.sin(2 * np.pi * 1900 * t).astype(np.float32), 48_000, 3_000.0)
    demod = SstvFmDemod(_SDR_RATE)
    out = demod.process(iq)[5_000:]
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    freqs = np.fft.rfftfreq(len(out), 1 / demod.output_rate)
    assert abs(freqs[int(np.argmax(spec))] - 1900) < 10
    assert 0.5 < out.std() * np.sqrt(2) < 0.7  # 3 kHz of 5 kHz full scale = 0.6


def test_a_fm_modulated_picture_is_decoded() -> None:
    got, rate = _decode(_fm_iq(_sstv_audio(), 48_000, 3_000.0))
    assert rate == 50_000
    assert got is not None
    ref = np.asarray(_picture(), dtype=float)
    assert np.abs(got - ref).mean() < 12.0


def test_a_noisy_fm_picture_is_still_decoded() -> None:
    got, _ = _decode(_fm_iq(_sstv_audio(), 48_000, 3_000.0, noise=0.12))
    assert got is not None
    ref = np.asarray(_picture(), dtype=float)
    assert np.abs(got - ref).mean() < 25.0
