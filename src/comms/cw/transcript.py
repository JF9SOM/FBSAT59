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
