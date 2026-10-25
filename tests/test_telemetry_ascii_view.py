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
    looks_like_text,
    row_ascii,
    summary_text,
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


def test_text_helpers() -> None:
    assert dotted_ascii(b"A\x00B") == "A.B"
    assert escaped_ascii(b"A\x00\xffB") == "A<0x00><0xFF>B"
    assert looks_like_text(b"DOS,80,0,0\x00")
    assert looks_like_text(b"")
    assert not looks_like_text(b"\x04\x00\x09HA2GRB\xff")


def test_binary_frames_have_no_text_form_so_they_show_a_size_or_a_summary() -> None:
    raw = b"\x04\x00\x09HA2GRB\xff"
    assert frame_ascii(raw) == "(binary 10 bytes)"
    assert frame_ascii(raw, "[PSU] a: 1  b: 2") == "[PSU] a: 1  b: 2"


def test_a_binary_ax25_payload_keeps_its_header_in_front_of_the_summary() -> None:
    frame = _address("APRS") + _address("OK0LSR", last=True) + b"\x03\xf0" + bytes(range(0, 20))
    assert frame_ascii(frame) == "OK0LSR>APRS: (binary 20 bytes)"
    assert frame_ascii(frame, "[T] x: 1") == "OK0LSR>APRS: [T] x: 1"


def test_summary_lists_the_first_decoded_fields_and_skips_protocol_headers() -> None:
    from comms.telemetry.decoder import decode_telemetry

    tf = decode_telemetry("HA2GRB", _grbbeta_psu_frame(), 60237)
    text = summary_text(tf)
    assert text is not None
    assert text.startswith("[PSU（電源系・CSPバイナリ）] PSU 時刻（RTC・UTC）: 2026-")
    assert "CSP 優先度" not in text and "CSP 送信元" not in text  # header fields are skipped
    assert text.count(": ") == 4  # four fields, after the frame type in brackets
    assert summary_text(None) is None
    assert summary_text(decode_telemetry("X", b"\x00", 99999999)) is None


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


_GR_AX25 = (
    "-> Packet from OK0KST\n"
    "Container:\n"
    "    callsign = u'CQ    '\n"
    "    ssid = 0\n"
    "    callsign = u'OK0KST'\n"
    "    ssid = 0\n"
    "    info = b'U,9008906,9008995,1,0,1632,1537,1475,6,JA1HCB,"
    "8048,980794,98,120\\x00' (total 66)\n"
)
_GR_DUMP = "-> Packet from FOO\n0000: 04 00 09 48 41 32 47 52 42 ff\n"


def test_gr_satellites_ax25_packet_shows_as_the_log_line(tab: TelemetryTab) -> None:
    tab._on_gr_telemetry(_GR_AX25)
    assert tab._table.rowCount() == tab._ascii_table.rowCount() == 1
    assert tab._ascii_table.item(0, 3).text() == (  # type: ignore[union-attr]
        "OK0KST>CQ:U,9008906,9008995,1,0,1632,1537,1475,6,JA1HCB,8048,980794,98,120<0x00>"
    )


def test_gr_satellites_binary_dump_shows_its_size(tab: TelemetryTab) -> None:
    tab._on_gr_telemetry(_GR_DUMP)
    assert tab._ascii_table.item(0, 3).text() == "(binary 10 bytes)"  # type: ignore[union-attr]


def test_a_row_that_kept_no_bytes_falls_back_to_its_hex_text() -> None:
    assert row_ascii(b"", _GREETING_HEX) == "OK0LSR>CQ:Celebrating 10 000 orbits!"


def _grbbeta_psu_frame() -> bytes:
    import struct

    header = (2 << 30) | (2 << 25) | (1 << 20) | (10 << 14) | (9 << 8) | 0x01
    body = struct.pack(
        "<IIIIHII" + "H" * 9 + "BBB" + "H" * 7 + "HHHHH",
        1_792_000_000, 1000, 2000, 3, 4, 5, 6,
        3300, 3301, 3302, 100, 101, 102, 5, 6, 7,
        0b101, 0b0100101, 2,
        11, 12, 13, 14, 15, 16, 17,
        8100, 20, 30, 293, 295,
    )  # fmt: skip
    return struct.pack(">I", header) + body


def test_a_binary_row_of_a_satellite_with_a_definition_shows_the_summary(
    tab: TelemetryTab,
) -> None:
    from comms.telemetry.ascii_view import format_field_value  # noqa: F401  (public helper)
    from ui.telemetry_tab import format_frame_hex

    tab._append_row(
        callsign="HA2GRB",
        sat_name="GRBBeta",
        data=format_frame_hex(_grbbeta_psu_frame()),
        norad=60237,
    )
    text = tab._ascii_table.item(0, 3).text()  # type: ignore[union-attr]
    assert text.startswith("[PSU（電源系・CSPバイナリ）] PSU 時刻（RTC・UTC）: 2026-")


def test_a_binary_row_without_a_definition_shows_its_size(tab: TelemetryTab) -> None:
    from ui.telemetry_tab import format_frame_hex

    tab._append_row(
        callsign="X", sat_name="Unknown", data=format_frame_hex(bytes(range(40))), norad=None
    )
    assert tab._ascii_table.item(0, 3).text() == "(binary 40 bytes)"  # type: ignore[union-attr]


def test_a_row_with_a_decoded_frame_shows_its_summary_even_without_bytes(
    tab: TelemetryTab,
) -> None:
    from comms.telemetry.decoder import decode_telemetry

    decoded = decode_telemetry("HA2GRB", _grbbeta_psu_frame(), 60237)
    tab._append_row(
        callsign="HADES", sat_name="HADES-L", data="[PSU] 0123", norad=60237, decoded=decoded
    )
    assert tab._ascii_table.item(0, 3).text().startswith("[PSU")  # type: ignore[union-attr]


def test_a_summary_makes_text_fields_printable_and_short() -> None:
    from comms.telemetry.decoder import TelemetryField, TelemetryFrame

    garbage = "J�W\x00W··z" + "x" * 40
    frame = TelemetryFrame(
        norad=1,
        callsign="X",
        satellite_name="X",
        raw_hex="",
        fields=[
            TelemetryField("n", "Number", 5, 5.0, "", is_integer=True),
            TelemetryField("m", "Message", 0, 0.0, garbage, is_string=True),
        ],
        telemetry_label="Msg",
    )
    text = summary_text(frame)
    assert text is not None
    assert text.startswith("[Msg] Number: 5  Message: J.W.W")
    assert "�" not in text and "\x00" not in text
    assert text.endswith("…")
