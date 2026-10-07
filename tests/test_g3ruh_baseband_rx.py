"""Brute-force G3RUH baseband receiver: streaming, polarity, dedupe, no false frames.

Everything is synthetic (frames built by comms.aprs.g3ruh_tx, noise from a seeded
generator) and numpy-only, so it also runs in CI without scipy or Direwolf.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.aprs.g3ruh_baseband_rx import G3ruhBasebandDecoder
from comms.aprs.g3ruh_tx import build_g3ruh_audio

_RATE = 48_000


def _address(call: str, last: bool = False) -> bytes:
    return bytes((ord(c) << 1) for c in call.ljust(6)) + bytes([0x60 | (1 if last else 0)])


def _frame(text: bytes = b"saved 'AAA' at box: 1") -> bytes:
    return _address("JI1IZR") + _address("JS1YSD", True) + b"\x03\xf0" + text


def _stream(
    frames: list[tuple[float, bytes]],
    seconds: float,
    noise: float,
    seed: int = 1,
    invert: bool = False,
    level: float = 0.5,
) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    out = (rng.standard_normal(int(seconds * _RATE)) * noise).astype(np.float32)
    for t, frame in frames:
        audio = build_g3ruh_audio(frame, txdelay_ms=40, txtail_ms=10) * level
        i = int(t * _RATE)
        out[i : i + len(audio)] += -audio if invert else audio
    return out


def _run(decoder: G3ruhBasebandDecoder, audio: NDArray[np.float32]) -> list[bytes]:
    found: list[bytes] = []
    for i in range(0, len(audio), 2048):
        decoder.push_samples(audio[i : i + 2048])
        found.extend(decoder.decode_pending(now=i / _RATE))
    return found


def test_decodes_frames_in_noise() -> None:
    frames = [(2.0, _frame(b"one")), (9.0, _frame(b"two two")), (15.0, _frame(b"3"))]
    audio = _stream(frames, 20.0, noise=0.15)
    assert _run(G3ruhBasebandDecoder(), audio) == [f for _, f in frames]


def test_inverted_polarity_is_decoded_too() -> None:
    frame = _frame()
    audio = _stream([(2.0, frame)], 8.0, noise=0.1, invert=True)
    assert _run(G3ruhBasebandDecoder(), audio) == [frame]


def test_each_frame_is_reported_once_despite_overlapping_windows() -> None:
    frame = _frame()
    audio = _stream([(2.0, frame)], 10.0, noise=0.05)
    assert _run(G3ruhBasebandDecoder(window_s=4.0, stride_s=0.5), audio) == [frame]


def test_the_same_frame_again_after_the_dedupe_time_is_reported_again() -> None:
    frame = _frame()
    audio = _stream([(2.0, frame), (30.0, frame)], 36.0, noise=0.05)
    assert _run(G3ruhBasebandDecoder(dedupe_s=10.0), audio) == [frame, frame]


def test_pure_noise_gives_no_frames() -> None:
    audio = _stream([], 60.0, noise=0.3, seed=7)
    assert _run(G3ruhBasebandDecoder(), audio) == []


def test_a_frame_is_not_accepted_on_one_hit_alone_when_the_bar_is_raised() -> None:
    frame = _frame()
    audio = _stream([(2.0, frame)], 8.0, noise=0.05)
    # An impossible hit count: nothing passes, so the bar really is applied.
    assert _run(G3ruhBasebandDecoder(min_hits=10_000), audio) == []


def test_weak_frames_must_pass_the_validator_but_strong_ones_do_not() -> None:
    frame = _frame()
    audio = _stream([(2.0, frame)], 8.0, noise=0.05)
    # Everything counts as weak: the validator decides.
    assert _run(G3ruhBasebandDecoder(strong_hits=10_000, validator=lambda f: False), audio) == []
    assert _run(G3ruhBasebandDecoder(strong_hits=10_000, validator=lambda f: True), audio) == [
        frame
    ]
    # A frame with enough hits is accepted without asking the validator.
    assert _run(G3ruhBasebandDecoder(strong_hits=1, validator=lambda f: False), audio) == [frame]


def test_9600_baud_is_supported() -> None:
    frame = _frame(b"nine six")
    rng = np.random.default_rng(3)
    audio = (rng.standard_normal(8 * _RATE) * 0.1).astype(np.float32)
    burst = build_g3ruh_audio(frame, baud=9600, txdelay_ms=40, txtail_ms=10) * 0.5
    audio[2 * _RATE : 2 * _RATE + len(burst)] += burst
    assert _run(G3ruhBasebandDecoder(baud=9600), audio) == [frame]


def test_a_baud_that_does_not_divide_48k_is_rejected() -> None:
    with pytest.raises(ValueError):
        G3ruhBasebandDecoder(baud=7000)


def test_reset_forgets_reported_frames() -> None:
    frame = _frame()
    audio = _stream([(2.0, frame)], 6.0, noise=0.05)
    dec = G3ruhBasebandDecoder()
    assert _run(dec, audio) == [frame]
    dec.reset()
    assert _run(dec, audio) == [frame]
