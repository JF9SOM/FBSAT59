"""Tests for the word spaces added to beacons keyed with short word gaps (cw/word_space.py)."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.cw.word_space import _two_groups, insert_word_spaces

HOP_S = 48 / 3200  # the model's frame step

_MORSE = {
    "H": "....",
    "I": "..",
    "D": "-..",
    "E": ".",
    "P": ".--.",
    "0": "-----",
    "Q": "--.-",
    "A": ".-",
}


def _keyed(
    text: str,
    dot: int = 6,
    letter_gap: float = 3.0,
    word_gap: float = 3.7,
    where: float = 1.0,
    seed: int = 0,
) -> tuple[list[tuple[str, float]], NDArray[np.float32]]:
    """Per-frame tone energy of *text* keyed with the given gaps (in dots) and the
    (character, seconds) pairs a CTC decoder would give.

    *where* places each character's time within its own keyed part (1.0 = its end).
    """
    rng = np.random.default_rng(seed)
    frames: list[float] = [0.0] * 40
    offsets: list[tuple[str, float]] = []
    for ci, ch in enumerate(text):
        if ch == " ":
            continue
        start = len(frames)
        for ei, el in enumerate(_MORSE[ch]):
            frames += [1.0] * (dot * (3 if el == "-" else 1))
            if ei < len(_MORSE[ch]) - 1:
                frames += [0.0] * dot
        end = len(frames)
        offsets.append((ch, (start + where * (end - start)) * HOP_S))
        last = ci == len(text) - 1
        if not last:
            gap = word_gap if text[ci + 1] == " " else letter_gap
            frames += [0.0] * int(round(gap * dot))
    frames += [0.0] * 40
    energy = np.array(frames) + rng.uniform(0.02, 0.06, len(frames))
    return offsets, energy.astype(np.float32)


def _text(offsets: list[tuple[str, float]]) -> str:
    return "".join(ch for ch, _t in offsets)


def _plain(text: str) -> str:
    return text.replace(" ", "")


@pytest.mark.parametrize("where", [1.0, 0.5])
def test_short_word_gaps_get_spaces(where: float) -> None:
    text = "HI HI DE DP0PQA"
    offsets, energy = _keyed(text, where=where)
    # What the model gives: no spaces, because no gap is long.
    assert _text(offsets) == _plain(text)
    out = insert_word_spaces(offsets, energy, HOP_S)
    assert _text(out) == text


def test_standard_word_gaps_are_left_to_the_model() -> None:
    offsets, energy = _keyed("HI HI DE DP0PQA", word_gap=7.0)
    assert insert_word_spaces(offsets, energy, HOP_S) == offsets


def test_equal_gaps_get_no_spaces() -> None:
    offsets, energy = _keyed("HI HI DE DP0PQA", word_gap=3.0)
    assert insert_word_spaces(offsets, energy, HOP_S) == offsets


def test_a_gap_only_slightly_longer_is_not_a_word_gap() -> None:
    # 3.0 vs 3.15 dots is timing jitter, not two groups.
    offsets, energy = _keyed("HI HI DE DP0PQA", word_gap=3.15)
    assert insert_word_spaces(offsets, energy, HOP_S) == offsets


def test_no_keying_changes_nothing() -> None:
    rng = np.random.default_rng(5)
    energy = rng.uniform(0.02, 0.06, 600).astype(np.float32)
    offsets = [(c, i * 0.4) for i, c in enumerate("HIHIDEDP0PQA")]
    assert insert_word_spaces(offsets, energy, HOP_S) == offsets


def test_fast_keying_is_not_split() -> None:
    # A dot of 3 frames cannot separate 3.0 from 3.7 dots reliably.
    offsets, energy = _keyed("HI HI DE DP0PQA", dot=3)
    assert insert_word_spaces(offsets, energy, HOP_S) == offsets


def test_text_the_model_already_spaced_is_kept() -> None:
    offsets, energy = _keyed("HI HI DE DP0PQA")
    spaced = [*offsets[:2], (" ", offsets[1][1] + 0.01), *offsets[2:]]
    assert insert_word_spaces(spaced, energy, HOP_S) == spaced


def test_too_little_text_changes_nothing() -> None:
    offsets, energy = _keyed("HI DE")
    assert insert_word_spaces(offsets, energy, HOP_S) == offsets


def test_two_groups_on_gaps_measured_from_a_real_beacon() -> None:
    """Gaps (in dots) taken from an ERMINAZ-2U beacon: letters 3.0-3.4, word gaps 3.6-4.2."""
    gaps = [4.0, 3.0, 3.8, 3.2, 3.6, 3.0, 3.8, 3.2, 3.2, 3.2, 3.2, 3.4, 3.4, 4.2]
    found = _two_groups(np.array(gaps))
    assert found is not None
    low, high, split = found
    assert 3.1 < low < 3.3 and 3.7 < high < 4.0 and 3.4 < split < 3.6


def test_two_groups_rejects_scatter_around_one_value() -> None:
    gaps = [3.0, 3.2, 3.4, 3.0, 3.2, 3.2, 3.4, 3.0, 3.2, 3.4, 3.2, 3.0]
    assert _two_groups(np.array(gaps)) is None
