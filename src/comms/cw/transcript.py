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


def insert_gap_markers(
    offsets: Sequence[tuple[str, float]],
    window_start_abs: float,
    last_char_abs: float | None,
    space_gap_s: float,
    newline_gap_s: float,
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
            if gap >= newline_gap_s:
                expanded.append(("\n", t))
            elif gap >= space_gap_s and ch != " ":
                expanded.append((" ", t))
        expanded.append((ch, t))
        prev_abs = abs_t

    return expanded, prev_abs
