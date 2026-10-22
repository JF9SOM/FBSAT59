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


def test_window_lists_packets_and_search_hides_rows(qtbot: QtBot) -> None:
    win = AprsIsWindow("JF9SOM")
    qtbot.addWidget(win)
    win._on_line(_DIGIPEATED)
    win._on_line(_BEACON)
    win._on_line("# comment")
    assert win.row_count() == 2
    assert win._table.item(0, 1).text() == "SONATE-2"
    assert win._table.item(1, 1).text() == "LASARsat"
    win._search_edit.setText("lasarsat")
    assert win._table.isRowHidden(0) and not win._table.isRowHidden(1)
    win._search_edit.setText("")
    assert not win._table.isRowHidden(0)


def test_window_filter_follows_the_ticked_satellites(qtbot: QtBot) -> None:
    win = AprsIsWindow()
    qtbot.addWidget(win)
    for name, box in win._sat_checks.items():
        box.setChecked(name == "SONATE-2")
    assert win.selected_satellites() == ["SONATE-2"]
    assert win.current_filter() == "d/DP0SNX b/DP0SNX"
