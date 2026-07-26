"""Incremental transcript stitching for the CW Decoder tab.

The CW model re-decodes a whole rolling audio window on every cycle rather
than emitting incremental output, so consecutive decode results overlap
heavily. Characters near the trailing edge of the window have had little or
no right-context yet and are the ones most likely to be revised once more
audio follows them (e.g. "FR0T" maturing into "FROM LASARSAT" once trailing
context confirms the reading). reconcile_pending() holds back that trailing
region as "pending" (replaceable) and only promotes text to "confirmed"
(permanent) once it is far enough from the window's trailing edge that the
model has already had a chance to revise it.

Kept free of Qt/UI/ONNX dependencies so the merge logic is unit-testable in
isolation.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

# A candidate gap is only trusted as real silence if at most this fraction
# of the (edge-trimmed) frames spanning it show energy above the window's
# own adaptive "active" threshold — see _looks_like_real_silence().
_MAX_ACTIVE_FRAME_RATIO = 0.2
# Frames within this many seconds of either edge of a candidate gap are
# excluded from the check, since a neighbouring character's tone
# attack/decay can bleed slightly across the CTC label boundary.
_ENERGY_EDGE_GUARD_S = 0.03


def reconcile_pending(
    offsets: Sequence[tuple[str, float]],
    window_duration: float,
    window_start_abs: float,
    confirmed_up_to_abs: float,
    pending_margin_s: float,
) -> tuple[str, str, float]:
    """Split a fresh decode into a confirmed delta and a tentative tail.

    Args:
        offsets: (char, seconds_from_window_start) pairs from this decode.
        window_duration: length (seconds) of the audio window just decoded.
        window_start_abs: absolute time (seconds, arbitrary epoch) at which
            the current audio window begins — i.e. how much audio has been
            permanently dropped from the rolling buffer so far.
        confirmed_up_to_abs: absolute time up to which text has already
            been committed as confirmed in a previous call.
        pending_margin_s: characters within this many seconds of the
            window's trailing edge are held back as still-revisable.

    Returns:
        (confirmed_delta, new_pending_text, new_confirmed_up_to_abs).
        confirmed_delta is the newly-matured text to append permanently
        (empty if nothing new has matured yet). new_pending_text is the
        current best-guess tail, meant to replace whatever tail was shown
        previously (it may shrink, grow, or be corrected outright).
    """
    cutoff_rel = window_duration - pending_margin_s
    if cutoff_rel <= 0:
        # No part of this window has enough trailing context yet.
        pending = "".join(ch for ch, _t in offsets)
        return "", pending, confirmed_up_to_abs

    cutoff_abs = window_start_abs + cutoff_rel
    delta = ""
    if cutoff_abs > confirmed_up_to_abs:
        already_rel = confirmed_up_to_abs - window_start_abs
        delta = "".join(ch for ch, t in offsets if already_rel < t <= cutoff_rel)
        confirmed_up_to_abs = cutoff_abs

    pending = "".join(ch for ch, t in offsets if t > cutoff_rel)
    return delta, pending, confirmed_up_to_abs


def _looks_like_real_silence(
    frame_energy: NDArray[np.float32],
    frame_duration_s: float,
    start_rel: float,
    end_rel: float,
) -> bool:
    """True if the audio frames spanning [start_rel, end_rel) (window-
    relative seconds) show essentially no tone energy.

    CTC label timing is a frame index at which the model's argmax *first*
    differs — a real, model-independent point in the audio — but greedy
    CTC can still assign two adjacent characters' labels many frames
    apart even when the true acoustic content between them is a normal,
    short inter-character gap (the model may simply take longer to
    "commit" to one label than another). frame_energy comes straight from
    the FFT magnitude, with no such per-character timing quirk, so
    checking it directly over the candidate gap tells whether the model's
    apparent pause reflects real silence or is just recognition timing
    noise (in which case the candidate gap is rejected and the characters
    are shown with no separator, as they would without this check).

    Uses an adaptive threshold derived from this same window's own energy
    distribution (25th percentile as the noise floor, 30% of the way to
    the window's peak as "clearly active") rather than a fixed absolute
    level, since input levels vary a lot between setups/signals.
    """
    lo = start_rel + _ENERGY_EDGE_GUARD_S
    hi = end_rel - _ENERGY_EDGE_GUARD_S
    if hi <= lo or frame_duration_s <= 0.0 or len(frame_energy) == 0:
        return True  # span too short (or no data) to meaningfully check

    i0 = max(0, int(round(lo / frame_duration_s)))
    i1 = min(len(frame_energy), int(round(hi / frame_duration_s)) + 1)
    if i1 <= i0:
        return True

    noise_floor = float(np.percentile(frame_energy, 25))
    peak = float(np.max(frame_energy))
    active_threshold = noise_floor + 0.3 * (peak - noise_floor)
    if active_threshold <= noise_floor:
        return True  # no meaningful dynamic range in this window at all

    span = frame_energy[i0:i1]
    active_fraction = float(np.mean(span > active_threshold))
    return active_fraction <= _MAX_ACTIVE_FRAME_RATIO


def insert_gap_markers(
    offsets: Sequence[tuple[str, float]],
    window_start_abs: float,
    last_char_abs: float | None,
    space_gap_s: float,
    newline_gap_s: float,
    frame_energy: NDArray[np.float32] | None = None,
    frame_duration_s: float = 0.0,
) -> tuple[list[tuple[str, float]], float | None]:
    """Insert synthetic ' '/'\\n' entries wherever a real silence gap is
    detected between consecutive decoded characters.

    The CTC model only emits a label for frames where it recognises
    something; a genuine multi-second (or multi-minute) pause between
    transmissions produces *no* characters at all — not even the model's
    own (much shorter) inter-word space — so naively concatenating decode
    output runs unrelated messages together with nothing in between (e.g.
    "...AR" immediately followed by "DE..."). Each character's absolute
    time (window start + its offset) lets that real silence be detected
    and marked explicitly.

    `last_char_abs` is the absolute time of the last real character seen
    across *previous* calls (None before anything has ever been decoded),
    so a gap spanning several otherwise-empty decode cycles is still
    caught the moment a new character finally appears.

    If `frame_energy`/`frame_duration_s` are supplied *and* the candidate
    gap's start lies within the current window (`prev_abs >=
    window_start_abs` — i.e. the audio spanning it is actually available
    to inspect), the gap is cross-checked against real audio energy via
    _looks_like_real_silence() before being accepted; a gap that fails
    this check (the model was just slow, not really silent) is dropped
    with no separator inserted at all. A gap whose start predates the
    current window has already survived one or more fully-empty decode
    cycles to get here (see cw_tab.py's silent-window handling) and is
    trusted without a frame_energy check, since there is no audio left in
    the buffer to inspect for it anyway.

    Returns (expanded_offsets, new_last_char_abs); the latter is
    `last_char_abs` unchanged when *offsets* is empty (nothing to update
    the anchor with).
    """
    if not offsets:
        return [], last_char_abs

    expanded: list[tuple[str, float]] = []
    prev_abs = last_char_abs
    for ch, t in offsets:
        abs_t = window_start_abs + t
        if prev_abs is not None:
            gap = abs_t - prev_abs
            if gap >= space_gap_s:
                verified = True
                if frame_energy is not None and prev_abs >= window_start_abs:
                    start_rel = prev_abs - window_start_abs
                    verified = _looks_like_real_silence(
                        frame_energy, frame_duration_s, start_rel, t
                    )
                if verified:
                    if gap >= newline_gap_s:
                        expanded.append(("\n", t))
                    elif ch != " ":
                        expanded.append((" ", t))
        expanded.append((ch, t))
        prev_abs = abs_t

    return expanded, prev_abs
