"""Clicking the FT4 waterfall picks the TX frequency (like WSJT-X's Wide Graph)."""

from __future__ import annotations

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from ui import ft4_waterfall_dialog as wf
from ui.ft4_waterfall_dialog import Ft4WaterfallDialog, freq_from_plot_x


def test_freq_from_plot_x_maps_the_plot_edges_and_clamps() -> None:
    assert freq_from_plot_x(0, 200.0, 3000.0) == pytest.approx(200.0)
    assert freq_from_plot_x(wf._PLOT_WIDTH, 200.0, 3000.0) == pytest.approx(3000.0)
    assert freq_from_plot_x(wf._PLOT_WIDTH / 2, 200.0, 3000.0) == pytest.approx(1600.0)
    assert freq_from_plot_x(-50, 200.0, 3000.0) == pytest.approx(200.0)
    assert freq_from_plot_x(10_000, 200.0, 3000.0) == pytest.approx(3000.0)


def test_click_before_any_audio_does_nothing(qtbot: QtBot) -> None:
    dlg = Ft4WaterfallDialog()
    qtbot.addWidget(dlg)
    got: list[float] = []
    dlg.tx_freq_requested.connect(got.append)
    dlg._on_plot_clicked(100, 50)
    assert got == []


def test_click_requests_the_frequency_under_the_pointer(qtbot: QtBot) -> None:
    dlg = Ft4WaterfallDialog()
    qtbot.addWidget(dlg)
    dlg.resize(wf._CANVAS_WIDTH + 100, wf._CANVAS_HEIGHT + 100)
    dlg.show()
    rng = np.random.default_rng(0)
    dlg.update_waterfall(rng.normal(0, 0.1, 12_000 * 7).astype(np.float32), [])
    pix = dlg._image_label.pixmap()
    x0 = (dlg._image_label.width() - pix.width()) // 2
    got: list[float] = []
    dlg.tx_freq_requested.connect(got.append)
    mid = wf._MARGIN_LEFT + wf._PLOT_WIDTH // 2
    dlg._on_plot_clicked(x0 + mid, 40)
    expected = round(freq_from_plot_x(wf._PLOT_WIDTH // 2, dlg._freq_lo, dlg._freq_hi))
    assert got == [float(expected)]
    dlg._on_plot_clicked(x0 + 2, 40)  # in the left margin: ignored
    assert len(got) == 1
