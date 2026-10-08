"""FT4 QSO auto-logging at RR73, the always-available Log QSO button, ft4_log upgrade."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from comms.ft4.qso import Ft4QsoManager, QsoState, ensure_ft4_log_schema  # noqa: E402
from data.database import SCHEMA_SQL  # noqa: E402 -- must follow importorskip above
from data.lotw_names import band_freq_hz, lotw_sat_name  # noqa: E402
from ui.ft4_tab import Ft4Tab  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _make_tab(qtbot: QtBot) -> Ft4Tab:
    radio = SimpleNamespace(
        _sat_name_label=SimpleNamespace(text=lambda: "RS-44 (DOSAAF-85)"),
        _norad_label=SimpleNamespace(text=lambda: "44909"),
    )
    conn = _conn()
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, alt_names) VALUES (44909, 'DOSAAF-85', ?)",
        ('["RS-44"]',),
    )
    tab = Ft4Tab(conn, radio)
    qtbot.addWidget(tab)
    tab._my_call = "JF9SOM"
    tab._my_grid = "PM86"
    rig = SimpleNamespace(last_ul_hz=145_990_000.4, last_dl_hz=435_610_000.2)
    setattr(tab, "_tx_rig", lambda: rig)  # noqa: B010 -- mypy forbids assigning a method
    return tab


def _rows(tab: Ft4Tab) -> list[Any]:
    rows: list[Any] = tab._conn.execute("SELECT * FROM ft4_log").fetchall()
    return rows


def test_schema_upgrade_adds_downlink_column() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ft4_log (id INTEGER PRIMARY KEY, qso_date TEXT NOT NULL, "
        "time_on TEXT NOT NULL, time_off TEXT, call TEXT NOT NULL, gridsquare TEXT, "
        "rst_sent TEXT, rst_rcvd TEXT, freq_hz INTEGER, norad_cat_id INTEGER, sat_name TEXT)"
    )
    ensure_ft4_log_schema(conn)
    assert "freq_rx_hz" in {r[1] for r in conn.execute("PRAGMA table_info(ft4_log)")}
    ensure_ft4_log_schema(conn)  # idempotent


def test_auto_log_when_our_rr73_has_been_sent(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    qso = tab._get_qso_manager()
    assert qso is not None
    qso.start_cq()
    qso.advance("JF9SOM JF1PTU PM95", their_snr=-16)
    qso.advance("JF9SOM JF1PTU R-07", their_snr=-11)
    assert qso.state == QsoState.CONFIRM  # RR73 queued, not on the air yet
    assert _rows(tab) == []
    tab._last_tx_msg = "JF1PTU JF9SOM RR73"
    tab._on_tx_finished()  # RR73 has now been transmitted
    rows = _rows(tab)
    assert len(rows) == 1
    r = rows[0]
    assert (r["call"], r["rst_sent"], r["rst_rcvd"], r["gridsquare"]) == (
        "JF1PTU",
        "-16",
        "-07",
        "PM95",
    )
    assert (r["freq_hz"], r["freq_rx_hz"]) == (145_000_000, 435_000_000)  # band only
    assert (r["sat_name"], r["norad_cat_id"]) == ("RS-44", 44909)  # LoTW name
    # the RR73 is repeated and their 73 arrives: still exactly one row
    tab._on_tx_finished()
    qso.advance("JF9SOM JF1PTU 73")
    tab._auto_log_qso()
    assert len(_rows(tab)) == 1


def test_auto_log_when_their_rr73_is_received(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    qso = tab._get_qso_manager()
    assert qso is not None
    qso.respond_with_grid("JA9DRP", "PM86", their_snr_db=-10)
    qso.advance("JF9SOM JA9DRP -08")  # their report; we answer with R-report
    assert qso.state == QsoState.RREPORT_SENT
    # their RR73 -> we send 73 and the QSO is complete; the tab logs it
    msgs: Any = [SimpleNamespace(text="JF9SOM JA9DRP RR73", snr_db=-9.0)]
    tab._auto_advance_qso(msgs, True)
    assert qso.state.name == "LOGGED"
    assert [r["call"] for r in _rows(tab)] == ["JA9DRP"]


def test_unlogged_qso_stays_loggable_after_the_next_one_starts(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    qso = tab._get_qso_manager()
    assert qso is not None
    qso.respond_with_grid("JF6BCC", "PM53", their_snr_db=-4)  # never reaches RR73
    tab._update_qso_display()
    assert tab._log_btn.isEnabled()
    qso.start_cq()  # operator moves on
    qso.advance("JF9SOM JH2LMH PM95", their_snr=-5)  # a new QSO is under way
    tab._update_qso_display()
    assert tab._log_btn.isEnabled()
    tab._on_log_qso()  # logs the earlier QSO, not the running one
    assert [r["call"] for r in _rows(tab)] == ["JF6BCC"]
    assert qso.session.their_call == "JH2LMH"  # the running QSO was not cleared
    assert qso.state == QsoState.EXCHANGE


def test_log_button_available_from_idle_after_clear(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    qso = tab._get_qso_manager()
    assert qso is not None
    qso.respond_with_grid("JF6BCC", "PM53")
    tab._on_clear_qso()
    assert qso.state == QsoState.IDLE
    assert tab._log_btn.isEnabled()
    tab._on_log_qso()
    assert [r["call"] for r in _rows(tab)] == ["JF6BCC"]
    assert not tab._log_btn.isEnabled()  # nothing left to log


def test_manager_never_logs_a_session_twice() -> None:
    conn = _conn()
    qso = Ft4QsoManager("JF9SOM", "PM86")
    qso.respond_with_grid("JA9DRP", "PM86")
    qso.log_qso(conn)
    qso.log_qso(conn)
    assert conn.execute("SELECT COUNT(*) FROM ft4_log").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("norad", "name", "alts", "expected"),
    [
        (44909, "DOSAAF-85", '["RS-44"]', "RS-44"),
        (43803, "JY1Sat", '["JO-97", "Jordan-OSCAR 97"]', "JO-97"),
        (53106, "GREENCUBE", '["IO-117"]', "IO-117"),
        (43017, "FOX-1B", '["AO-91", "RADFXSAT"]', "AO-91"),
        (42761, "ZHUHAI-1 OVS-01", '["CAS 4A", "BJ1SK"]', "CAS-4A"),
        (25544, "ISS", '["ZARYA", "RS0ISS", "NA1SS"]', "ARISS"),  # LoTW's ID for the ISS
        (43770, "FOX-1C", '["AO-95Fox-1Cliff"]', "FOX-1C"),  # AO-95 is not on LoTW's list
        (44881, "CAS-6 (TO-108)", '["2019-093C"]', "TO-108"),
        (24278, "JAS-2", '["FO-29"]', "FO-29"),
        (27607, "SAUDISAT 1C", '["SO-50"]', "SO-50"),
        (7530, "OSCAR 7", '["AO-7", "AMSAT OSCAR 7"]', "AO-7"),
        (39444, "FUNCUBE-1", '["AO-73"]', "AO-73"),
        (42017, "NAYIF-1", '["EO-88", "FUNCUBE-5"]', "EO-88"),
        (12345, "NEWSAT", None, "NEWSAT"),
    ],
)
def test_lotw_sat_name(norad: int, name: str, alts: str | None, expected: str) -> None:
    assert lotw_sat_name(norad, name, alts) == expected


def test_band_freq_rounds_down_to_the_mhz() -> None:
    assert band_freq_hz(145_988_148.0) == 145_000_000
    assert band_freq_hz(435_619_315.7) == 435_000_000
    assert band_freq_hz(None) == 0
    assert band_freq_hz(0) == 0


def test_export_writes_lotw_satellite_names_for_every_mode(qtbot: QtBot) -> None:
    """FT4/Q65/APRS rows all come out with the LoTW name, even older rows saved
    with the SATNOGS name."""
    from PySide6.QtCore import QDate

    from ui.log_export_dialog import LogExportDialog

    conn = _conn()
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, alt_names) VALUES (44909, 'DOSAAF-85', ?)",
        ('["RS-44"]',),
    )
    ensure_ft4_log_schema(conn)
    conn.execute(
        "CREATE TABLE q65_log (id INTEGER PRIMARY KEY, qso_date TEXT, time_on TEXT, time_off TEXT,"
        " call TEXT, gridsquare TEXT, rst_sent TEXT, rst_rcvd TEXT, freq_hz INTEGER,"
        " norad_cat_id INTEGER, sat_name TEXT)"
    )
    conn.execute(
        "CREATE TABLE aprs_log (id INTEGER PRIMARY KEY, received_at TEXT, callsign TEXT, via TEXT,"
        " latitude_deg REAL, longitude_deg REAL, comment TEXT, raw_frame TEXT, norad_sat INTEGER)"
    )
    conn.execute(
        "INSERT INTO ft4_log (qso_date,time_on,call,norad_cat_id,sat_name)"
        " VALUES ('20261006','080000','JA1AAA',44909,'DOSAAF-85')"
    )
    conn.execute(
        "INSERT INTO q65_log (qso_date,time_on,call,norad_cat_id,sat_name)"
        " VALUES ('20261006','081000','JA1BBB',44909,'DOSAAF-85')"
    )
    conn.execute(
        "INSERT INTO aprs_log (received_at,callsign,norad_sat)"
        " VALUES ('2026-10-06 08:20:00','JA1CCC>APRS',44909)"
    )
    conn.commit()
    dlg = LogExportDialog(conn)
    qtbot.addWidget(dlg)
    dlg._from_edit.setDate(QDate(2026, 10, 6))
    dlg._to_edit.setDate(QDate(2026, 10, 6))
    records = [r for _, r in dlg._collect_records()]
    assert len(records) == 3
    assert all("<SAT_NAME:5>RS-44" in r for r in records)
    ft4, q65, aprs = records
    assert "<MODE:3>FT4" in ft4
    assert "<MODE:4>DATA" in q65  # LoTW's list has no Q65
    assert "<MODE:6>PACKET" in aprs  # LoTW's name for the packet mode
    # APRS via a satellite is 2 m up and down; LoTW needs a BAND on every record
    assert "<BAND:2>2M" in aprs
    assert "<BAND_RX:2>2M" in aprs
    assert dlg._unlisted_satellites == set()


def test_every_id_in_the_lotw_list_resolves_to_itself() -> None:
    from data.lotw_satellites import LOTW_SATELLITES

    assert len(LOTW_SATELLITES) >= 100
    assert "RS-44" in LOTW_SATELLITES
    for sat_id in LOTW_SATELLITES:
        assert lotw_sat_name(None, "x", f'["{sat_id}"]') == sat_id


def test_is_lotw_satellite_is_an_exact_match() -> None:
    from data.lotw_names import is_lotw_satellite

    assert is_lotw_satellite("AO-7")
    assert not is_lotw_satellite("AO7")  # LoTW rejects this spelling
    assert not is_lotw_satellite("DOSAAF-85")
    assert not is_lotw_satellite("")


@pytest.mark.parametrize(
    ("hz", "band"),
    [
        (145_000_000, "2M"),
        (145_992_560, "2M"),
        (435_000_000, "70CM"),
        (435_619_315, "70CM"),
        (29_000_000, "10M"),
        (1_269_000_000, "23CM"),
        (2_400_000_000, "13CM"),
        (0, ""),
        (None, ""),
        (7_500_000_000, ""),
    ],
)
def test_adif_band(hz: float | None, band: str) -> None:
    from ui.adif_utils import adif_band

    assert adif_band(hz) == band


def test_satellite_record_has_everything_lotw_needs() -> None:
    from ui.adif_utils import build_satellite_record

    rec = build_satellite_record(
        call="JF1PTU",
        qso_date="20261006",
        time_on="080515",
        time_off="080645",
        mode="FT4",
        sat_name="RS-44",
        freq_hz=145_000_000,
        freq_rx_hz=435_000_000,
        rst_sent="-16",
        rst_rcvd="-07",
        gridsquare="PM95",
    )
    for needed in (
        "<CALL:6>JF1PTU",
        "<QSO_DATE:8>20261006",
        "<TIME_ON:6>080515",
        "<BAND:2>2M",
        "<BAND_RX:4>70CM",
        "<MODE:3>FT4",
        "<PROP_MODE:3>SAT",
        "<SAT_NAME:5>RS-44",
    ):
        assert needed in rec
    assert rec.endswith("<EOR>\n")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("TEVEL2-4", "TEV2-4"), ("TEVEL2-9", "TEV2-9"), ("TEVEL-3", "TEVEL3"), ("UKUBE-1", "UKUBE1")],
)
def test_lotw_ids_with_irregular_spellings(name: str, expected: str) -> None:
    assert lotw_sat_name(None, name, None) == expected


def test_old_log_rows_are_rewritten_to_lotw_names() -> None:
    from data.lotw_names import normalize_logged_satellite_names

    conn = _conn()
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, alt_names) VALUES (44909, 'DOSAAF-85', ?)",
        ('["RS-44"]',),
    )
    ensure_ft4_log_schema(conn)
    conn.execute(
        "INSERT INTO ft4_log (qso_date,time_on,call,norad_cat_id,sat_name)"
        " VALUES ('20261006','080000','JA1AAA',44909,'DOSAAF-85')"
    )
    conn.execute(
        "INSERT INTO ft4_log (qso_date,time_on,call,norad_cat_id,sat_name)"
        " VALUES ('20261006','090000','JA1BBB',NULL,'Unknown')"
    )
    conn.commit()
    assert normalize_logged_satellite_names(conn) == 1
    assert normalize_logged_satellite_names(conn) == 0  # idempotent
    names = [r[0] for r in conn.execute("SELECT sat_name FROM ft4_log ORDER BY id")]
    assert names == ["RS-44", "Unknown"]


def test_band_freqs_from_transmitter_fields() -> None:
    from data.lotw_names import band_freqs_from_transmitter

    iss = {"uplink_low": 145_825_000, "downlink_low": 145_825_000, "description": "APRS"}
    assert band_freqs_from_transmitter(iss) == (145_000_000, 145_000_000)
    uhf_digi = {"uplink_low": 145_900_000, "uplink_high": None, "downlink_low": 437_100_000}
    assert band_freqs_from_transmitter(uhf_digi) == (145_000_000, 437_000_000)
    assert band_freqs_from_transmitter(
        {"downlink_low": 435_610_000, "downlink_high": 435_670_000}
    ) == (
        0,
        435_000_000,
    )
    assert band_freqs_from_transmitter(None) == (0, 0)


def test_aprs_band_follows_the_logged_frequencies() -> None:
    from ui.adif_utils import aprs_band_fields

    assert aprs_band_fields()["BAND"] == "2M"  # no frequency logged: 2 m both ways
    uhf = aprs_band_fields(145_000_000, 437_000_000)
    assert (uhf["BAND"], uhf["BAND_RX"]) == ("2M", "70CM")
    assert aprs_band_fields(435_000_000, 435_000_000)["BAND"] == "70CM"


def test_export_uses_the_aprs_row_frequencies(qtbot: QtBot) -> None:
    from PySide6.QtCore import QDate

    from comms.aprs.log_db import ensure_aprs_log_schema
    from ui.log_export_dialog import LogExportDialog

    conn = _conn()
    ensure_aprs_log_schema(conn)
    conn.execute(
        "INSERT INTO aprs_log (received_at,callsign,norad_sat,freq_hz,freq_rx_hz)"
        " VALUES ('2026-10-06 08:20:00','JA1CCC>APRS',NULL,145000000,437000000)"
    )
    conn.execute(
        "INSERT INTO aprs_log (received_at,callsign) VALUES ('2026-10-06 08:30:00','JA1DDD>APRS')"
    )
    conn.commit()
    dlg = LogExportDialog(conn)
    qtbot.addWidget(dlg)
    dlg._from_edit.setDate(QDate(2026, 10, 6))
    dlg._to_edit.setDate(QDate(2026, 10, 6))
    uhf, old = [r for _, r in dlg._collect_records()]
    assert "<BAND:2>2M" in uhf
    assert "<BAND_RX:4>70CM" in uhf
    assert "<BAND:2>2M" in old
    assert "<BAND_RX:2>2M" in old  # a row without frequencies falls back to 2 m


def test_their_rr73_is_answered_with_73_before_tx_stops(qtbot: QtBot) -> None:
    """RR73 from the partner -> keep TX on until our closing 73 has gone out."""
    from comms.ft4.codec import Ft4Message

    tab = _make_tab(qtbot)
    qso = tab._get_qso_manager()
    assert qso is not None
    qso.respond_with_grid("JH1NHK", "PM95", -10)
    qso.advance("JF9SOM JH1NHK -10", their_snr=-1)  # their report -> our R-report
    assert qso.state == QsoState.RREPORT_SENT
    tab._tx_enabled = True
    tab._auto_advance_qso([Ft4Message("JF9SOM JH1NHK RR73", 980.0, 3.0, 0.1)], True)
    assert qso.state == QsoState.LOGGED
    assert tab._tx_edit.text() == "JH1NHK JF9SOM 73"
    assert tab._tx_enabled  # TX must stay on for the 73
    assert len(_rows(tab)) == 1  # ... and the QSO is logged already
    # a burst that was already on the air (their report was R-01) must not end it
    tab._last_tx_msg = "JH1NHK JF9SOM R-01"
    tab._on_tx_finished()
    assert tab._tx_enabled
    # our 73 has now been sent: TX stops and the box is cleared
    tab._last_tx_msg = "JH1NHK JF9SOM 73"
    tab._on_tx_finished()
    assert not tab._tx_enabled
    assert tab._tx_edit.text() == ""
