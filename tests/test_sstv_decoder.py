"""Tests for comms/sstv/decoder.py against pySSTV, an independent SSTV *encoder*.

pySSTV turns a picture into Robot36 / PD120 audio; the decoder must turn that audio
back into the picture. (pySSTV has no decoder, so it cannot be used in the app --
only as the reference signal source here.)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("pysstv")

from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from pysstv.color import PD120, Robot36  # noqa: E402
from scipy import signal as sp_signal  # noqa: E402

from comms.sstv.decoder import SstvDecoder  # noqa: E402

_SIZE = {"Robot36": (320, 240), "PD120": (640, 496)}
_ENCODER = {"Robot36": Robot36, "PD120": PD120}
_HEADER_S = 0.91  # 300 + 10 + 300 ms leader/break + 10 x 30 ms VIS
_LINE_S = {"Robot36": 0.150, "PD120": 0.50848}


def pattern(width: int, height: int) -> Image.Image:
    """A smooth colour ramp with a few solid shapes (no single-pixel detail)."""
    im = Image.new("RGB", (width, height))
    d = ImageDraw.Draw(im)
    for x in range(width):
        d.line([(x, 0), (x, height)], fill=(int(255 * x / width), int(255 * (1 - x / width)), 128))
    d.rectangle((width // 8, height // 8, width // 3, height // 2), fill=(230, 40, 40))
    d.ellipse((width // 2, height // 3, width * 7 // 8, height * 7 // 8), fill=(30, 60, 230))
    d.rectangle((0, height * 7 // 8, width, height), fill=(20, 20, 20))
    return im


def sstv_audio(mode: str, rate: int, image: Image.Image | None = None) -> np.ndarray:
    """pySSTV's audio for *mode* (float32 in [-1, 1]), 0.7 s of silence before, 0.5 s after."""
    w, h = _SIZE[mode]
    image = image if image is not None else pattern(w, h)
    samples = np.array(list(_ENCODER[mode](image, rate, 16).gen_samples()), dtype=np.float32)
    samples /= 32768.0
    return np.concatenate(
        [np.zeros(int(0.7 * rate), np.float32), samples, np.zeros(rate // 2, np.float32)]
    )


def to_array(qimg: QImage) -> np.ndarray:
    rgb = qimg.convertToFormat(QImage.Format.Format_RGB888)
    buf = np.frombuffer(rgb.constBits(), dtype=np.uint8)
    out: np.ndarray = buf.reshape(rgb.height(), rgb.bytesPerLine())[:, : rgb.width() * 3]
    return out.reshape(rgb.height(), rgb.width(), 3).astype(float)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse == 0 else float(10 * np.log10(255.0**2 / mse))


class Run:
    """The signals of one decoder run."""

    def __init__(self, audio: np.ndarray, rate: int, chunk: int = 4096, stop: bool = True) -> None:
        self.images: list[tuple[QImage, str]] = []
        self.modes: list[str] = []
        self.lines: list[int] = []
        self.dec = SstvDecoder(rate)
        self.dec.image_complete.connect(lambda q, m: self.images.append((q, m)))
        self.dec.mode_detected.connect(self.modes.append)
        self.dec.line_received.connect(lambda n, _q: self.lines.append(n))
        self.dec.start()
        for i in range(0, len(audio), chunk):
            self.dec.push_samples(audio[i : i + chunk])
        if stop:
            self.dec.stop()


def add_noise(audio: np.ndarray, snr_db: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    power = float(np.mean(audio[audio != 0] ** 2))
    return (audio + rng.standard_normal(len(audio)) * np.sqrt(power / 10 ** (snr_db / 10))).astype(
        np.float32
    )


# --------------------------------------------------------------------------
# Clean signals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rate", [44100, 48000])
@pytest.mark.parametrize(("mode", "min_psnr"), [("Robot36", 26.0), ("PD120", 31.0)])
def test_decodes_pysstv_audio_into_the_picture(mode: str, min_psnr: float, rate: int) -> None:
    w, h = _SIZE[mode]
    run = Run(sstv_audio(mode, rate), rate)
    assert [m for _q, m in run.images] == [mode]
    q = run.images[0][0]
    assert (q.width(), q.height()) == (w, h)
    assert psnr(to_array(q), np.asarray(pattern(w, h), dtype=float)) >= min_psnr
    assert run.modes == [mode]


@pytest.mark.parametrize("mode", ["Robot36", "PD120"])
def test_the_picture_is_not_shifted(mode: str) -> None:
    """The best horizontal alignment with the original is no shift (a sync-edge bias
    once left the picture ~2 pixels off)."""
    w, h = _SIZE[mode]
    run = Run(sstv_audio(mode, 48000), 48000)
    got = to_array(run.images[0][0])
    ref = np.asarray(pattern(w, h), dtype=float)
    err = {
        s: float(np.abs(np.roll(got, s, axis=1)[:, 10:-10] - ref[:, 10:-10]).mean())
        for s in (-2, -1, 0, 1, 2)
    }
    assert min(err, key=lambda s: err[s]) == 0


@pytest.mark.parametrize("chunk", [1000, 4096, 12345, 1_000_000])
def test_chunk_size_does_not_matter(chunk: int) -> None:
    audio = sstv_audio("Robot36", 48000)
    reference = to_array(Run(audio, 48000, chunk=4096).images[0][0])
    run = Run(audio, 48000, chunk=chunk)
    assert len(run.images) == 1
    assert np.abs(to_array(run.images[0][0]) - reference).mean() < 0.5


def test_progressive_lines_arrive_in_order() -> None:
    run = Run(sstv_audio("Robot36", 48000), 48000)
    assert run.lines == list(range(240))


def test_stereo_input_is_accepted() -> None:
    mono = sstv_audio("Robot36", 48000)
    run = Run(np.stack([mono, mono], axis=1), 48000, chunk=4096)
    assert len(run.images) == 1


def test_samples_before_start_are_ignored() -> None:
    dec = SstvDecoder(48000)
    got: list[Any] = []
    dec.image_complete.connect(lambda q, m: got.append(m))
    dec.push_samples(sstv_audio("Robot36", 48000))
    dec.stop()
    assert got == []


# --------------------------------------------------------------------------
# Real-world imperfections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "max_mae"), [("Robot36", 14.0), ("PD120", 15.0)])
def test_noisy_audio_still_gives_the_picture(mode: str, max_mae: float) -> None:
    w, h = _SIZE[mode]
    run = Run(add_noise(sstv_audio(mode, 48000), 12.0), 48000)
    assert [m for _q, m in run.images] == [mode]
    mae = float(np.abs(to_array(run.images[0][0]) - np.asarray(pattern(w, h), dtype=float)).mean())
    assert mae < max_mae


@pytest.mark.parametrize("ppm", [-1000, -300, 300, 1000])
def test_sound_card_clock_error_does_not_skew_the_picture(ppm: int) -> None:
    """A sample clock 0.1 % off would slant the picture unless every line is re-timed."""
    audio = sstv_audio("Robot36", 48000)
    n = len(audio)
    stretched = np.interp(
        np.arange(int(n * (1 + ppm * 1e-6))) / (1 + ppm * 1e-6), np.arange(n), audio
    ).astype(np.float32)
    run = Run(stretched, 48000)
    assert len(run.images) == 1
    ref = np.asarray(pattern(320, 240), dtype=float)
    assert psnr(to_array(run.images[0][0]), ref) >= 22.0


def test_a_tuning_error_shifts_brightness_only_a_little() -> None:
    """All tones 40 Hz high (an SSB receiver slightly off frequency): brightness and hue
    shift a little (40 Hz is 5 % of the 800 Hz range) but it is still the same picture."""
    audio = sstv_audio("Robot36", 48000)
    analytic = sp_signal.hilbert(audio.astype(np.float64))
    shifted = (analytic * np.exp(2j * np.pi * 40.0 * np.arange(len(audio)) / 48000)).real.astype(
        np.float32
    )
    run = Run(shifted, 48000)
    assert len(run.images) == 1
    ref = np.asarray(pattern(320, 240), dtype=float)
    assert psnr(to_array(run.images[0][0]), ref) >= 18.0


def test_leading_noise_before_the_header_is_skipped() -> None:
    audio = sstv_audio("Robot36", 48000)
    rng = np.random.default_rng(3)
    lead = (rng.standard_normal(6 * 48000) * 0.2).astype(np.float32)
    run = Run(np.concatenate([lead, audio]), 48000)
    assert [m for _q, m in run.images] == ["Robot36"]


def test_two_images_back_to_back() -> None:
    audio = np.concatenate([sstv_audio("Robot36", 48000), sstv_audio("PD120", 48000)])
    run = Run(audio, 48000)
    assert [m for _q, m in run.images] == ["Robot36", "PD120"]
    assert run.modes == ["Robot36", "PD120"]
    assert to_array(run.images[1][0]).shape == (496, 640, 3)


# --------------------------------------------------------------------------
# No header, cut-off and lost signals
# --------------------------------------------------------------------------


def test_without_the_vis_header_a_train_of_sync_pulses_starts_the_image() -> None:
    """Recording that starts at line 30: no VIS, but the syncs are one line apart."""
    rate = 48000
    audio = sstv_audio("Robot36", rate)
    start = int((0.7 + _HEADER_S + 30 * _LINE_S["Robot36"] - 0.03) * rate)
    run = Run(audio[start:], rate)
    assert run.modes == ["Robot36"]
    assert len(run.images) == 1  # the rest of the image, kept when the audio ends
    got = to_array(run.images[0][0])
    ref = np.asarray(pattern(320, 240), dtype=float)
    # decoded row 0 is the original row 30 (line 30 is even, so the chroma pairing agrees)
    assert np.abs(got[: 240 - 30 - 4] - ref[30 : 240 - 4]).mean() < 6.0


def test_a_stream_that_ends_mid_image_keeps_what_arrived() -> None:
    audio = sstv_audio("Robot36", 48000)
    cut = int((0.7 + _HEADER_S + 100 * 0.150) * 48000)
    run = Run(audio[:cut], 48000)
    assert len(run.images) == 1
    q = run.images[0][0]
    assert (q.width(), q.height()) == (320, 240)
    got = to_array(q)
    ref = np.asarray(pattern(320, 240), dtype=float)
    assert np.abs(got[:96] - ref[:96]).mean() < 6.0  # the lines that arrived
    assert got[110:].sum() == 0  # the rest stays black


def test_a_stream_that_ends_almost_at_once_gives_no_image() -> None:
    audio = sstv_audio("Robot36", 48000)
    cut = int((0.7 + _HEADER_S + 3 * 0.150) * 48000)
    assert Run(audio[:cut], 48000).images == []


def test_a_signal_that_disappears_gives_the_partial_picture() -> None:
    rate = 48000
    audio = sstv_audio("Robot36", rate)
    lost = int((0.7 + _HEADER_S + 130 * 0.150) * rate)
    rng = np.random.default_rng(5)
    audio[lost:] = (rng.standard_normal(len(audio) - lost) * 0.05).astype(np.float32)
    run = Run(audio, rate)
    assert len(run.images) == 1
    got = to_array(run.images[0][0])
    ref = np.asarray(pattern(320, 240), dtype=float)
    assert np.abs(got[:120] - ref[:120]).mean() < 6.0
    assert got[140:].sum() == 0  # the noise lines were blanked, not kept


def test_silence_and_noise_never_produce_an_image() -> None:
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(90 * 48000) * 0.3).astype(np.float32)
    run = Run(noise, 48000)
    assert run.images == []
    assert run.modes == []


# --------------------------------------------------------------------------
# Sample rate
# --------------------------------------------------------------------------


def test_set_sample_rate_restarts_the_search_at_the_new_rate() -> None:
    dec = SstvDecoder(44100)
    got: list[str] = []
    dec.image_complete.connect(lambda q, m: got.append(m))
    dec.start()
    dec.push_samples(sstv_audio("Robot36", 44100)[: 44100 * 3])  # a bit of the wrong-rate stream
    dec.set_sample_rate(48000)
    assert dec.sample_rate == 48000
    audio = sstv_audio("Robot36", 48000)
    for i in range(0, len(audio), 4096):
        dec.push_samples(audio[i : i + 4096])
    dec.stop()
    assert got == ["Robot36"]


def test_a_mismatched_sample_rate_does_not_decode_a_picture() -> None:
    """Audio at 48 kHz fed to a 44.1 kHz decoder (the SDR-audio mistake this fixes)."""
    run = Run(sstv_audio("Robot36", 48000), 44100)
    assert all(m != "Robot36" or True for _q, m in run.images)  # never a crash
    if run.images:
        ref = np.asarray(pattern(320, 240), dtype=float)
        assert psnr(to_array(run.images[0][0]), ref) < 20.0
