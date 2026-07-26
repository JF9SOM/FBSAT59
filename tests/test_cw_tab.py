"""Qt-widget regression test for CwTab's gap-marker anchor stability.

Kept separate from test_cw_decoder.py (which is intentionally Qt/ONNX-free)
since this specifically exercises CwTab._reconcile_decode() end-to-end,
including its confirmed/pending state — qtbot + qtbot.addWidget() per this
project's QWidget testing convention.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

from comms.cw.codec import DecodeResult
from ui.cw_tab import CwTab

_EMPTY_ENERGY = np.zeros(0, dtype=np.float32)


def _make_tab(qtbot: Any) -> CwTab:
    conn = sqlite3.connect(":memory:")
    tab = CwTab(conn)
    qtbot.addWidget(tab)
    return tab


class TestGapMarkerAnchorStability:
    """self._last_char_abs_time (the gap-detection anchor) must only ever
    advance to a character that has just been permanently *confirmed*,
    never to one still sitting in the tentative pending tail.

    Regression coverage for a real bug: greedy CTC's per-character timing
    is not perfectly reproducible across independent decode passes of
    overlapping windows, so if the anchor were allowed to track
    still-pending content, a later cycle's fresh (and slightly different)
    timing for that same audio could make an already-shown gap marker
    silently vanish — observed live as two unrelated transmissions
    running together ("...SK" directly followed by "CQ...") with no
    separator at all, even though a newline had correctly appeared for
    that same gap the cycle before.
    """

    def test_anchor_is_untouched_when_nothing_newly_confirms(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)
        tab._last_char_abs_time = 3.0  # pretend something confirmed earlier

        # 20s window, 5s pending margin -> cutoff_rel=15. Both characters
        # are past it (t > 15), so nothing should newly confirm this cycle.
        # "A"/"B" (not "S"/"K") deliberately avoids apply_prosign_conventions'
        # unrelated SK -> VA rule, which is not what this test is about.
        result = DecodeResult(
            offsets=[("A", 16.0), ("B", 16.5)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert tab._confirmed_text == ""
        assert "AB" in tab._pending_text
        assert tab._last_char_abs_time == 3.0  # untouched — not 16.5

    def test_anchor_advances_to_the_last_confirmed_character_only(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)

        # "DE " confirms (t <= 15); "AB" stays pending (t > 15). "A"/"B"
        # (not "S"/"K") deliberately avoids the unrelated SK -> VA rule.
        result = DecodeResult(
            offsets=[("D", 8.0), ("E", 8.3), (" ", 8.4), ("A", 16.0), ("B", 16.5)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert "DE" in tab._confirmed_text
        assert "AB" in tab._pending_text
        assert tab._last_char_abs_time == 8.4  # last *confirmed* char, not 16.5

    def test_fully_silent_window_leaves_anchor_untouched(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)
        tab._last_char_abs_time = 12.0
        tab._pending_text = "SK"

        # No characters decoded at all (offsets empty) -> the pending tail
        # is folded into confirmed, but the anchor itself must not move.
        result = DecodeResult(offsets=[], window_duration=20.0, frame_energy=_EMPTY_ENERGY)
        tab._reconcile_decode(result)

        assert tab._confirmed_text == "SK"
        assert tab._pending_text == ""
        assert tab._last_char_abs_time == 12.0


class TestDeferredTrailingS:
    """Regression coverage for the "SK" -> "VA" prosign display convention
    when "S" and "K" mature into confirmed_text in *separate* decode
    cycles (as opposed to landing in the same delta, already covered by
    test_cw_decoder.py's apply_prosign_conventions tests)."""

    def test_trailing_s_is_held_back_instead_of_confirmed_bare(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)

        # "73 S" all sit within the confirmable region (t <= 15 for a 20s
        # window), but the trailing standalone "S" must be deferred.
        result = DecodeResult(
            offsets=[("7", 8.0), ("3", 8.3), (" ", 8.5), ("S", 9.0)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert tab._confirmed_text == "73 "
        assert tab._pending_text == "S"

    def test_deferred_s_resolves_to_va_once_k_arrives_next_cycle(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)
        tab._reconcile_decode(
            DecodeResult(
                offsets=[("7", 8.0), ("3", 8.3), (" ", 8.5), ("S", 9.0)],
                window_duration=20.0,
                frame_energy=_EMPTY_ENERGY,
            )
        )
        tab._samples_dropped_total = int(5.0 * tab._rx_sample_rate)  # window advances 5s

        # Fresh decode of the (now shifted) window re-recognises "S" and
        # finally "K", both within the confirmable region.
        tab._reconcile_decode(
            DecodeResult(
                offsets=[("S", 3.6), ("K", 4.1)],
                window_duration=20.0,
                frame_energy=_EMPTY_ENERGY,
            )
        )

        assert tab._confirmed_text == "73 VA"
        assert "S" not in tab._pending_text
