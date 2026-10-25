"""Received Frames, ASCII View and Decoded Fields are linked: selection, bold newest frames."""

from __future__ import annotations

import datetime
import sqlite3

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from comms.telemetry.decoder import decode_telemetry
from ui.telemetry_tab import TelemetryTab, format_frame_hex

_KOSTKA = 69935
# KOSTKA beacons from the JJ1WTK log: two OBC frames and one MRG frame.
_OBC1 = b"255,0,11,8660379,9117082,1792507744,0,22.20,12.39"
_OBC2 = b"255,0,12,8660439,9117142,1792507804,0,22.40,12.50"
_MRG = b"U,9008906,9008995,1,0,1632,1537,1475,6,JA1HCB,8048,980794,98,120\x00"


class _FakeRadioControl(QWidget):
    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def tab(qtbot: QtBot) -> TelemetryTab:
    assert QApplication.instance() is not None
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        "CREATE TABLE transmitters (uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, "
        "description TEXT, mode TEXT, baud INTEGER, alive INTEGER)"
    )
    t = TelemetryTab(c, _FakeRadioControl())
    qtbot.addWidget(t)
    t._rebuild_decode_tabs(_KOSTKA)
    return t


def _receive(tab: TelemetryTab, payload: bytes) -> None:
    """A received frame as _on_ax25_frame() adds it: the row, then the decode tab."""
    tf = decode_telemetry("OK0KST", payload, _KOSTKA)
    tab._append_row(
        callsign="OK0KST",
        sat_name="KOSTKA",
        data=format_frame_hex(payload),
        norad=_KOSTKA,
        decoded=tf,
    )
    tab._update_decode_tab(tf)


def _bold(tab: TelemetryTab, row: int) -> tuple[bool, bool]:
    """Whether the Received Frames row and the ASCII View row are drawn bold."""
    return (
        tab._table.item(row, 3).font().bold(),  # type: ignore[union-attr]
        tab._ascii_table.item(row, 3).font().bold(),  # type: ignore[union-attr]
    )


def _selected(table: object) -> list[int]:
    return sorted({i.row() for i in table.selectedIndexes()})  # type: ignore[attr-defined]


def test_only_the_newest_frame_of_each_type_is_bold(tab: TelemetryTab) -> None:
    _receive(tab, _OBC1)
    assert _bold(tab, 0) == (True, True)
    _receive(tab, _OBC2)  # supersedes the first OBC frame
    _receive(tab, _MRG)  # a different type: the OBC frame stays bold
    assert [_bold(tab, r) for r in range(3)] == [(False, False), (True, True), (True, True)]


def test_a_row_without_a_decoded_frame_is_never_bold(tab: TelemetryTab) -> None:
    tab._append_row(callsign="X", sat_name="Y", data="[HK1] 0123", norad=None)
    assert _bold(tab, 0) == (False, False)


def test_selecting_a_frame_selects_its_ascii_row_and_highlights_its_fields(
    tab: TelemetryTab,
) -> None:
    _receive(tab, _OBC1)
    _receive(tab, _OBC2)
    tab._table.selectRow(0)  # the older OBC frame
    assert _selected(tab._ascii_table) == [0]
    obc = tab._decode_tables["OBC"]
    assert _selected(obc) == list(range(obc.rowCount()))  # every field highlighted
    assert tab._decode_id_tabs.currentWidget() is obc
    assert obc.item(2, 1).text() == "11 %"  # type: ignore[union-attr]  # the older frame's CPU load
    assert not tab._decode_banner.isHidden()
    assert "#1" in tab._decode_banner.text()


def test_the_newest_frame_shows_no_warning(tab: TelemetryTab) -> None:
    _receive(tab, _OBC1)
    _receive(tab, _OBC2)
    tab._table.selectRow(0)
    assert not tab._decode_banner.isHidden()
    tab._table.selectRow(1)
    assert tab._decode_banner.isHidden()
    assert tab._decode_tables["OBC"].item(2, 1).text() == "12 %"  # type: ignore[union-attr]


def test_selecting_an_ascii_row_selects_the_frame(tab: TelemetryTab) -> None:
    _receive(tab, _OBC1)
    _receive(tab, _MRG)
    tab._ascii_table.selectRow(1)
    assert _selected(tab._table) == [1]
    assert tab._decode_id_tabs.currentWidget() is tab._decode_tables["U"]


