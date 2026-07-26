"""Unit tests for the CW Decoder's CTC offset decoding and incremental
transcript stitching.

_ctc_decode_with_offsets() (comms/cw/codec.py) and reconcile_pending()
(comms/cw/transcript.py) are both pure functions with no ONNX/Qt
dependency, so they are tested directly against synthetic inputs — no
model file or QApplication required.
"""

from __future__ import annotations

import numpy as np

from comms.cw.codec import HOP_LENGTH, SAMPLE_RATE, _ctc_decode, _ctc_decode_with_offsets
from comms.cw.transcript import insert_gap_markers, reconcile_pending

# ---------------------------------------------------------------------------
# _ctc_decode_with_offsets() / _ctc_decode()
# ---------------------------------------------------------------------------

# Vocab index aliases matching codec._VOCAB = ",./0123456789?ABCDEFGHIJKLMNOPQRSTUVWXYZ "
_BLANK = 41
_SPACE = 40  # last vocab entry
_A = 14  # ",./0123456789?" is 14 chars (indices 0-13), so 'A' is index 14


def _log_probs_from_path(path: list[int], num_classes: int = 42) -> np.ndarray:
    """Build a (1, T, num_classes) one-hot log_probs array whose argmax
    equals *path* at every frame."""
    t = len(path)
    probs = np.full((1, t, num_classes), -10.0, dtype=np.float32)
    for frame, idx in enumerate(path):
        probs[0, frame, idx] = 10.0
    return probs


def test_repeated_label_collapses_without_blank() -> None:
    # 'A' three frames in a row with no blank in between collapses to one 'A'.
    log_probs = _log_probs_from_path([_A, _A, _A])
    assert _ctc_decode(log_probs) == "A"


def test_blank_resets_previous_label() -> None:
    # 'A', blank, 'A' again → blank resets repeat-suppression → two 'A's.
    log_probs = _log_probs_from_path([_A, _BLANK, _A])
    assert _ctc_decode(log_probs) == "AA"


def test_offsets_track_frame_index_in_seconds() -> None:
    # 'A' at frame 0, blank, 'A' again at frame 4.
    log_probs = _log_probs_from_path([_A, _BLANK, _BLANK, _BLANK, _A])
    offsets = _ctc_decode_with_offsets(log_probs)
    assert [ch for ch, _t in offsets] == ["A", "A"]
    assert offsets[0][1] == 0.0
    assert offsets[1][1] == 4 * HOP_LENGTH / SAMPLE_RATE


def test_double_space_from_blank_separated_space_tokens() -> None:
    # Two separate space recognitions split by a blank are NOT collapsed
    # (this is the source of the double-space cleanup done by the caller).
    log_probs = _log_probs_from_path([_A, _SPACE, _BLANK, _SPACE, _A])
    assert _ctc_decode(log_probs) == "A  A"


# ---------------------------------------------------------------------------
# reconcile_pending()
# ---------------------------------------------------------------------------

_MARGIN = 5.0


def test_short_window_below_margin_confirms_nothing() -> None:
    offsets = [("A", 0.0), ("B", 3.0)]
    delta, pending, confirmed_up_to = reconcile_pending(
        offsets,
        window_duration=4.0,
        window_start_abs=0.0,
        confirmed_up_to_abs=0.0,
        pending_margin_s=_MARGIN,
    )
    assert delta == ""
    assert pending == "AB"
    assert confirmed_up_to == 0.0


def test_confirms_text_older_than_margin_from_trailing_edge() -> None:
    # 20 s window, margin 5 s → cutoff at t=15s. Chars before/at 15s confirm,
    # the rest stays pending.
    offsets = [("D", 1.0), ("E", 2.0), (" ", 3.0), ("O", 14.0), ("K", 18.0)]
    delta, pending, confirmed_up_to = reconcile_pending(
        offsets,
        window_duration=20.0,
        window_start_abs=0.0,
        confirmed_up_to_abs=0.0,
        pending_margin_s=_MARGIN,
    )
    assert delta == "DE O"
    assert pending == "K"
    assert confirmed_up_to == 15.0


