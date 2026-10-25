"""The Telemetry tab's paste-only "ASCII Decode" page."""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from ui.telemetry_paste import (
    AsciiPasteWidget,
    format_field_value,
    norad_for_callsign,
    parse_pasted_line,
)

_MRG = (
    "[0 2026-10-19 14:40:07 UTC] "
    "OK0KST>CQ:U,9008906,9008995,1,0,1632,1537,1475,6,JA1HCB,8048,980794,98,120<0x00>"
)
_OBC = "[0 2026-10-20 14:47:20 UTC] OK0KST>CQ:255,0,11,8660379,9117082,1792507744,0,22.20,12.39"
_GRB = (
    "[0 2026-10-20 23:07:35 UTC] "
    "HA2GRB>CQ:U,6860489,71980801,68,2,2011,1976,1868,806,DL7NDR,30675,5635053,103,125<0x00>"
)


def test_parse_splits_a_log_line() -> None:
    p = parse_pasted_line(_MRG)
    assert p is not None
    assert (p.callsign, p.stamp) == ("OK0KST", "2026-10-19 14:40:07 UTC")
    assert p.payload.startswith(b"U,9008906,") and p.payload.endswith(b"120\x00")


def test_parse_takes_a_headerless_line_as_the_payload_and_skips_blank_lines() -> None:
    p = parse_pasted_line("  U,1,2,3  ")
    assert p is not None and p.callsign == "" and p.payload == b"U,1,2,3"
    assert parse_pasted_line("   ") is None
    ssid = parse_pasted_line("JA1ABC-9>APRS,WIDE1-1:hello")
    assert ssid is not None and ssid.callsign == "JA1ABC" and ssid.payload == b"hello"


def test_callsign_finds_the_satellite() -> None:
    assert norad_for_callsign("ok0kst") == 69935
    assert norad_for_callsign("HA2GRB") == 60237
    assert norad_for_callsign("NOSUCH") is None
    assert norad_for_callsign("") is None


def _texts(widget: AsciiPasteWidget) -> list[tuple[str, str]]:
    """(label, value) of every child item, in order."""
    out: list[tuple[str, str]] = []
    for i in range(widget._tree.topLevelItemCount()):
        top = widget._tree.topLevelItem(i)
        assert top is not None
        for j in range(top.childCount()):
            child = top.child(j)
            assert child is not None
            out.append((child.text(0), child.text(1)))
    return out


def test_pasted_lines_are_decoded_with_the_satellite_found_from_the_callsign(qtbot: QtBot) -> None:
    w = AsciiPasteWidget()
    qtbot.addWidget(w)
    w._input.setPlainText("\n".join([_MRG, _OBC, _GRB]))
    w.decode_text()
    assert w._tree.topLevelItemCount() == 3
    titles = [w._tree.topLevelItem(i).text(0) for i in range(3)]  # type: ignore[union-attr]
    assert "KOSTKA" in titles[0] and "MRG" in titles[0] and "2026-10-19 14:40:07" in titles[0]
    assert "OBC" in titles[1]
    assert "GRBBeta" in titles[2]
    fields = dict(_texts(w))
    assert fields["最後にデジピートした局"] == "JA1HCB"
    assert fields["UHF 最後にデジピートした局"] == "DL7NDR"
    assert fields["MCU温度"] == "22.2000 °C"
    assert "3 of 3" in w._status.text()


def test_a_headerless_line_uses_the_tabs_satellite(qtbot: QtBot) -> None:
    w = AsciiPasteWidget()
    qtbot.addWidget(w)
    w._input.setPlainText("255,0,11,8660379,9117082,1792507744,0,22.20,12.39")
    w.decode_text()
    assert "select a satellite" in w._tree.topLevelItem(0).text(1)  # type: ignore[union-attr]
    w.set_default_norad(69935)
    w.decode_text()
    assert "OBC" in w._tree.topLevelItem(0).text(0)  # type: ignore[union-attr]


def test_unknown_callsign_and_undecodable_lines_are_reported_not_dropped(qtbot: QtBot) -> None:
    w = AsciiPasteWidget()
    qtbot.addWidget(w)
    w._input.setPlainText("NOSUCH>CQ:1,2,3\nOK0KST>CQ:<0x01><0x02>")
    w.decode_text()
    assert "no satellite definition" in w._tree.topLevelItem(0).text(1)  # type: ignore[union-attr]
    assert "not decodable" in w._tree.topLevelItem(1).text(1)  # type: ignore[union-attr]
    assert "0 of 2" in w._status.text()


def test_free_text_lines_show_as_a_message(qtbot: QtBot) -> None:
    w = AsciiPasteWidget()
    qtbot.addWidget(w)
    w._input.setPlainText("OK0KST>CQ:hello from space")
    w.decode_text()
    assert ("メッセージ", "hello from space") in _texts(w)


def test_the_satellite_box_overrides_the_callsign(qtbot: QtBot) -> None:
    w = AsciiPasteWidget()
    qtbot.addWidget(w)
    index = w._combo.findData(69935)
    w._combo.setCurrentIndex(index)
    w._input.setPlainText("255,0,11,8660379,9117082,1792507744,0,22.20,12.39")
    w.decode_text()
    assert "OBC" in w._tree.topLevelItem(0).text(0)  # type: ignore[union-attr]


def test_clear_empties_the_page(qtbot: QtBot) -> None:
    w = AsciiPasteWidget()
    qtbot.addWidget(w)
    w._input.setPlainText(_MRG)
    w.decode_text()
    w._clear_btn.click()
    assert w._tree.topLevelItemCount() == 0 and w._input.toPlainText() == ""


def test_format_field_value_matches_the_decoded_fields_tables() -> None:
    from comms.telemetry.decoder import decode_telemetry

    tf = decode_telemetry("OK0KST", b"255,0,11,8660379,9117082,1792507744,0,22.20,12.39", 69935)
    by_name = {f.name: f for f in tf.fields}
    assert format_field_value(by_name["obc_uptime"]) == "8,660,379 s"


def test_the_help_window_wraps_the_paste_widget(qtbot: QtBot) -> None:
    from ui.telemetry_paste import AsciiDecodeWindow

    win = AsciiDecodeWindow()
    qtbot.addWidget(win)
    assert win.windowTitle() == "ASCII Decode"
    win.set_default_norad(69935)
    win._widget._input.setPlainText("255,0,11,8660379,9117082,1792507744,0,22.20,12.39")
    win._widget.decode_text()
    assert "OBC" in win._widget._tree.topLevelItem(0).text(0)  # type: ignore[union-attr]
