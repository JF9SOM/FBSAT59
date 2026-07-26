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
        result = DecodeResult(
            offsets=[("S", 16.0), ("K", 16.5)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert tab._confirmed_text == ""
        assert "SK" in tab._pending_text
        assert tab._last_char_abs_time == 3.0  # untouched — not 16.5

    def test_anchor_advances_to_the_last_confirmed_character_only(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)

        # "DE " confirms (t <= 15); "SK" stays pending (t > 15).
        result = DecodeResult(
            offsets=[("D", 8.0), ("E", 8.3), (" ", 8.4), ("S", 16.0), ("K", 16.5)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert "DE" in tab._confirmed_text
        assert "SK" in tab._pending_text
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