def test_already_confirmed_region_is_not_reconfirmed() -> None:
    """Second decode cycle, window has slid forward 5s (buffer dropped the
    oldest 5s of audio): only the newly-matured slice beyond the previous
    high-water mark becomes the new delta, expressed in the *new* window's
    own relative time base."""
    # Cycle 1 established confirmed_up_to_abs = 15.0 (see test above).
    # Cycle 2: window_start_abs advances to 5.0 (steady 20s window).
    # cutoff_rel is still 15.0 (20 - 5), so cutoff_abs = 5.0 + 15.0 = 20.0.
    # already_rel = confirmed_up_to_abs(15.0) - window_start_abs(5.0) = 10.0,
    # i.e. only characters with t_rel in (10.0, 15.0] are newly confirmable.
    offsets = [("K", 10.5), ("A", 14.0), ("B", 19.0)]
    delta, pending, confirmed_up_to = reconcile_pending(
        offsets,
        window_duration=20.0,
        window_start_abs=5.0,
        confirmed_up_to_abs=15.0,
        pending_margin_s=_MARGIN,
    )
    assert delta == "KA"
    assert pending == "B"
    assert confirmed_up_to == 20.0


def test_correction_near_trailing_edge_never_gets_confirmed() -> None:
    """Regression test for the "FR0T" -> "FROM LASARSAT" scenario: a
    misreading near the tail of one decode must still be revisable in the
    next decode — i.e. it must never have been folded into `delta` while
    still within the pending margin, so the correction can simply replace
    the old pending text instead of appearing as a duplicate."""
    # Cycle 1: 20 s window. "F" and "R" are far enough from the trailing
    # edge to confirm; the misread "0T" sits within the last 5s and stays
    # pending.
    offsets_1 = [("F", 10.0), ("R", 11.0), ("0", 16.0), ("T", 18.0)]
    delta_1, pending_1, confirmed_up_to = reconcile_pending(
        offsets_1,
        window_duration=20.0,
        window_start_abs=0.0,
        confirmed_up_to_abs=0.0,
        pending_margin_s=_MARGIN,
    )
    assert delta_1 == "FR"
    assert pending_1 == "0T"
    assert confirmed_up_to == 15.0

    # Cycle 2 (window slides forward 5s): more trailing context arrives and
    # the model corrects its reading of that same audio region to
    # "OM LASARSAT" instead of "0T", continuing on from the already
    # confirmed "FR".
    offsets_2 = [
        ("O", 11.0),
        ("M", 12.0),
        (" ", 13.0),
        ("L", 14.0),
        ("A", 15.0),  # confirms: t_rel <= cutoff_rel(15)
        ("S", 16.0),
        ("A", 17.0),
        ("R", 18.0),
        ("S", 18.5),
        ("A", 18.8),
        ("T", 19.5),  # still pending: t_rel > 15
    ]
    delta_2, pending_2, confirmed_up_to_2 = reconcile_pending(
        offsets_2,
        window_duration=20.0,
        window_start_abs=5.0,
        confirmed_up_to_abs=confirmed_up_to,
        pending_margin_s=_MARGIN,
    )
    assert delta_2 == "OM LA"
    assert pending_2 == "SARSAT"
    assert confirmed_up_to_2 == 20.0

    # The wrong first reading ("0T") never made it into any delta, and the
    # combined confirmed text plus the still-open pending tail reads the
    # corrected message cleanly, with no duplication.
    combined_confirmed = delta_1 + delta_2
    assert "0" not in combined_confirmed
    assert combined_confirmed == "FROM LA"
    assert combined_confirmed + pending_2 == "FROM LASARSAT"


# ---------------------------------------------------------------------------
# insert_gap_markers()
# ---------------------------------------------------------------------------

_SPACE_GAP = 1.0
_NEWLINE_GAP = 3.0


