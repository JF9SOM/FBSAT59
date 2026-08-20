"""Unit tests for ui/meteor_waterfall.py — MeteorWaterfallWidget.

Uses the qtbot fixture (pytest-qt) rather than manual QApplication + close()
management — see CLAUDE.md's note on why: manual app/close() teardown has
caused real Qt object-lifetime segfaults for other widgets in this project.
"""

from __future__ import annotations

import numpy as np
from pytestqt.qtbot import QtBot

from ui.meteor_waterfall import MeteorWaterfallWidget


class TestMeteorWaterfallWidget:
    def test_initial_state_shows_placeholder_text(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        assert w._canvas.pixmap().isNull()
        assert "Waiting" in w._canvas.text()

    def test_add_frame_draws_a_pixmap_and_updates_status(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.resize(400, 300)
        w.add_frame([1.0, 5.0, 2.0, 8.0, 3.0])
        assert not w._canvas.pixmap().isNull()
        assert "Last update" in w._status.text()

    def test_add_frame_ignores_empty_input(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.add_frame([])
        assert len(w._rows) == 0
        assert w._canvas.pixmap().isNull()

    def test_history_is_bounded(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        for i in range(300):
            w.add_frame([float(i), float(i) + 1.0])
        assert len(w._rows) == w._rows.maxlen

    def test_reset_clears_history_and_status(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.add_frame([1.0, 2.0, 3.0])
        assert len(w._rows) == 1
        w.reset()
        assert len(w._rows) == 0
        assert w._canvas.pixmap().isNull()
        assert w._status.text() == ""

    def test_show_unavailable_before_any_data_replaces_canvas_text(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.show_unavailable("no api")
        assert w._canvas.text() == "no api"
        assert w._status.text() == "no api"

    def test_show_unavailable_after_data_keeps_existing_pixmap(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.resize(400, 300)
        w.add_frame([1.0, 2.0, 3.0])
        assert not w._canvas.pixmap().isNull()
        w.show_unavailable("connection lost")
        # Pixmap from the prior frame is preserved -- only the status text changes.
        assert not w._canvas.pixmap().isNull()
        assert w._status.text() == "connection lost"

    def test_resize_with_existing_data_redraws(self, qtbot: QtBot) -> None:
        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.add_frame([1.0, 2.0, 3.0])
        w.resize(500, 350)
        assert not w._canvas.pixmap().isNull()

    def test_add_frame_resamples_to_plot_width(self, qtbot: QtBot) -> None:
        from ui.meteor_waterfall import _PLOT_WIDTH

        w = MeteorWaterfallWidget()
        qtbot.addWidget(w)
        w.add_frame(np.linspace(0.0, 10.0, num=17).tolist())
        assert w._rows[-1].shape == (_PLOT_WIDTH,)
