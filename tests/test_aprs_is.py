"""APRS-IS monitor: line parsing, satellite matching, filter building, client, window."""

from __future__ import annotations

import socket
import threading

import pytest
from pytestqt.qtbot import QtBot

from comms.aprs.aprs_is import (
    AprsIsClient,
    build_filter,
    match_satellite,
    parse_is_line,
)
from ui.aprs_is_window import AprsIsWindow

_DIGIPEATED = "JA1ABC>APRS,DP0SNX*,qAR,JJ1WTK-6:!3606.25N/13622.50E-TEST"
_BEACON = "OK0LSR>APRS,WIDE1-1,qAR,JA2XYZ:>Hello from LASARsat"


def test_parse_splits_header_and_info() -> None:
    pkt = parse_is_line(_DIGIPEATED)
    assert pkt is not None
    assert (pkt.source, pkt.dest) == ("JA1ABC", "APRS")
    assert pkt.path == ("DP0SNX*", "qAR", "JJ1WTK-6")
    assert pkt.info == "!3606.25N/13622.50E-TEST"


def test_parse_ignores_comments_and_garbage() -> None:
    assert parse_is_line("# aprsc 2.1.23") is None
    assert parse_is_line("") is None
    assert parse_is_line("no header here") is None
    assert parse_is_line(">APRS:x") is None


def test_match_satellite_by_digipeater_and_by_source() -> None:
    digi = parse_is_line(_DIGIPEATED)
    beacon = parse_is_line(_BEACON)
    other = parse_is_line("JA1ABC>APRS,WIDE1*,qAR,JA2XYZ:>x")
    assert digi is not None and beacon is not None and other is not None
    assert match_satellite(digi) == "SONATE-2"
    assert match_satellite(beacon) == "LASARsat"
    assert match_satellite(other) is None
    assert match_satellite(digi, ["LASARsat"]) is None


def test_build_filter_covers_digipeated_and_own_packets() -> None:
    f = build_filter(["SONATE-2", "ISS"], extra="p/JA")
    assert f == "d/DP0SNX/RS0ISS/ARISS b/DP0SNX/RS0ISS/ARISS p/JA"
    assert build_filter([]) == ""


def test_client_logs_in_read_only_with_filter_and_forwards_packets(qtbot: QtBot) -> None:
    received_login: list[str] = []
    ready = threading.Event()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve() -> None:
        conn, _ = srv.accept()
        conn.sendall(b"# aprsc test\r\n")
        received_login.append(conn.makefile("rb").readline().decode().strip())
        conn.sendall(
            b"# logresp JF9SOM unverified, server TEST\r\n"
            b"# keepalive\r\n" + _DIGIPEATED.encode() + b"\r\n"
        )
        ready.set()
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client = AprsIsClient()
    lines: list[str] = []
    statuses: list[str] = []
    client.line_received.connect(lines.append)
    client.status_changed.connect(statuses.append)
    client.start("127.0.0.1", port, "JF9SOM", "d/DP0SNX")
    qtbot.waitUntil(lambda: bool(lines), timeout=5000)
    client.stop()
    srv.close()
    assert statuses[:2] == ["connecting", "connected"]  # "connected" only after the logresp
    assert received_login == ["user JF9SOM pass -1 vers FBSAT59 1.0 filter d/DP0SNX"]
    assert lines == [_DIGIPEATED]  # the "#" comment lines are not forwarded


