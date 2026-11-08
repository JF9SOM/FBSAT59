"""Unit tests for ui/aprs_tab.py.

Covers the "Open in Google Maps" right-click affordance on the received-packet
log: a packet that carries a position stashes its (lat, lon) on the list item
so the context menu can build a maps URL; a packet without a position stashes
nothing. Also checks the URL builder itself.

Uses pytest-qt's qtbot (per CLAUDE.md — new QWidget tests must register the
widget with qtbot.addWidget to avoid the Qt object-lifetime segfault the manual
QApplication + close() pattern has caused).
"""

from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import comms.aprs.engine as engine_mod
from ui.aprs_tab import AprsTab, _google_maps_url


class _FakeRadioControl(QWidget):
    """QWidget stand-in matching AprsTab's radio_control parameter type."""

    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)
    transmitter_changed_ = Signal(object)

    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return c


@pytest.fixture(autouse=True)
def _reset_engine_singleton() -> None:
    """AprsEngine is a process-wide singleton; drop it between tests so each
    AprsTab gets a fresh one wired to its own in-memory DB."""
    engine_mod.AprsEngine._instance = None
    yield
    engine_mod.AprsEngine._instance = None


def _make_tab(qtbot: QtBot, conn: sqlite3.Connection) -> AprsTab:
    tab = AprsTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def test_google_maps_url_has_pin_and_zoom() -> None:
    url = _google_maps_url(35.4817, 139.7210)
    assert url == "https://www.google.com/maps?q=35.481700,139.721000&z=15"


def test_google_maps_url_handles_southern_western_hemisphere() -> None:
    url = _google_maps_url(-33.8688, -151.2093)
    assert "q=-33.868800,-151.209300" in url


def test_positioned_packet_stashes_coords_on_item(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JA4GWS-9",
        via="TCPIP",
        comment="Pos 35.4817,139.7210",
        raw_frame="=3528.90N/13943.26E>",
        lat=35.4817,
        lon=139.7210,
    )
    item = tab._log_list.item(tab._log_list.count() - 1)
    coords = item.data(Qt.ItemDataRole.UserRole)
    assert coords == (35.4817, 139.7210)


def test_positionless_packet_stashes_nothing(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JG1RBF-7",
        via="TCPIP",
        comment="MSG->JF1BQZ-9: Good afternoon",
        raw_frame=":JF1BQZ-9 :Good afternoon{10",
    )
    item = tab._log_list.item(tab._log_list.count() - 1)
    assert item.data(Qt.ItemDataRole.UserRole) is None


def test_open_item_on_map_emits_url_for_positioned_item(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JA4GWS-9",
        via="TCPIP",
        comment="Pos 35.4817,139.7210",
        raw_frame="",
        lat=35.4817,
        lon=139.7210,
    )
    item = tab._log_list.item(tab._log_list.count() - 1)

    with qtbot.waitSignal(tab.open_map_url, timeout=1000) as blocker:
        tab._open_item_on_map(item)

    assert blocker.args == ["https://www.google.com/maps?q=35.481700,139.721000&z=15"]


