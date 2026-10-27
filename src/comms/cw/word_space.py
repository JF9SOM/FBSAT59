"""Word spaces for beacons that key their words only slightly apart.

DeepCW prints a space only after a long gap (the standard word gap is seven dots).
A beacon whose keyer leaves just ~3.7 dots between words (letters ~3.0), such as
ERMINAZ-2U's ``HI HI DE DP0PQA``, comes out as ``HIHIDEDP0PQA``. The key-up gaps
are measured from the per-frame tone energy of the same window and, when they fall
into two clearly separate groups, the longer group is marked as word gaps.

Nothing is changed unless the evidence is clear (see :func:`insert_word_spaces`), so
ordinary Morse, which the model already spaces, is left exactly as it was.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

# A frame is "on" above floor + this fraction of the floor-to-peak span.
_ON_FRACTION = 0.4
# The tone must stand out of the floor this much, or there is no keying to measure.
_MIN_KEYING_RATIO = 3.0
# Runs shorter than this many frames are keying chatter and are merged into their neighbours.
_MIN_RUN_FRAMES = 2
# A dot shorter than this many frames leaves too little resolution to tell 3.0 dots from 3.7.
_MIN_DOT_FRAMES = 4.0
# A gap this long (in dots) is a standard word gap: the model spaces those itself, so leave it.
_STANDARD_WORD_GAP = 5.0
# Gaps between letters/short words considered for the two-group split (in dots).
_GAP_MIN, _GAP_MAX = 2.2, _STANDARD_WORD_GAP
# The shorter group is the letter gap and must look like one (in dots).
_LETTER_GAP_RANGE = (2.4, 3.6)
# The group centres must differ by this ratio and by this many dots (a frame is ~0.2 dot, so
# anything closer is measurement scatter), by at least this many standard deviations of the
# gaps inside a group, and each group needs at least this many members.
_MIN_RATIO = 1.15
_MIN_CENTRE_DISTANCE = 0.45
_MIN_SEPARATION_SIGMAS = 2.5
_MIN_GROUP = 2
_MIN_GAPS = 6


def _runs(on: NDArray[np.bool_]) -> list[list[int]]:
    """Runs as [state, start, length] with chatter shorter than _MIN_RUN_FRAMES merged away."""
    runs: list[list[int]] = []
    start = 0
    for i in range(1, len(on) + 1):
        if i == len(on) or on[i] != on[start]:
            runs.append([int(on[start]), start, i - start])
            start = i
    changed = True
    while changed:
        changed = False
        for i in range(1, len(runs) - 1):
            if runs[i][2] < _MIN_RUN_FRAMES:
                runs[i - 1][2] += runs[i][2] + runs[i + 1][2]
                del runs[i : i + 2]
                changed = True
                break
    return runs


def _two_groups(values: NDArray[np.float64]) -> tuple[float, float, float] | None:
    """Split *values* into a low and a high group by two-means.

    Returns (low centre, high centre, split) or None when the values do not form two
    clearly separate groups. A split at the widest single gap is not used: measured gaps
    scatter by a frame or two, so that gap can fall anywhere inside one group.
    """
    if len(values) < _MIN_GAPS:
        return None
    ordered = np.sort(values)
    low_c, high_c = float(ordered[0]), float(ordered[-1])
    for _ in range(20):
        split = (low_c + high_c) / 2.0
        low, high = ordered[ordered < split], ordered[ordered >= split]
        if len(low) == 0 or len(high) == 0:
            return None
        new_low, new_high = float(low.mean()), float(high.mean())
        if new_low == low_c and new_high == high_c:
            break
        low_c, high_c = new_low, new_high
    split = (low_c + high_c) / 2.0
    low, high = ordered[ordered < split], ordered[ordered >= split]
    if len(low) < _MIN_GROUP or len(high) < _MIN_GROUP:
        return None
    if high_c - low_c < _MIN_CENTRE_DISTANCE or high_c / low_c < _MIN_RATIO:
        return None
    spread = float(np.sqrt((low.var() * len(low) + high.var() * len(high)) / len(ordered)))
    if high_c - low_c < _MIN_SEPARATION_SIGMAS * max(spread, 0.05):
        return None
    if not _LETTER_GAP_RANGE[0] <= low_c <= _LETTER_GAP_RANGE[1]:
        return None
    return low_c, high_c, split


def insert_word_spaces(
    offsets: list[tuple[str, float]],
    frame_energy: NDArray[np.float32],
    hop_s: float,
) -> list[tuple[str, float]]:
    """Return *offsets* with spaces added at the longer of two clearly separate letter gaps.

    *offsets* are (character, seconds) pairs and *frame_energy* the tone energy per frame
    (``hop_s`` seconds apart) of the same window. The list is returned unchanged when the
    keying cannot be measured reliably, when the window holds a standard word gap (the
    model spaces those itself) or when the gaps between letters do not form two groups.
    """
    if len(offsets) < _MIN_GAPS + 1 or len(frame_energy) < 20 or hop_s <= 0.0:
        return offsets
    energy = np.asarray(frame_energy, dtype=np.float64)
    floor, peak = float(np.percentile(energy, 10)), float(np.percentile(energy, 95))
    if peak < _MIN_KEYING_RATIO * max(floor, 1e-12):
        return offsets
    runs = _runs(energy > floor + _ON_FRACTION * (peak - floor))
    on_lengths = [r[2] for r in runs if r[0]]
    if len(on_lengths) < _MIN_GAPS:
        return offsets
    dot = float(np.percentile(on_lengths, 15))
    if dot < _MIN_DOT_FRAMES:
        return offsets
    off_runs = [(r[1], r[2]) for r in runs if not r[0]]
    # Gaps within the signal only: the first and last runs are the window edges, not keying.
    if runs and not runs[0][0]:
        off_runs = off_runs[1:]
    if runs and not runs[-1][0] and off_runs:
        off_runs = off_runs[:-1]
    if any(length / dot >= _STANDARD_WORD_GAP for _s, length in off_runs):
        return offsets
    if any(ch == " " for ch, _t in offsets):
        return offsets

    pair_gaps: list[tuple[int, float]] = []  # (index of the first character, gap in dots)
    for i in range(len(offsets) - 1):
        t1, t2 = offsets[i][1], offsets[i + 1][1]
        inside = [
            length / dot for start, length in off_runs if t1 <= (start + length / 2.0) * hop_s <= t2
        ]
        if inside:
            pair_gaps.append((i, max(inside)))
    values = np.array([g for _i, g in pair_gaps if _GAP_MIN <= g < _GAP_MAX], dtype=np.float64)
    split = _two_groups(values)
    if split is None:
        return offsets
    threshold = split[2]

    out: list[tuple[str, float]] = []
    wide = {i for i, g in pair_gaps if g >= threshold}
    for i, item in enumerate(offsets):
        out.append(item)
        if i in wide:
            out.append((" ", (offsets[i][1] + offsets[i + 1][1]) / 2.0))
    return out