def test_selecting_a_field_selects_the_frame_it_came_from(tab: TelemetryTab) -> None:
    _receive(tab, _OBC1)
    _receive(tab, _MRG)
    _receive(tab, _OBC2)
    mrg = tab._decode_tables["U"]
    mrg.clearSelection()
    mrg.selectRow(2)  # a field of the MRG frame (row 1 of the frame table)
    assert _selected(tab._table) == [1]
    assert _selected(tab._ascii_table) == [1]
    obc = tab._decode_tables["OBC"]
    obc.selectRow(0)  # the OBC tab shows the newest OBC frame, row 2
    assert _selected(tab._table) == [2]


def test_a_newer_frame_updates_the_sub_tab_and_clears_the_warning(tab: TelemetryTab) -> None:
    _receive(tab, _OBC1)
    _receive(tab, _OBC2)
    tab._table.selectRow(0)
    assert not tab._decode_banner.isHidden()
    _receive(tab, _OBC1)  # a new OBC frame arrives while the old one is shown
    assert tab._decode_banner.isHidden()
    assert tab._shown_row["OBC"] == 2


def test_clear_forgets_the_links(tab: TelemetryTab) -> None:
    _receive(tab, _OBC1)
    tab._on_clear()
    assert not tab._row_type and not tab._latest_row and not tab._shown_row
    _receive(tab, _OBC2)
    assert _bold(tab, 0) == (True, True)


def test_the_selected_decode_tab_is_tinted_with_the_selection_colour(
    tab: TelemetryTab, qtbot: QtBot
) -> None:
    from PySide6.QtGui import QColor, QPalette

    from ui.telemetry_tab import _HighlightTabBar

    bar = tab._decode_id_tabs.tabBar()
    assert isinstance(bar, _HighlightTabBar)
    _receive(tab, _OBC1)
    tab._table.selectRow(0)
    tab._log_tabs.setCurrentWidget(tab._decode_page)
    tab.resize(900, 500)
    tab.show()
    qtbot.waitExposed(tab)
    image = bar.grab().toImage()
    rect = bar.tabRect(bar.currentIndex())
    other = bar.tabRect(0 if bar.currentIndex() != 0 else 1)
    highlight = bar.palette().color(QPalette.ColorRole.Highlight)

    def blue_share(r: object) -> float:
        # how close the middle-left pixel of a tab is to the selection colour (0..1)
        x, y = r.left() + 6, r.center().y()  # type: ignore[attr-defined]
        c = QColor(image.pixel(x, y))
        return 1 - (abs(c.red() - highlight.red()) + abs(c.blue() - highlight.blue())) / 510

    assert blue_share(rect) > blue_share(other)


def test_rows_are_measured_again_when_the_ascii_view_is_first_shown(
    tab: TelemetryTab, qtbot: QtBot
) -> None:
    """Rows measured while the page was hidden (narrow columns) were many lines tall."""
    tab.resize(1000, 500)
    tab.show()
    qtbot.waitExposed(tab)
    _receive(tab, _OBC1)
    before = tab._row_fits["ascii"]
    tab._log_tabs.setCurrentWidget(tab._ascii_page)
    qtbot.waitUntil(lambda: tab._row_fits["ascii"] > before, timeout=2000)


def test_rows_are_measured_again_the_moment_a_column_is_resized(
    tab: TelemetryTab, qtbot: QtBot
) -> None:
    """Before the next paint, so stale heights are never drawn."""
    tab.resize(1000, 500)
    tab.show()
    qtbot.waitExposed(tab)
    _receive(tab, _OBC1)
    tab._log_tabs.setCurrentWidget(tab._ascii_page)
    qtbot.wait(100)
    before = tab._row_fits["ascii"]
    tab._ascii_table.setColumnWidth(0, tab._ascii_table.columnWidth(0) + 40)
    assert tab._row_fits["ascii"] > before  # no waiting: it happened inside the resize


def test_a_long_table_is_measured_after_a_short_pause_instead(
    tab: TelemetryTab, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TelemetryTab, "_SYNC_FIT_ROWS", 2)  # "long" is more than two rows here
    tab.resize(1000, 500)
    tab.show()
    qtbot.waitExposed(tab)
    tab._log_tabs.setCurrentWidget(tab._ascii_page)
    for _ in range(4):
        tab._append_ascii_row(datetime.datetime.now(datetime.UTC), "X", "Y", "text", False)
    qtbot.wait(100)
    before = tab._row_fits["ascii"]
    tab._ascii_table.setColumnWidth(0, tab._ascii_table.columnWidth(0) + 40)
    assert tab._row_fits["ascii"] == before  # not inside the resize
    qtbot.waitUntil(lambda: tab._row_fits["ascii"] > before, timeout=2000)