def test_no_marker_before_the_very_first_character() -> None:
    expanded, last_abs = insert_gap_markers(
        [("A", 0.0)],
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == [("A", 0.0)]
    assert last_abs == 0.0


def test_empty_offsets_leaves_anchor_unchanged() -> None:
    expanded, last_abs = insert_gap_markers(
        [],
        window_start_abs=99.0,
        last_char_abs=42.0,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == []
    assert last_abs == 42.0


def test_short_gap_within_one_window_inserts_space() -> None:
    # A 1.5 s gap between two letters (unusually long for same-message
    # spacing, but short of a real transmission boundary) gets a space.
    expanded, last_abs = insert_gap_markers(
        [("A", 0.0), ("B", 1.5)],
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == [("A", 0.0), (" ", 1.5), ("B", 1.5)]
    assert last_abs == 1.5


def test_long_gap_within_one_window_inserts_newline() -> None:
    expanded, _last_abs = insert_gap_markers(
        [("A", 0.0), ("B", 5.0)],
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == [("A", 0.0), ("\n", 5.0), ("B", 5.0)]


def test_gap_spanning_several_silent_cycles_uses_carried_over_anchor() -> None:
    # last_char_abs=100.0 came from a decode many cycles ago; the window
    # has since moved on to abs time 150.0 with no characters in between.
    expanded, last_abs = insert_gap_markers(
        [("D", 0.0)],
        window_start_abs=150.0,
        last_char_abs=100.0,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == [("\n", 0.0), ("D", 0.0)]
    assert last_abs == 150.0


def test_model_own_space_within_gap_threshold_is_not_doubled() -> None:
    # The model already emitted an explicit space at a 1.2 s gap — don't
    # add a second one on top of it.
    expanded, _last_abs = insert_gap_markers(
        [("A", 0.0), (" ", 1.2)],
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == [("A", 0.0), (" ", 1.2)]


def test_normal_intra_word_timing_gets_no_extra_markers() -> None:
    # Realistic same-word letter spacing (well under 1 s) must not trigger
    # any synthetic separator.
    offsets = [("C", 0.0), ("Q", 0.08), (" ", 0.20), ("D", 0.35), ("E", 0.42)]
    expanded, _last_abs = insert_gap_markers(
        offsets,
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=_SPACE_GAP,
        newline_gap_s=_NEWLINE_GAP,
    )
    assert expanded == offsets


def test_full_pipeline_bridges_a_real_pause_between_messages_with_newline() -> None:
    """End-to-end (still Qt-free) regression test for the "...AR" directly
    followed by "DE..." symptom: a genuine multi-minute pause between two
    transmissions must render as a newline in the assembled transcript,
    not a bare concatenation."""
    # Cycle 1: message 1 ("AR") decodes and fully confirms (both letters
    # sit well before the pending margin in a 20s window; realistic
    # same-word letter spacing, well under the 1 s space threshold).
    offsets_1 = [("A", 1.0), ("R", 1.1)]
    window_start_1 = 0.0
    expanded_1, last_abs = insert_gap_markers(
        offsets_1, window_start_1, None, _SPACE_GAP, _NEWLINE_GAP
    )
    delta_1, pending_1, confirmed_up_to = reconcile_pending(
        expanded_1,
        window_duration=20.0,
        window_start_abs=window_start_1,
        confirmed_up_to_abs=0.0,
        pending_margin_s=_MARGIN,
    )
    assert delta_1 == "AR"
    assert pending_1 == ""

    # Many silent decode cycles pass in between (represented here simply
    # by advancing window_start_abs and last_char_abs staying put, exactly
    # as cw_tab.py's empty-offsets branch leaves it untouched).

    # Cycle N: a new, unrelated message ("DE") begins after a multi-minute
    # real pause.
    offsets_n = [("D", 0.0), ("E", 0.1)]
    window_start_n = 300.0
    expanded_n, _last_abs = insert_gap_markers(
        offsets_n, window_start_n, last_abs, _SPACE_GAP, _NEWLINE_GAP
    )
    delta_n, pending_n, _confirmed_up_to = reconcile_pending(
        expanded_n,
        window_duration=20.0,
        window_start_abs=window_start_n,
        confirmed_up_to_abs=confirmed_up_to,
        pending_margin_s=_MARGIN,
    )

    combined = delta_1 + delta_n
    assert combined == "AR\nDE"
    assert pending_n == ""


# ---------------------------------------------------------------------------
# insert_gap_markers() with frame_energy verification
#
# Regression tests for the "JF9SOM" -> "JF 9SOM" false positive: greedy CTC
# can report two characters of the *same* word far enough apart in time
# (here modeled on a real ~1.2s gap observed between 'F' and '9' with no
# real silence between them) to cross the space threshold on timing alone.
# frame_energy lets insert_gap_markers tell a real gap from the model just
# being slow to commit to a label.
# ---------------------------------------------------------------------------

_FRAME_DURATION_S = 0.015  # matches HOP_LENGTH/SAMPLE_RATE = 48/3200


def _make_frame_energy(
    total_frames: int, active_ranges: list[tuple[int, int]], base: float = 0.05, active: float = 5.0
) -> np.ndarray:
    energy = np.full(total_frames, base, dtype=np.float32)
    for lo, hi in active_ranges:
        energy[lo:hi] = active
    return energy


def test_energy_check_suppresses_false_positive_from_slow_recognition() -> None:
    # 'F' reported at 7.755s, '9' at 8.955s (~1.2s apart) — matches the real
    # observed case — but the audio actually has tone energy continuously
    # present through that span (frames 560-580 out of the checked
    # 519-596 range), meaning the model was just slow, not really silent.
    frame_energy = _make_frame_energy(700, active_ranges=[(560, 580)])
    offsets = [("F", 7.755), ("9", 8.955)]
    expanded, _last_abs = insert_gap_markers(
        offsets,
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=1.0,
        newline_gap_s=3.0,
        frame_energy=frame_energy,
        frame_duration_s=_FRAME_DURATION_S,
    )
    assert expanded == offsets  # no synthetic space inserted


def test_energy_check_still_confirms_a_genuinely_silent_gap() -> None:
    # Same reported gap, but this time the audio is quiet throughout —
    # a real pause, so the space should still be inserted.
    frame_energy = _make_frame_energy(700, active_ranges=[])
    offsets = [("F", 7.755), ("9", 8.955)]
    expanded, _last_abs = insert_gap_markers(
        offsets,
        window_start_abs=0.0,
        last_char_abs=None,
        space_gap_s=1.0,
        newline_gap_s=3.0,
        frame_energy=frame_energy,
        frame_duration_s=_FRAME_DURATION_S,
    )
    assert expanded == [("F", 7.755), (" ", 8.955), ("9", 8.955)]


def test_energy_check_skipped_when_gap_predates_the_window() -> None:
    # last_char_abs is from a previous, already-evicted window (before
    # window_start_abs) — there is no audio left to inspect for it, so the
    # gap is trusted without a frame_energy check even if the (irrelevant)
    # energy data for *this* window looks fully "active" throughout.
    frame_energy = _make_frame_energy(700, active_ranges=[(0, 700)])
    offsets = [("D", 0.0)]
    expanded, _last_abs = insert_gap_markers(
        offsets,
        window_start_abs=300.0,
        last_char_abs=100.0,
        space_gap_s=1.0,
        newline_gap_s=3.0,
        frame_energy=frame_energy,
        frame_duration_s=_FRAME_DURATION_S,
    )
    assert expanded == [("\n", 0.0), ("D", 0.0)]


def test_no_frame_energy_supplied_keeps_prior_timing_only_behaviour() -> None:
    # Backwards compatibility: omitting frame_energy falls back to pure
    # timing-based detection (as used by the existing tests above).
    offsets = [("F", 7.755), ("9", 8.955)]
    expanded, _last_abs = insert_gap_markers(
        offsets, window_start_abs=0.0, last_char_abs=None, space_gap_s=1.0, newline_gap_s=3.0
    )
    assert expanded == [("F", 7.755), (" ", 8.955), ("9", 8.955)]
