"""The Telemetry tab's "ASCII View" sub-tab and the hex -> ASCII conversion behind it."""

from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from comms.telemetry.ascii_view import (
    ax25_to_ascii,
    dotted_ascii,
    escaped_ascii,
    frame_ascii,
    hex_text_to_bytes,
    row_ascii,
)
from ui.telemetry_tab import TelemetryTab

# LASARsat frames as the Received Frames tab shows them (hex of the whole AX.25 frame).
_GREETING_HEX = (
    "86 A2 40 40 40 40 60 9E 96 60 98 A6 A4 61 03 F0 "
    "43 65 6C 65 62 72 61 74 69 6E 67 20 31 30 20 30 30 30 20 6F 72 62 69 74 73 21"
)
_DOS_HEX = "86 A2 40 40 40 40 60 9E 96 60 98 A6 A4 61 03 F0 44 4F 53 2C 38 30 2C 30 2C 30 00"


def _address(call: str, ssid: int = 0, last: bool = False, repeated: bool = False) -> bytes:
    field = bytes(ord(c) << 1 for c in call.ljust(6))
    flag = 0x60 | (ssid << 1) | (0x80 if repeated else 0) | (1 if last else 0)
    return field + bytes([flag])


def test_a_real_lasarsat_frame_reads_like_the_satellite_log() -> None:
    raw = hex_text_to_bytes(_GREETING_HEX)
    assert raw is not None
    assert ax25_to_ascii(raw) == "OK0LSR>CQ:Celebrating 10 000 orbits!"


def test_a_trailing_nul_is_shown_the_way_the_log_shows_it() -> None:
    raw = hex_text_to_bytes(_DOS_HEX)
    assert raw is not None
    assert ax25_to_ascii(raw) == "OK0LSR>CQ:DOS,80,0,0<0x00>"


def test_digipeaters_ssids_and_the_repeated_flag() -> None:
    frame = (
        _address("APRS")
        + _address("JF9SOM", 3)
        + _address("DP0SNX", repeated=True)
        + _address("WIDE2", 1, last=True)
        + b"\x03\xf0>hi"
    )
    assert ax25_to_ascii(frame) == "JF9SOM-3>APRS,DP0SNX*,WIDE2-1:>hi"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00" * 30,  # not callsign bytes
        _address("CQ") + _address("OK0LSR", last=True),  # no control/PID
        _address("CQ") + _address("OK0LSR", last=True) + b"\x13\xf0x",  # not a UI frame
        bytes.fromhex("040009") + bytes(72),  # a CSP frame
    ],
)
def test_frames_that_are_not_ax25_ui_frames_are_not_misread(raw: bytes) -> None:
    assert ax25_to_ascii(raw) is None


def test_other_frames_get_a_dotted_dump() -> None:
    raw = b"\x04\x00\x09HA2GRB\xff"
    assert frame_ascii(raw) == "...HA2GRB."
    assert dotted_ascii(b"A\x00B") == "A.B"
    assert escaped_ascii(b"A\x00\xffB") == "A<0x00><0xFF>B"


def test_hex_text_is_read_only_when_it_is_a_plain_hex_dump() -> None:
    assert hex_text_to_bytes("86 a2\n40") == b"\x86\xa2\x40"
    for text in ("", "[HK1] 0123", "86 A2 4", "hello"):
        assert hex_text_to_bytes(text) is None


def test_row_ascii_uses_the_kept_bytes_then_the_shown_hex_then_a_dash() -> None:
    raw = hex_text_to_bytes(_GREETING_HEX)
    assert row_ascii(raw, "ignored") == "OK0LSR>CQ:Celebrating 10 000 orbits!"
    assert row_ascii(None, _GREETING_HEX) == "OK0LSR>CQ:Celebrating 10 000 orbits!"
    assert row_ascii(None, "[HK1] 0123456789ABCDEF") == "—"
    assert row_ascii(None, "") == "—"


# -- the tab ---------------------------------------------------------------------------


class _FakeRadioControl(QWidget):
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
        "CREATE TABLE transmitters (uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, "
        "description TEXT, mode TEXT, baud INTEGER, alive INTEGER)"
    )
    return c


@pytest.fixture
def tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    assert QApplication.instance() is not None
    t = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(t)
    return t


def test_the_ascii_view_sits_between_received_frames_and_decoded_fields(tab: TelemetryTab) -> None:
    titles = [tab._log_tabs.tabText(i) for i in range(tab._log_tabs.count())]
    assert titles == ["Received Frames", "ASCII View", "Decoded Fields"]


def test_the_old_paste_tab_is_gone_from_the_telemetry_tab(tab: TelemetryTab) -> None:
    assert "ASCII Decode" not in [tab._log_tabs.tabText(i) for i in range(tab._log_tabs.count())]


def test_every_received_row_gets_an_ascii_row(tab: TelemetryTab) -> None:
    tab._append_row(callsign="OK0LSR", sat_name="LASARsat", data=_GREETING_HEX, norad=62391)
    tab._append_row(callsign="OK0LSR", sat_name="LASARsat", data=_DOS_HEX, norad=62391)
    tab._append_row(callsign="JS1YSD", sat_name="ARICA-2", data="[HK1] 0123", norad=68796)
    assert tab._table.rowCount() == tab._ascii_table.rowCount() == 3
    texts = [tab._ascii_table.item(r, 3).text() for r in range(3)]  # type: ignore[union-attr]
    assert texts == ["OK0LSR>CQ:Celebrating 10 000 orbits!", "OK0LSR>CQ:DOS,80,0,0<0x00>", "—"]
    assert tab._ascii_table.item(0, 1).text() == "OK0LSR"  # type: ignore[union-attr]
    assert tab._ascii_table.item(0, 2).text() == "LASARsat"  # type: ignore[union-attr]


def test_clear_empties_both_tables(tab: TelemetryTab) -> None:
    tab._append_row(callsign="OK0LSR", sat_name="LASARsat", data=_GREETING_HEX, norad=62391)
    tab._on_clear()
    assert tab._table.rowCount() == tab._ascii_table.rowCount() == 0


def test_copy_gives_log_lines_that_the_help_window_can_read(
    tab: TelemetryTab, qtbot: QtBot
) -> None:
    tab._append_row(callsign="OK0LSR", sat_name="LASARsat", data=_DOS_HEX, norad=62391)
    tab._ascii_table.selectAll()
    tab._copy_ascii_rows()
    text = QApplication.clipboard().text()
    assert text.startswith("[0 ") and " UTC] " in text
    assert text.endswith("] OK0LSR>CQ:DOS,80,0,0<0x00>")
    from ui.telemetry_paste import parse_pasted_line

    parsed = parse_pasted_line(text)
    assert parsed is not None and parsed.callsign == "OK0LSR"
    assert parsed.payload == b"DOS,80,0,0\x00"
