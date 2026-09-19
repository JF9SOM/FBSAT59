"""Telemetry tab: the "minimum SNR needed to decode" info button and window."""

from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QToolButton, QWidget
from pytestqt.qtbot import QtBot

from ui.telemetry_tab import _SNR_GUIDE_ROWS, TelemetryTab, _snr_guide_html, _SnrGuideDialog


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        """CREATE TABLE transmitters (
            uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, description TEXT,
            mode TEXT, baud INTEGER, alive INTEGER
        )"""
    )
    return c


def test_guide_lists_every_speed_and_both_decoders() -> None:
    html = _snr_guide_html()
    for speed in ("1200", "4800", "9600"):
        assert speed in html
    assert "Direwolf" in html
    assert "gr-satellites" in html
    # every row's numbers appear in the text
    for _speed, dw, gr, band, tol in _SNR_GUIDE_ROWS:
        for cell in (dw, gr, band, tol):
            assert cell in html


def test_guide_explains_the_gr_satellites_preamble_caveat() -> None:
    assert "preamble" in _snr_guide_html()


def test_the_info_button_sits_at_the_right_end_of_the_input_source_row(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)

    button = tab._btn_snr_info
    assert isinstance(button, QToolButton)
    assert button.toolTip()
    # Same layout row as the Mode / Baud / Log controls, and the last item in it.
    row = tab._btn_backend_log.parentWidget().layout()
    assert row is not None
    found = None
    for i in range(row.count()):
        item = row.itemAt(i)
        sub = item.layout()
        if sub is not None and sub.indexOf(button) >= 0:
            found = sub
    assert found is not None
    assert found.indexOf(tab._btn_backend_log) >= 0
    assert found.itemAt(found.count() - 1).widget() is button


def test_clicking_the_button_opens_the_guide_window_once(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    assert tab._snr_guide_window is None

    tab._btn_snr_info.click()
    first = tab._snr_guide_window
    assert isinstance(first, _SnrGuideDialog)
    assert first.isVisible()
    assert "9600" in first._view.toPlainText()

    first.close()
    tab._btn_snr_info.click()
    assert tab._snr_guide_window is first  # reused, not rebuilt
    assert first.isVisible()
    first.close()