def test_open_item_on_map_noop_for_positionless_item(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab.append_packet(callsign="JG1RBF-7", via="", comment="hi", raw_frame="")
    item = tab._log_list.item(tab._log_list.count() - 1)

    triggered: list[str] = []
    tab.open_map_url.connect(triggered.append)
    tab._open_item_on_map(item)
    assert triggered == []


# --------------------------------------------------------------------------- #
# Show: Plain / Raw toggle
# --------------------------------------------------------------------------- #


def test_display_toggle_switches_between_plain_and_raw(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    assert tab._display_mode == "plain"  # default

    tab.append_packet(
        callsign="JA4GWS-9",
        via="ARISS",
        comment="Pos 35.48,139.72",
        raw_frame="!3540.00N/13945.00E-shack",
        plain="house · 35.4800°N 139.7200°E",
    )
    item = tab._log_list.item(tab._log_list.count() - 1)
    assert item.text().endswith("house · 35.4800°N 139.7200°E")

    # Switch to Raw — the existing row must re-render to the on-air info field.
    tab._display_combo.setCurrentIndex(tab._display_combo.findData("raw"))
    assert tab._display_mode == "raw"
    assert item.text().endswith("!3540.00N/13945.00E-shack")

    # And back to Plain.
    tab._display_combo.setCurrentIndex(tab._display_combo.findData("plain"))
    assert item.text().endswith("house · 35.4800°N 139.7200°E")


def test_plain_mode_shows_origin_callsign_for_relayed_packet(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """A third-party / I-Gate-relayed packet's frame source is the relaying
    I-Gate (e.g. JH9YVX-10), not the station that actually sent it. Plain
    mode should read as the originating station; Raw mode keeps the on-air
    frame source untouched."""
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JH9YVX-10",
        via="NOGATE",
        comment="[}] JE9VAX-14>APK004,...",
        raw_frame="}JE9VAX-14>APK004,TCPIP,JH9YVX-10*:!3540.00N/13945.00E-hi",
        plain="truck moving · 36.9980°N 136.8763°E · NE 56 km/h",
        plain_callsign="JE9VAX-14",
    )
    item = tab._log_list.item(tab._log_list.count() - 1)

    assert "JE9VAX-14: truck moving" in item.text()
    assert "JH9YVX-10" not in item.text()

    tab._display_combo.setCurrentIndex(tab._display_combo.findData("raw"))
    assert "JH9YVX-10,NOGATE: }JE9VAX-14>APK004" in item.text()


def test_display_mode_persists_to_app_settings(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab._display_combo.setCurrentIndex(tab._display_combo.findData("raw"))

    row = conn.execute("SELECT value FROM app_settings WHERE key = 'aprs_display_mode'").fetchone()
    assert row is not None and row["value"] == "raw"

    # A fresh tab on the same DB restores the saved mode.
    tab2 = AprsTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab2)
    assert tab2._display_mode == "raw"


# ---------------------------------------------------------------------------
# Transmit: TX level slider and what happens when the engine cannot send
# ---------------------------------------------------------------------------


class _NoSignal:
    """Accepts connect()/disconnect() like a Qt signal."""

    def connect(self, *_args: object) -> None:
        return None

    def disconnect(self, *_args: object) -> None:
        return None


class _RecordingEngine:
    """Replaces the tab's AprsEngine for the send tests."""

    def __getattr__(self, name: str) -> _NoSignal:
        # The tab disconnects the engine's signals when it closes.
        return _NoSignal()

    def __init__(self, result: bool) -> None:
        self.result = result
        self.messages: list[tuple[object, ...]] = []
        self.positions: list[tuple[object, ...]] = []
        self.is_running = True

    def send_message(self, *args: object, uiss_style: bool = False) -> bool:
        self.messages.append((*args, uiss_style))
        return self.result

    def send_position(self, *args: object) -> bool:
        self.positions.append(args)
        return self.result


def test_a_message_that_could_not_be_sent_is_kept_and_not_logged(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab._engine = _RecordingEngine(False)  # type: ignore[assignment]
    tab._callsign_edit.setText("JF9SOM")
    tab._to_edit.setText("JA1XYZ")
    tab._msg_edit.setText("hello")
    tab._on_send()
    assert tab._msg_edit.text() == "hello"  # still there, can be sent again
    assert tab._log_list.count() == 0  # no "sent" echo for a packet that never went out
    assert tab._pending_qso == set()


def test_a_message_that_was_sent_is_logged_and_kept(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    tab._engine = _RecordingEngine(True)  # type: ignore[assignment]
    tab._callsign_edit.setText("JF9SOM")
    tab._to_edit.setText("JA1XYZ")
    tab._msg_edit.setText("hello")
    tab._on_send()
    assert tab._msg_edit.text() == "hello"  # kept so it can be sent again
    assert tab._to_edit.text() == "JA1XYZ"
    assert tab._log_list.count() == 1
    assert tab._pending_qso == {"JA1XYZ"}


def test_lasarsat_selects_its_digipeater_in_via_and_restores_it(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab._via_edit.setCurrentText("WIDE")
    tab._on_transmitter_changed({"norad_cat_id": 62391, "baud": 9600})
    assert tab._via_edit.currentText() == "OK0LSR"
    assert tab._saved_via() == "WIDE"  # the user's own Via is what gets persisted
    tab._on_transmitter_changed({"norad_cat_id": 40931, "baud": 1200})
    assert tab._via_edit.currentText() == "WIDE"
    assert tab._saved_via() == "WIDE"


def test_sonate2_selects_its_digipeater_in_via_and_restores_it(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab._via_edit.setCurrentText("ARISS")
    tab._on_transmitter_changed({"norad_cat_id": 59112, "baud": 1200})
    assert tab._via_edit.currentText() == "DP0SNX"
    assert tab._saved_via() == "ARISS"
    tab._on_transmitter_changed({"norad_cat_id": 40931, "baud": 1200})
    assert tab._via_edit.currentText() == "ARISS"


def test_every_listed_satellite_selects_its_own_via(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    from ui.aprs_tab import _SATELLITE_VIA

    tab = _make_tab(qtbot, conn)
    tab._via_edit.setCurrentText("WIDE")
    for norad, call in _SATELLITE_VIA.items():
        tab._on_transmitter_changed({"norad_cat_id": norad, "baud": 1200})
        assert tab._via_edit.currentText() == call
    tab._on_transmitter_changed({"norad_cat_id": 40931, "baud": 1200})  # LAPAN-A2: none known
    assert tab._via_edit.currentText() == "WIDE"
    assert tab._saved_via() == "WIDE"


def test_via_typed_by_hand_is_kept_when_leaving_lasarsat(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab._on_transmitter_changed({"norad_cat_id": 62391, "baud": 9600})
    tab._via_edit.setCurrentText("APRSAT")  # changed by hand while on LASARsat
    tab._on_transmitter_changed({"norad_cat_id": 40931, "baud": 1200})
    assert tab._via_edit.currentText() == "APRSAT"


def test_receive_log_shows_the_destination(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    """A packet addressed to us (UISS style) must show who it was for."""
    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JK4DHT",
        via="DP0SNX*",
        comment="UR599 PM54",
        raw_frame="UR599 PM54",
        dest="JF9SOM",
    )
    assert tab._log_list.item(0).text().endswith("JK4DHT>JF9SOM,DP0SNX*: UR599 PM54")


def test_uiss_format_is_passed_to_the_engine_and_echoed(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    engine = _RecordingEngine(True)
    tab._engine = engine  # type: ignore[assignment]
    tab._callsign_edit.setText("JF9SOM")
    tab._format_combo.setCurrentIndex(tab._format_combo.findData("uiss"))
    tab._to_edit.setText("cq")
    tab._msg_edit.setText("PM86")
    tab._on_send()
    assert engine.messages[0][-1] is True
    assert "JF9SOM>CQ" in tab._log_list.item(tab._log_list.count() - 1).text()
    saved = conn.execute("SELECT value FROM app_settings WHERE key = 'aprs_settings'").fetchone()
    assert '"msg_format": "uiss"' in saved["value"]


def _qso_tab(qtbot: QtBot, conn: sqlite3.Connection) -> AprsTab:
    tab = _make_tab(qtbot, conn)
    tab._callsign_edit.setText("JF9SOM")
    tab._get_my_location = lambda: (36.1, 136.4)  # type: ignore[method-assign]
    return tab


def _rx(tab: AprsTab, src: str, dest: str, text: str) -> int:
    from comms.aprs.parser import AprsPacket

    tab._on_packet_received(
        AprsPacket(
            callsign=src,
            dest=dest,
            via="DP0SNX*",
            data_type=text[:1],
            raw_info=text,
            comment=text,
        )
    )
    return tab._log_list.count() - 1


def test_cq_button_is_half_width_to_leave_room_for_the_message_box(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    assert tab._cq_btn.maximumWidth() == tab._cq_btn.sizeHint().width() // 2


def test_to_box_width_leaves_room_for_the_message_box(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    assert tab._to_edit.maximumWidth() == 90


def test_cq_button_fills_cq_and_our_grid(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _qso_tab(qtbot, conn)
    tab._cq_btn.click()
    assert tab._to_edit.text() == "CQ"
    assert tab._msg_edit.text() == "PM86"
    assert tab._format_combo.currentData() == "uiss"


def test_clicking_a_cq_fills_the_callsign_and_our_grid(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _qso_tab(qtbot, conn)
    row = _rx(tab, "JK4DHT", "CQ", "PM54")
    tab._on_log_item_clicked(tab._log_list.item(row))
    assert tab._to_edit.text() == "JK4DHT"
    assert tab._msg_edit.text() == "PM86"


def test_clicking_a_call_to_us_fills_599_then_73(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _qso_tab(qtbot, conn)
    tab._engine = _RecordingEngine(True)  # type: ignore[assignment]
    row = _rx(tab, "JK4DHT", "JF9SOM", "UR599 PM54")
    tab._on_log_item_clicked(tab._log_list.item(row))
    assert (tab._to_edit.text(), tab._msg_edit.text()) == ("JK4DHT", "599")
    tab._on_send()
    # Calling the same station again now fills 73.
    tab._on_log_item_clicked(tab._log_list.item(row))
    assert tab._msg_edit.text() == "73"
    tab._on_send()
    tab._on_log_item_clicked(tab._log_list.item(row))
    assert tab._msg_edit.text() == "599"


def test_clicking_other_traffic_or_our_own_echo_changes_nothing(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _qso_tab(qtbot, conn)
    row = _rx(tab, "JK4DHT", "JA1XYZ", "hello")
    tab._on_log_item_clicked(tab._log_list.item(row))
    own = _rx(tab, "JF9SOM", "CQ", "PM86")
    tab._on_log_item_clicked(tab._log_list.item(own))
    assert tab._to_edit.text() == "" and tab._msg_edit.text() == ""


def _aprs_log_tab(qtbot: QtBot, conn: sqlite3.Connection) -> AprsTab:
    from comms.aprs.log_db import ensure_aprs_log_schema

    ensure_aprs_log_schema(conn)
    tab = _qso_tab(qtbot, conn)
    tab._engine = _RecordingEngine(True)  # type: ignore[assignment]
    tab._broadcast_adif = lambda *a, **k: None  # type: ignore[method-assign]
    return tab


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT callsign, comment FROM aprs_log"))


def _send(tab: AprsTab, to: str, msg: str) -> None:
    tab._to_edit.setText(to)
    tab._msg_edit.setText(msg)
    tab._on_send()


def test_qso_is_logged_when_73_is_sent_after_599(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _rx(tab, "JK4DHT", "JF9SOM", "UR599 PM54")
    _send(tab, "JK4DHT", "599")
    assert _rows(conn) == []
    _send(tab, "JK4DHT", "73")
    rows = _rows(conn)
    assert [(r["callsign"], r["comment"]) for r in rows] == [("JK4DHT", "QSO PM54")]
    assert conn.execute("SELECT gridsquare FROM aprs_log").fetchone()[0] == "PM54"
    _send(tab, "JK4DHT", "73")  # a repeated 73 does not log again
    assert len(_rows(conn)) == 1


def test_73_without_a_call_from_them_or_a_599_is_not_logged(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _send(tab, "JK4DHT", "599")
    _send(tab, "JK4DHT", "73")  # they never called us
    _rx(tab, "JA1XYZ", "JF9SOM", "hello")
    _send(tab, "JA1XYZ", "73")  # we never sent 599
    assert _rows(conn) == []


def test_receive_log_starts_empty_even_with_logged_qsos(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """Past QSOs live in the QSO log (export); the receive log shows live packets only."""
    from comms.aprs.log_db import ensure_aprs_log_schema

    ensure_aprs_log_schema(conn)
    conn.execute(
        "INSERT INTO aprs_log (received_at, callsign, comment) "
        "VALUES ('2026-10-29 07:34:54', 'JK4DHT', 'QSO PM54')"
    )
    conn.commit()
    tab = _make_tab(qtbot, conn)
    assert tab._log_list.count() == 0
    assert "1" in tab._qso_count_label.text()


def test_cq_button_sends_the_call_when_transmitting_is_possible(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _qso_tab(qtbot, conn)
    engine = _RecordingEngine(True)
    tab._engine = engine  # type: ignore[assignment]
    tab._send_btn.setEnabled(True)
    tab._cq_btn.click()
    assert [m[3:] for m in engine.messages] == [("CQ", "PM86", True)]
    assert tab._msg_edit.text() == "PM86"  # kept, so it can be sent again
    assert "JF9SOM>CQ" in tab._log_list.item(tab._log_list.count() - 1).text()


def test_choosing_a_preset_fills_the_message_without_sending(
    qtbot: QtBot, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tab = _make_tab(qtbot, conn)
    monkeypatch.setattr(tab, "_my_grid", lambda: "PM95")
    sent: list[str] = []
    monkeypatch.setattr(tab, "_on_send", lambda: sent.append("sent"))
    idx = tab._preset_combo.findData("599 {grid}")
    assert idx >= 0
    tab._on_preset_chosen(idx)
    assert tab._msg_edit.text() == "599 PM95"
    assert sent == []
    assert tab._preset_combo.currentIndex() == -1


def test_to_and_message_fields_have_a_clear_button(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    assert tab._to_edit.isClearButtonEnabled()
    assert tab._msg_edit.isClearButtonEnabled()
    assert tab._pos_comment_edit.isClearButtonEnabled()


def test_missing_direwolf_shows_install_hint_and_disables_sending(
    qtbot: QtBot, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("comms.aprs.engine.find_direwolf", lambda: None)
    tab = _make_tab(qtbot, conn)
    tab._rig_connected = True
    tab._refresh_input_source()
    assert "Direwolf is not installed" in tab._input_label.text()
    assert "Help > Direwolf Installation" in tab._input_label.text()
    assert not tab._send_btn.isEnabled()
    assert not tab._pos_send_btn.isEnabled()


def test_sent_rows_are_coloured_pale_blue(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QColor

    tab = _make_tab(qtbot, conn)
    tab.append_packet(
        callsign="JF1PTU>CQ", via="ARISS", comment="[TX→CQ] PM95", raw_frame="", tx=True
    )
    tab.append_packet(callsign="JA1XYZ", via="", comment="hello", raw_frame="")
    sent = tab._log_list.item(tab._log_list.count() - 2)
    received = tab._log_list.item(tab._log_list.count() - 1)
    assert sent.background().color() == QColor("#b3e5fc")
    assert sent.foreground().color() == QColor("#000000")
    # A received row keeps the default look (no background brush is set).
    assert received.background().style() == _Qt.BrushStyle.NoBrush


def test_sending_a_message_and_a_position_marks_the_rows_as_sent() -> None:
    import inspect

    import ui.aprs_tab as aprs_tab

    source = inspect.getsource(aprs_tab.AprsTab)
    assert source.count("tx=True") == 2  # message echo and position echo


def test_packets_addressed_to_us_are_highlighted_yellow(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QListWidgetItem

    tab = _make_tab(qtbot, conn)
    tab._callsign_edit.setText("JF1PTU-5")

    def row(callsign: str, dest: str, addressee: str | None = None) -> QListWidgetItem:
        tab.append_packet(
            callsign=callsign, via="", comment="hi", raw_frame="", dest=dest, addressee=addressee
        )
        return tab._log_list.item(tab._log_list.count() - 1)

    by_dest = row("JA1XYZ", dest="JF1PTU")
    by_addressee = row("JA1XYZ", dest="APRS", addressee="JF1PTU-7")
    assert by_dest.background().color() == QColor(_Qt.GlobalColor.yellow)
    assert by_dest.foreground().color() == QColor("#000000")
    assert by_addressee.background().color() == QColor(_Qt.GlobalColor.yellow)
    # Other stations' CQ, packets for someone else, and our own echoes stay plain.
    for plain_row in (
        row("JA1XYZ", dest="CQ"),
        row("JA1XYZ", dest="JA9ABC", addressee="JA9ABC"),
        row("JF1PTU", dest="JF1PTU"),
    ):
        assert plain_row.background().style() == _Qt.BrushStyle.NoBrush


def test_clear_button_empties_the_screen_but_not_the_saved_history(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    for call in ("JA1XYZ", "JA2ABC"):
        tab.append_packet(callsign=call, via="", comment="hi", raw_frame="", log=True)
    saved = conn.execute("SELECT COUNT(*) FROM aprs_log").fetchone()[0]
    assert tab._log_list.count() == 2 and saved == 2
    tab._log_clear_btn.click()
    assert tab._log_list.count() == 0
    assert conn.execute("SELECT COUNT(*) FROM aprs_log").fetchone()[0] == saved


def test_clear_button_is_on_the_received_packets_title_line(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _make_tab(qtbot, conn)
    tab.resize(1000, 640)
    tab.show()
    qtbot.waitExposed(tab)
    btn = tab._log_clear_btn
    assert btn.parent() is tab._log_group
    # On the title line (above the list), left of the group's right edge.
    assert btn.geometry().bottom() < tab._log_list.geometry().top()
    assert btn.geometry().right() < tab._log_group.width() - 20


def test_input_label_follows_rig_and_sdr_not_the_engine_status(
    qtbot: QtBot, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("comms.aprs.engine.find_direwolf", lambda: "/usr/bin/direwolf")
    tab = _make_tab(qtbot, conn)
    # The engine announcing "Connected (Rig + Direwolf ...)" must not reach the label.
    tab._on_engine_status("Connected (Rig + Direwolf, 1200 baud)")
    assert tab._input_label.text() == "Connect Rig or SDR"
    assert "Direwolf" not in tab._input_label.text()
    tab._sdr_connected = True
    tab._on_engine_status("Connected (SDR — Direwolf, 1200 baud, receive only)")
    assert tab._input_label.text() == "SDR Connected"


def test_own_digipeat_is_blue_and_own_tx_is_pale_blue(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QColor

    tab = _make_tab(qtbot, conn)
    tab._callsign_edit.setText("JF1PTU-6")

    def last() -> object:
        return tab._log_list.item(tab._log_list.count() - 1)

    tab.append_packet(callsign="JF1PTU-6", via="RS0ISS*", comment="x", raw_frame="", dest="CQ")
    assert last().background().color() == QColor("#4fc3f7")  # type: ignore[attr-defined]
    # Not yet digipeated (no used hop) or another station: no blue.
    tab.append_packet(callsign="JF1PTU-6", via="RS0ISS", comment="x", raw_frame="", dest="CQ")
    assert last().background().style() == _Qt.BrushStyle.NoBrush  # type: ignore[attr-defined]
    tab.append_packet(callsign="JA1XYZ", via="RS0ISS*", comment="x", raw_frame="", dest="CQ")
    assert last().background().style() == _Qt.BrushStyle.NoBrush  # type: ignore[attr-defined]
    tab._append_log_item("t", "JF1PTU-6", "RS0ISS", plain="p", raw="r", dest="CQ", tx=True)
    assert last().background().color() == QColor("#b3e5fc")  # type: ignore[attr-defined]


def _rx_msg(tab: AprsTab, src: str, to: str, text: str) -> None:
    from comms.aprs.parser import AprsPacket

    tab._on_packet_received(
        AprsPacket(
            callsign=src,
            dest="APRS",
            via="DP0SNX*",
            data_type=":",
            raw_info=f":{to:<9}:{text}",
            comment=text,
            message_addressee=to,
            message_text=text,
        )
    )


def test_replies_are_not_logged_only_a_73_closes_the_qso(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _send(tab, "JA1XYZ", "CQ")
    _rx_msg(tab, "JA1XYZ", "JF9SOM", "hello")
    _rx_msg(tab, "JA1XYZ", "JF9SOM", "UR 599")
    assert _rows(conn) == []


def test_73_from_the_other_station_logs_the_qso_once(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _send(tab, "JA1XYZ", "CQ")
    _rx_msg(tab, "JA1XYZ", "JF9SOM", "73")
    assert [r["callsign"] for r in _rows(conn)] == ["JA1XYZ"]
    _send(tab, "JA1XYZ", "73")  # our 73 after theirs: the same QSO, not logged again
    _rx_msg(tab, "JA1XYZ", "JF9SOM", "73")
    assert len(_rows(conn)) == 1


def test_73_from_a_station_we_never_messaged_is_not_logged(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _rx_msg(tab, "JA9ZZZ", "JF9SOM", "73")
    assert _rows(conn) == []


def test_level_bar_follows_the_receive_level(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = _make_tab(qtbot, conn)
    levels: list[float | None] = [None]
    tab._engine = type(  # type: ignore[assignment]
        "E", (), {"is_running": True, "rx_level_dbfs": staticmethod(lambda: levels[0])}
    )()
    tab._update_level_bar()
    assert tab._level_bar.value() == 0  # no audio at all: an empty bar
    levels[0] = -30.0
    tab._update_level_bar()
    assert tab._level_bar.value() == 50
    levels[0] = -3.0
    tab._update_level_bar()
    assert tab._level_bar.value() == 95


def test_rr73_in_uiss_style_from_the_other_station_logs_the_qso(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """GitHub Issue #35, 2026-11-08: JF6BCC sent "RR73" in a UISS-style frame (callsign in the
    destination field, no ':' message); we answered "R TU 73". Both must close the QSO."""
    tab = _aprs_log_tab(qtbot, conn)
    tab._callsign_edit.setText("JF1PTU")
    _send(tab, "JF6BCC", "599")
    _rx(tab, "JF6BCC", "JF1PTU-5", "RR73")  # UISS style: no ':' data type
    assert [r["callsign"] for r in _rows(conn)] == ["JF6BCC"]
    _send(tab, "JF6BCC", "R TU 73")  # our closing text: the same QSO, not logged twice
    assert len(_rows(conn)) == 1


def test_r_tu_73_from_us_closes_a_qso_that_they_started(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _rx(tab, "JK4DHT", "JF9SOM", "UR599 PM54")
    _send(tab, "JK4DHT", "599")
    _send(tab, "JK4DHT", "R TU 73")
    assert [(r["callsign"], r["comment"]) for r in _rows(conn)] == [("JK4DHT", "QSO PM54")]


def test_a_report_or_a_number_containing_73_is_not_a_closing(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _aprs_log_tab(qtbot, conn)
    _send(tab, "JA1XYZ", "599")
    _rx_msg(tab, "JA1XYZ", "JF9SOM", "599 PM53 402103")
    _rx_msg(tab, "JA1XYZ", "JF9SOM", "call 1730")
    assert _rows(conn) == []
