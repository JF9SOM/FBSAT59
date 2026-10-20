"""Foresail-1p uplink waveform -> I/Q -> receiver round trip.

Needs scipy and reed-solomon-ccsds (skipped when they are missing).
"""

from __future__ import annotations

from math import gcd

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("reed_solomon_ccsds")

from scipy import signal  # noqa: E402

from comms.foresail.rx import ForesailRxStream, decode_iq  # noqa: E402
from comms.foresail.skylink import parse_repeater_frame, parse_skylink_frame  # noqa: E402
from comms.foresail.tx import (  # noqa: E402
    AUDIO_RATE,
    build_uplink,
    gaussian_nrz_audio,
)

FS = 250_000


def _iq(audio: np.ndarray, snr_db: float, offset_hz: float = -1000.0, seed: int = 1) -> np.ndarray:
    """FM-modulate *audio* (+-1 = +-2400 Hz, h = 0.5) into I/Q with noise, 0.5 s of lead-in."""
    g = gcd(FS, AUDIO_RATE)
    up = signal.resample_poly(audio, FS // g, AUDIO_RATE // g)
    base = np.concatenate([np.zeros(FS // 2), up, np.zeros(FS // 2)])
    t = np.arange(len(base)) / FS
    carrier = np.exp(1j * (2 * np.pi * np.cumsum(base * 2400.0) / FS + 2 * np.pi * offset_hz * t))
    active = np.zeros(len(base))
    active[FS // 2 : FS // 2 + len(up)] = 1.0
    noise_power = 10 ** (-snr_db / 10) * (FS / 2) / 14_000.0  # signal power 1, in 14 kHz
    rng = np.random.default_rng(seed)
    noise = (rng.standard_normal(len(base)) + 1j * rng.standard_normal(len(base))) * np.sqrt(
        noise_power / 2
    )
    return (carrier * active + noise).astype(np.complex64)


def test_gaussian_audio_is_bounded_and_settles_to_the_nrz_levels() -> None:
    audio = gaussian_nrz_audio(np.array([1] * 20 + [0] * 20, dtype=np.uint8))
    assert np.max(np.abs(audio)) <= 1.0 + 1e-6
    assert audio[8 * 5] == pytest.approx(1.0, abs=1e-3)
    assert audio[-8 * 5] == pytest.approx(-1.0, abs=1e-3)
    assert len(audio) == 40 * 5


@pytest.mark.parametrize("snr_db", [30.0, 12.0])
def test_receiver_decodes_the_uplink_waveform(snr_db: float) -> None:
    skylink, audio = build_uplink(
        "APRS", "JF9SOM", b">Hello via Foresail-1p", ("WIDE1-1",), sequence=5
    )
    frames = decode_iq(_iq(audio, snr_db), FS)
    assert [f.payload for f in frames] == [skylink]
    parsed = parse_skylink_frame(frames[0].payload)
    assert parsed is not None and parsed.vc == 3 and parsed.identity == "PyGS"
    repeater = parse_repeater_frame(parsed.payload)
    assert repeater is not None
    assert (str(repeater.source), repeater.via, repeater.text) == (
        "JF9SOM",
        "WIDE1-1",
        ">Hello via Foresail-1p",
    )
    assert repeater.fcs_status == "ok"


def test_receiver_reports_frame_times_in_a_stream_and_does_not_repeat_them() -> None:
    skylink, audio = build_uplink("CQ", "N0CALL", b"test")
    iq = _iq(audio, 25.0)
    stream = ForesailRxStream(FS)
    found = []
    for start in range(0, len(iq), FS // 10):  # 0.1 s blocks, as an SDR delivers them
        found.extend(stream.feed(iq[start : start + FS // 10]))
    found.extend(stream.flush())
    assert [f.payload for f in found] == [skylink]
    # The frame starts after the 0.5 s of silence plus the preamble (16 bytes = 13 ms).
    assert 0.5 < found[0].start_s < 0.6
    assert found[0].end_s > found[0].start_s


def test_noise_alone_decodes_nothing() -> None:
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(FS * 2) + 1j * rng.standard_normal(FS * 2)).astype(np.complex64)
    assert decode_iq(noise, FS) == []


def test_receiver_decodes_the_uplink_from_rig_audio() -> None:
    skylink, audio = build_uplink("CQ", "N0CALL", b"audio path")
    rng = np.random.default_rng(2)
    sound = np.concatenate([np.zeros(24_000), audio, np.zeros(24_000)])
    sound = (sound + rng.standard_normal(len(sound)) * 0.15).astype(np.float32)
    frames = decode_iq(sound, AUDIO_RATE, audio=True)
    assert [f.payload for f in frames] == [skylink]
    # The same audio with the polarity flipped (a rig that inverts its data output).
    inverted = decode_iq(-sound, AUDIO_RATE, audio=True)
    assert [f.payload for f in inverted] == [skylink]


def test_silent_audio_decodes_nothing() -> None:
    assert decode_iq(np.zeros(AUDIO_RATE * 2, dtype=np.float32), AUDIO_RATE, audio=True) == []
