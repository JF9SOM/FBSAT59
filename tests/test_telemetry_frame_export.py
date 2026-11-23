"""Tests for exporting the saved Telemetry frames (comms.telemetry.frame_export)."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from comms.telemetry import frame_export as fe
from comms.telemetry.csv_import import parse_line, read_frames

START = datetime(2026, 11, 23, tzinfo=UTC)
END = datetime(2026, 11, 24, tzinfo=UTC)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE telemetry_log (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT,"
        " norad_cat_id INTEGER, callsign TEXT, raw_hex TEXT, parsed_json TEXT, signal_db REAL,"
        " frame_hex TEXT, kind TEXT)"
    )
    return conn


def _add(
    conn: sqlite3.Connection,
    when: str,
    frame_hex: str | None,
    *,
    norad: int | None = 68796,
    callsign: str = "",
    kind: str | None = "hdlc",
    raw_hex: str = "00",
    signal: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO telemetry_log (received_at, norad_cat_id, callsign, raw_hex, signal_db,"
        " frame_hex, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (when, norad, callsign, raw_hex, signal, frame_hex, kind),
    )


def test_kiss_escape_replaces_fend_and_fesc() -> None:
    assert fe.kiss_escape(bytes([0x01, 0xC0, 0xDB, 0x02])) == bytes(
        [0x01, 0xDB, 0xDC, 0xDB, 0xDD, 0x02]
    )


def test_kiss_line_wraps_and_escapes() -> None:
    assert fe.kiss_line(bytes([0x2C, 0xC0])) == "C0 00 2C DB DC C0"


def test_effective_kind_infers_old_rows() -> None:
    assert fe.effective_kind("gr", "X") == "gr"
    assert fe.effective_kind(None, "") == "hdlc"
    assert fe.effective_kind("", "JS1YSD") == "ax25"


def test_select_frames_filters_by_satellite_range_and_kind() -> None:
    conn = _conn()
    _add(conn, "2026-11-23T06:28:25+00:00", "2CC0")
    _add(conn, "2026-11-22T23:59:59+00:00", "AA")  # before the range
    _add(conn, "2026-11-24T00:00:00+00:00", "BB")  # the end is exclusive
    _add(conn, "2026-11-23T07:00:00+00:00", "CC", norad=12345)  # other satellite
    _add(conn, "2026-11-23T07:01:00+00:00", "DD", kind="gr")  # other mode
    _add(conn, "2026-11-23T07:02:00+00:00", "EE", norad=None)  # no NORAD
    frames = fe.select_frames(conn, 68796, START, END, ("ax25", "hdlc"))
    assert [f.hex_text for f in frames] == ["2CC0"]
    assert frames[0].whole


def test_select_frames_orders_oldest_first_and_handles_naive_times() -> None:
    conn = _conn()
    _add(conn, "2026-11-23T08:00:00", "02")  # no time zone: read as UTC
    _add(conn, "2026-11-23T06:00:00+00:00", "01")
    frames = fe.select_frames(conn, 68796, START, END, ("hdlc",))
    assert [f.hex_text for f in frames] == ["01", "02"]


def test_old_row_without_frame_hex_is_payload_only() -> None:
    conn = _conn()
    _add(conn, "2026-11-23T06:00:00+00:00", None, raw_hex="abcd", kind=None)
    (frame,) = fe.select_frames(conn, 68796, START, END, ("hdlc",))
    assert not frame.whole
    assert frame.hex_text == "ABCD"


def test_csv_keeps_payload_only_rows_and_marks_them() -> None:
    conn = _conn()
    _add(conn, "2026-11-23T06:28:25.062+00:00", "2cc0", signal=-12.5)
    _add(conn, "2026-11-23T06:30:00+00:00", None, raw_hex="ab", kind=None)
    frames = fe.select_frames(conn, 68796, START, END, ("hdlc",))
    text, written, skipped = fe.export_text(frames, fe.FORMAT_CSV)
    assert (written, skipped) == (2, 0)
    rows = list(csv.reader(io.StringIO(text)))
    assert tuple(rows[0]) == fe.CSV_HEADER
    assert rows[1] == ["2026-11-23 06:28:25.062", "68796", "", "hdlc", "-12.5", "1", "2CC0"]
    assert rows[2][5] == "0" and rows[2][6] == "AB"


def test_kiss_text_one_escaped_line_per_frame_and_skips_payload_only() -> None:
    conn = _conn()
    _add(conn, "2026-11-23T06:00:00+00:00", "2CC0DB")
    _add(conn, "2026-11-23T06:01:00+00:00", None, raw_hex="ab", kind=None)
    frames = fe.select_frames(conn, 68796, START, END, ("hdlc",))
    text, written, skipped = fe.export_text(frames, fe.FORMAT_KISS)
    assert text == "C0 00 2C DB DC DB DD C0\n"
    assert (written, skipped) == (1, 1)


def test_bad_hex_frame_is_left_out_of_binary_formats() -> None:
    frame = fe.LoggedFrame(START, 1, "", "hdlc", None, "XYZ", True)
    assert fe.export_text([frame], fe.FORMAT_KISS) == ("", 0, 1)


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        fe.export_text([], "bogus")


def test_satnogs_text_is_read_back_by_the_csv_import(tmp_path: Path) -> None:
    conn = _conn()
    _add(conn, "2026-11-23T06:28:25+00:00", "2CC0DB01")
    _add(conn, "2026-11-23T06:29:25+00:00", "FF00")
    frames = fe.select_frames(conn, 68796, START, END, ("hdlc",))
    text, written, skipped = fe.export_text(frames, fe.FORMAT_SATNOGS)
    assert (written, skipped) == (2, 0)
    assert text.splitlines()[0] == "2026-11-23 06:28:25|2CC0DB01||"
    path = tmp_path / "frames.csv"
    path.write_text(text, encoding="utf-8")
    lines, read = read_frames(path)
    assert lines == 2
    assert [f.frame for f in read] == [bytes.fromhex("2CC0DB01"), bytes.fromhex("FF00")]
    assert parse_line(text.splitlines()[0]) is not None


def test_empty_selection_writes_nothing() -> None:
    assert fe.export_text([], fe.FORMAT_KISS) == ("", 0, 0)
    text, written, _skipped = fe.export_text([], fe.FORMAT_CSV)
    assert written == 0 and text.splitlines() == [",".join(fe.CSV_HEADER)]


def test_default_filename() -> None:
    when = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
    assert (
        fe.default_filename(68796, "ARICA-2", fe.FORMAT_CSV, when) == "68796_ARICA-2_20261123.csv"
    )
    assert fe.default_filename(1, "A B/c", fe.FORMAT_KISS, when) == "1_A_B_c_20261123.txt"
    assert fe.default_filename(1, "", fe.FORMAT_SATNOGS, when) == "1_20261123.txt"


def test_dialog_range_includes_the_end_day(qtbot: QtBot) -> None:
    from PySide6.QtCore import QDate

    from ui.telemetry_export_dialog import TelemetryExportDialog

    dialog = TelemetryExportDialog("ARICA-2 (68796)")
    qtbot.addWidget(dialog)
    dialog._from_edit.setDate(QDate(2026, 11, 22))
    dialog._to_edit.setDate(QDate(2026, 11, 23))
    start, end = dialog.range_utc()
    assert start == datetime(2026, 11, 22, tzinfo=UTC)
    assert end == datetime(2026, 11, 24, tzinfo=UTC)
    assert dialog.export_format() == fe.FORMAT_CSV
    dialog._format_combo.setCurrentIndex(1)
    assert dialog.export_format() == fe.FORMAT_KISS


def test_export_button_flow_writes_a_kiss_file(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tab's Export… handler: dialog -> frames of the selected satellite -> file."""
    from PySide6.QtWidgets import QDialog, QWidget

    import ui.telemetry_export_dialog as export_dialog_mod
    import ui.telemetry_tab as tab_mod

    class _Radio(QWidget):
        from PySide6.QtCore import Signal as _Signal

        rig_connected = _Signal()
        rig_disconnected = _Signal()
        rig2_connected = _Signal()
        rig2_disconnected = _Signal()
        transmitter_changed = _Signal(object)

        def current_transmitter(self) -> None:
            return None

    class _FakeDialog:
        def __init__(self, label: str, parent: object = None) -> None:
            self.label = label

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def range_utc(self) -> tuple[datetime, datetime]:
            return START, END

        def export_format(self) -> str:
            return fe.FORMAT_KISS

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    conn.execute(
        "CREATE TABLE transmitters (uuid TEXT PRIMARY KEY, norad_cat_id INTEGER,"
        " description TEXT, mode TEXT, baud INTEGER, alive INTEGER)"
    )
    conn.execute("INSERT INTO satellites VALUES (68796, 'ARICA-2', 0)")
    conn.execute(
        "INSERT INTO transmitters VALUES ('u1', 68796, 'Mode U - GMSK4k8 - AX.25', 'GMSK', 4800, 1)"
    )
    tab = tab_mod.TelemetryTab(conn, _Radio())
    qtbot.addWidget(tab)
    _add(conn, "2026-11-23T06:28:25+00:00", "2CC0")
    _add(conn, "2026-11-23T06:29:25+00:00", None, raw_hex="ab", kind=None)
    conn.commit()

    target = tmp_path / "out.txt"
    monkeypatch.setattr(export_dialog_mod, "TelemetryExportDialog", _FakeDialog)
    monkeypatch.setattr(tab_mod.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert tab._active_norad() == 68796
    tab._on_export_frames()

    assert target.read_text(encoding="utf-8") == "C0 00 2C DB DC C0\n"
    assert "Exported 1 frame(s)." in tab._lbl_status.text()
    assert "1 left out" in tab._lbl_status.text()