def test_client_survives_a_silent_server_by_sending_keepalives(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("comms.aprs.aprs_is._RECV_TIMEOUT_S", 0.2)
    monkeypatch.setattr("comms.aprs.aprs_is._IDLE_LIMIT_S", 5.0)
    keepalives: list[bytes] = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve() -> None:
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        f.readline()  # login
        keepalives.append(f.readline())  # first keepalive after the silence
        keepalives.append(f.readline())
        conn.sendall(_DIGIPEATED.encode() + b"\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client = AprsIsClient()
    lines: list[str] = []
    statuses: list[str] = []
    client.line_received.connect(lines.append)
    client.status_changed.connect(statuses.append)
    client.start("127.0.0.1", port, "JF9SOM", "d/DP0SNX")
    qtbot.waitUntil(lambda: bool(lines), timeout=5000)
    client.stop()
    srv.close()
    assert keepalives == [b"#\r\n", b"#\r\n"]
    assert not any(s.startswith("error") for s in statuses)


def test_window_lists_packets_and_search_narrows_the_list(qtbot: QtBot) -> None:
    win = AprsIsWindow("JF9SOM")
    qtbot.addWidget(win)
    win._on_line(_DIGIPEATED)
    win._on_line(_BEACON)
    win._on_line("# comment")
    win._flush_pending()
    assert win.row_count() == 2
    assert win._table.item(0, 1).text() == "SONATE-2"
    assert win._table.item(1, 1).text() == "LASARsat"
    win._search_edit.setText("lasarsat")
    assert win.row_count() == 1
    assert win._table.item(0, 1).text() == "LASARsat"
    win._on_line(_DIGIPEATED)  # a new packet that does not match the search stays out
    win._flush_pending()
    assert win.row_count() == 1
    win._search_edit.setText("")
    assert win.row_count() == 3  # nothing was lost, the search only narrowed the view


def test_window_filter_follows_the_ticked_satellites(qtbot: QtBot) -> None:
    win = AprsIsWindow()
    qtbot.addWidget(win)
    for name, box in win._sat_checks.items():
        box.setChecked(name == "SONATE-2")
    assert win.selected_satellites() == ["SONATE-2"]
    assert win.current_filter() == "d/DP0SNX b/DP0SNX"


def test_select_all_and_none_buttons(qtbot: QtBot) -> None:
    win = AprsIsWindow()
    qtbot.addWidget(win)
    win._select_none_btn.click()
    assert win.selected_satellites() == []
    assert win.current_filter() == ""
    win._select_all_btn.click()
    assert len(win.selected_satellites()) == len(win._sat_checks)
    assert win.current_filter().startswith("d/")


def test_range_filter_uses_the_saved_qth_and_the_presets(qtbot: QtBot) -> None:
    win = AprsIsWindow(location_fn=lambda: (36.1042, 136.375))
    qtbot.addWidget(win)
    win._select_none_btn.click()
    assert win._lat_spin.value() == pytest.approx(36.1042)  # starts at the QTH
    win._preset_btns[2].click()  # 1000 km; also ticks the range box
    assert win._range_check.isChecked()
    assert win.current_filter() == "r/36.1042/136.3750/1000"
    win._extra_edit.setText("p/JA")
    assert win.current_filter() == "r/36.1042/136.3750/1000 p/JA"


def test_qth_button_without_a_saved_position_says_so(qtbot: QtBot) -> None:
    win = AprsIsWindow(location_fn=lambda: None)
    qtbot.addWidget(win)
    win._qth_btn.click()
    assert not win._range_check.isChecked()
    assert "not set" in win._status_label.text()


def test_show_everything_selects_the_full_feed_and_greys_out_the_filters(qtbot: QtBot) -> None:
    from comms.aprs.aprs_is import FULL_FEED_PORT

    win = AprsIsWindow()
    qtbot.addWidget(win)
    assert win._effective_port() == win._port_spin.value()
    win._all_check.setChecked(True)
    assert win.current_filter() == ""
    assert win._effective_port() == FULL_FEED_PORT
    assert not win._select_all_btn.isEnabled()
    assert not win._range_check.isEnabled()
    win._all_check.setChecked(False)
    assert win._select_all_btn.isEnabled()


def test_changing_the_filter_of_a_running_session_sends_it_without_reconnecting(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comms.aprs.aprs_is import NO_MATCH_FILTER

    win = AprsIsWindow()
    qtbot.addWidget(win)
    sent: list[str] = []
    monkeypatch.setattr(AprsIsClient, "running", property(lambda self: True))
    monkeypatch.setattr(AprsIsClient, "update_filter", lambda self, text: sent.append(text))
    win._connected_port = win._effective_port()
    win._select_none_btn.click()
    assert sent == [NO_MATCH_FILTER]
    win._range_check.setChecked(True)
    assert sent[-1].startswith("r/")


def test_restarting_a_session_does_not_report_the_old_one_as_disconnected(qtbot: QtBot) -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    conns: list[socket.socket] = []

    def serve() -> None:
        for _unused in range(2):
            conn, _addr = srv.accept()
            conns.append(conn)
            conn.makefile("rb").readline()  # login
            conn.sendall(b"# logresp JF9SOM unverified, server TEST\r\n")

    threading.Thread(target=serve, daemon=True).start()
    client = AprsIsClient()
    statuses: list[str] = []
    client.status_changed.connect(statuses.append)
    client.start("127.0.0.1", port, "JF9SOM", "d/DP0SNX")
    qtbot.waitUntil(lambda: "connected" in statuses, timeout=5000)
    client.start("127.0.0.1", port, "JF9SOM", "r/35/139/100")  # restart on a new connection
    qtbot.waitUntil(lambda: statuses.count("connected") == 2, timeout=5000)
    qtbot.wait(300)
    assert "disconnected" not in statuses
    assert client.running
    client.stop()
    qtbot.waitUntil(lambda: "disconnected" in statuses, timeout=5000)  # a real stop still reports
    srv.close()
    for conn in conns:
        conn.close()
