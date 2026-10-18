"""The Telemetry tab's HADES FSK mode with HADES-SA (800 baud) next to UNNE-1B."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import ui.telemetry_tab as telemetry_tab_mod
from comms.telemetry.hades_fsk import HadesFrame
from comms.telemetry.hades_fsk_sdr import HADES_SA_NORAD, make_stream
from comms.telemetry.hades_sa_fsk import HadesSaFskStream
from ui.telemetry_tab import TelemetryTab

STATUS = bytes.fromhex(
    "33868b0901848b0901010044470035001bff00008400ffffffffff006438001225000045780043"
)


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


class _FakeDevice:
    sample_rate = 250_000


class _FakePipeline:
    def __init__(self) -> None:
        self._device = _FakeDevice()
        self.subscribers: list[Any] = []

    def subscribe(self, fn: Any) -> None:
        self.subscribers.append(fn)

    def unsubscribe(self, fn: Any) -> None:
        self.subscribers.remove(fn)


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
    c.execute("INSERT INTO satellites VALUES (68446, 'HADES-SA', 0)")
    c.execute("INSERT INTO satellites VALUES (98205, 'UNNE-1B', 0)")
    for uuid, norad, desc, mode, baud in (
        ("a", 68446, "SSDV", "SSDV", 800),
        ("b", 68446, "FSK 800 bps 1600 Hz separation", "GENESIS FSK", 800),
        ("c", 68446, "FSK 200 bps 1125 Hz separation", "GENESIS FSK", 200),
        ("d", 98205, "Mode U - 200bps FSK TLM", "FSK", 200),
    ):
        c.execute(
            "INSERT INTO transmitters VALUES (?, ?, ?, ?, ?, 1)", (uuid, norad, desc, mode, baud)
        )
    return c


def _tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def _select_sat(tab: TelemetryTab, norad: int) -> None:
    tab._combo_mode.setCurrentIndex(tab._combo_mode.findText(telemetry_tab_mod._MODE_HADES))
    combo = tab._combo_hades_sat
    combo.setCurrentIndex(combo.findData(norad))


def _frame() -> HadesFrame:
    return HadesFrame(0, 0.0, 300.0, 14.6, 3, 3, STATUS, False)


def test_combo_lists_hades_sa_and_unne_1b(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    combo = _tab(qtbot, conn)._combo_hades_sat
    assert sorted(combo.itemData(i) for i in range(combo.count())) == [68446, 98205]


def test_stream_choice_follows_the_satellite() -> None:
    assert isinstance(make_stream(250_000, HADES_SA_NORAD), HadesSaFskStream)
    assert not isinstance(make_stream(250_000, 98205), HadesSaFskStream)


def test_start_for_hades_sa_runs_the_800_baud_demodulator(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    pipeline = _FakePipeline()
    tab._sdr_pipeline = pipeline
    tab._on_start()
    try:
        assert tab._hades_demod is not None
        assert isinstance(tab._hades_demod._stream, HadesSaFskStream)
        assert tab._hades_norad == 68446
        assert "800 baud" in tab._lbl_status.text()
    finally:
        tab._on_stop()
    assert pipeline.subscribers == []


def test_start_for_unne_1b_still_runs_the_200_baud_demodulator(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 98205)
    tab._sdr_pipeline = _FakePipeline()
    tab._on_start()
    try:
        assert tab._hades_demod is not None
        assert not isinstance(tab._hades_demod._stream, HadesSaFskStream)
        assert "200 baud" in tab._lbl_status.text()
    finally:
        tab._on_stop()


def test_a_hades_sa_status_frame_is_shown_logged_and_decoded(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    tab._hades_norad = 68446
    tab._on_hades_frame(_frame(), 1.0)
    assert tab._table.rowCount() == 1
    assert tab._table.item(0, 3).text().startswith("[Status]")
    assert tab._table.item(0, 2).text() == "HADES-SA"
    row = conn.execute("SELECT kind, norad_cat_id, frame_hex FROM telemetry_log").fetchone()
    assert (row["kind"], row["norad_cat_id"], row["frame_hex"]) == ("hades", 68446, STATUS.hex())


def test_decoded_fields_tabs_are_hades_sas_and_fill_from_a_frame(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    tab._rebuild_decode_tabs(68446)
    # status (3) and BBS data (15), no SSDV (10) and no UNNE-1B voice
    assert sorted(tab._decode_tables, key=int) == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "8",
        "9",
        "12",
        "14",
        "15",
    ]
    tab._hades_norad = 68446
    tab._on_hades_frame(_frame(), 1.0)
    table = tab._decode_tables["3"]
    labels = [table.item(r, 0).text() for r in range(table.rowCount())]
    assert "Current SSDV image id" in labels


# ---- Import CSV (frames other stations sent to SatNOGS DB) ------------------------------- #

POWER = bytes.fromhex("13ae8b0901434f386560000bb16c10d3e3533d0fd201ac6d3000000000")
TEMPERATURE = bytes.fromhex("2312f60801ff636663ff6e706dff78")


def _csv(tmp_path: Any) -> Any:
    """A SatNOGS-style CSV, newest first, with copies from two stations and non-telemetry."""
    lines = [
        f"2026-10-17 10:02:00|{TEMPERATURE.hex().upper()}||KM7DOS-CN87xi",
        f"2026-10-17 10:01:00|{STATUS.hex().upper()}||KC6HAI-DM43co",
        f"2026-10-17 10:01:00|{STATUS.hex().upper()}||KM7DOS-CN87xi",  # the same, another station
        f"2026-10-17 10:00:30|{('B316' + '00' * 35).upper()}||KM7DOS-CN87xi",  # CODEC2 voice
        f"2026-10-17 10:00:20|{('A3' + '00' * 250).upper()}||KM7DOS-CN87xi",  # SSDV
        f"2026-10-17 10:00:10|{STATUS[:-1].hex().upper()}||KM7DOS-CN87xi",  # wrong length
        f"2026-10-17 10:00:05|{('3C' + '00' * 26).upper()}||KM7DOS-CN87xi",  # another satellite
        f"2026-10-17 10:00:00|{POWER.hex().upper()}||KM7DOS-CN87xi",
        "not a frame line",
    ]
    path = tmp_path / "frames.csv"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_footer_has_import_before_clear_and_no_export(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    from PySide6.QtWidgets import QPushButton

    tab = _tab(qtbot, conn)
    tab.resize(1100, 600)
    tab.show()
    assert not hasattr(tab, "_btn_export")
    assert tab._btn_clear.text() == "Clear"
    assert tab._btn_import.text() == "Import CSV…"
    assert tab._btn_import.geometry().x() < tab._btn_clear.geometry().x()
    assert not [b for b in tab.findChildren(QPushButton) if "Export" in b.text()]


def test_import_adds_the_telemetry_oldest_first_without_copies(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Any
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    frames, added = tab.import_csv_file(_csv(tmp_path), 68446, telemetry_tab_mod._MODE_HADES)
    assert frames == 8 and added == 3  # power, status, temperature
    texts = [tab._table.item(r, 3).text().split("]")[0] + "]" for r in range(3)]
    assert texts == ["[Power]", "[Status]", "[Temperature]"]
    times = [tab._table.item(r, 0).text() for r in range(3)]
    assert times == ["2026-10-17 10:00:00", "2026-10-17 10:01:00", "2026-10-17 10:02:00"]
    assert "kHz" not in tab._table.item(0, 3).text()  # a CSV frame has no carrier offset
    assert tab._table.item(1, 2).text() == "HADES-SA"


def test_import_is_logged_but_never_offered_for_upload(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ui.telemetry_tab as mod

    def _no_upload() -> None:
        raise AssertionError("a frame from SatNOGS DB must not be sent back")

    monkeypatch.setattr(mod, "get_satnogs_uploader", _no_upload)
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    tab.import_csv_file(_csv(tmp_path), 68446, mod._MODE_HADES)
    rows = conn.execute(
        "SELECT kind, norad_cat_id, satnogs_uploaded_at FROM telemetry_log"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["kind"] == "hades" and r["norad_cat_id"] == 68446 for r in rows)
    assert all(r["satnogs_uploaded_at"] for r in rows)  # marked as already public
    # "Load saved" brings them back, and they are not sendable either
    tab._on_clear()
    tab._on_load_saved()
    assert tab._table.rowCount() == 3


def test_importing_the_same_csv_twice_adds_nothing(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Any
) -> None:
    tab = _tab(qtbot, conn)
    _select_sat(tab, 68446)
    path = _csv(tmp_path)
    tab.import_csv_file(path, 68446, telemetry_tab_mod._MODE_HADES)
    _frames, added = tab.import_csv_file(path, 68446, telemetry_tab_mod._MODE_HADES)
    assert added == 0
    assert tab._table.rowCount() == 3
    assert conn.execute("SELECT COUNT(*) FROM telemetry_log").fetchone()[0] == 3


def test_import_without_a_satellite_or_in_other_modes_says_so(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    tab._combo_hades_sat.clear()
    tab._on_import_csv()
    assert "satellite" in tab._lbl_status.text().lower()


def _ax25_frame(src: str, info: bytes) -> bytes:
    def addr(call: str, last: bool) -> bytes:
        body = bytes((ord(c) << 1) for c in call.ljust(6))
        return body + bytes([0x60 | (1 if last else 0)])

    return addr("CQ", False) + addr(src, True) + b"\x03\xf0" + info


def test_import_in_ax25_mode_uses_the_times_of_the_file_and_never_uploads(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ui.telemetry_tab as mod

    monkeypatch.setattr(
        mod,
        "get_satnogs_uploader",
        lambda: (_ for _ in ()).throw(AssertionError("must not upload an imported frame")),
    )
    tab = _tab(qtbot, conn)
    tab.set_satellite(68446, "HADES-SA")
    raw = _ax25_frame("N0CALL", b"hello")
    path = tmp_path / "ax.csv"
    path.write_text(
        f"2026-10-17 09:00:00|{raw.hex().upper()}||KM7DOS-CN87xi\n"
        f"2026-10-17 09:00:00|{raw.hex().upper()}||KC6HAI-DM43co\n"
    )
    frames, added = tab.import_csv_file(path, 68446, mod._MODE_AFSK)
    assert (frames, added) == (2, 1)
    assert tab._table.item(0, 0).text() == "2026-10-17 09:00:00"
    row = conn.execute("SELECT kind, satnogs_uploaded_at FROM telemetry_log").fetchone()
    assert row["kind"] == "ax25" and row["satnogs_uploaded_at"]
    # a second import of the same file adds nothing
    assert tab.import_csv_file(path, 68446, mod._MODE_AFSK)[1] == 0
