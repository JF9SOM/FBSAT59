"""The classical (timing) CW decoder, on synthetic keyed carriers."""

from __future__ import annotations

import numpy as np

from comms.cw.classic import _MORSE, decode_classic, find_tone

RATE = 48_000
_CODE = {v: k for k, v in _MORSE.items()}


def _keyed(
    text: str,
    unit_s: float = 0.058,
    tone_hz: float = 700.0,
    wander_hz: float = 0.0,
    snr_db: float = 25.0,
    seed: int = 1,
) -> np.ndarray:
    """A carrier keyed with *text* in Morse (lead-in silence included), plus noise."""
    rng = np.random.default_rng(seed)
    on = []
    for ch in text:
        if ch == " ":
            on += [(False, 7 * unit_s)]
            continue
        for i, el in enumerate(_CODE[ch]):
            on += [(True, unit_s if el == "." else 3 * unit_s)]
            on += [(False, unit_s if i < len(_CODE[ch]) - 1 else 3 * unit_s)]
    key = np.concatenate(
        [np.full(int(round(d * RATE)), 1.0 if s else 0.0) for s, d in [(False, 1.5)] + on]
    )
    key = np.concatenate([key, np.zeros(RATE * 3)])
    t = np.arange(len(key)) / RATE
    # the carrier wanders a few tens of Hz, as a real satellite's does
    freq = tone_hz + wander_hz * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    noise = rng.normal(0.0, 10 ** (-snr_db / 20) / 2, len(key))
    return (key * np.sin(phase) + noise).astype(np.float32)


def _text(audio: np.ndarray) -> str:
    return "".join(c for c, _t in decode_classic(audio, RATE).offsets).strip()


def test_decodes_a_clean_carrier() -> None:
    assert _text(_keyed("JS1YRU 81 7E 7B A4")) == "JS1YRU 81 7E 7B A4"


def test_decodes_a_wandering_carrier() -> None:
    audio = _keyed("ORIGAMI2 81 7E 7C 43", wander_hz=30.0)
    assert _text(audio) == "ORIGAMI2 81 7E 7C 43"


def test_other_speeds() -> None:
    assert _text(_keyed("CQ DE JS1YSD", unit_s=0.076)) == "CQ DE JS1YSD"
    assert _text(_keyed("0123456789", unit_s=0.045)) == "0123456789"


def test_noise_alone_gives_no_text() -> None:
    noise = np.random.default_rng(3).normal(0.0, 0.2, RATE * 12).astype(np.float32)
    assert decode_classic(noise, RATE).offsets == []
    assert find_tone(noise, RATE) is None


def test_short_audio_is_empty() -> None:
    result = decode_classic(np.zeros(RATE, dtype=np.float32), RATE)
    assert result.offsets == [] and result.window_duration == 0.0


def test_character_times_follow_the_audio() -> None:
    result = decode_classic(_keyed("E T E T"), RATE)
    times = [t for c, t in result.offsets if c != " "]
    assert len(times) == 4 and 1.5 < times[0] < times[1] < times[2] < times[3]
    assert times[3] < result.window_duration
