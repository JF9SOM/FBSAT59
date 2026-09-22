"""Telemetry tab, "CW TLM" mode: Morse-coded hexadecimal housekeeping frames.

The CW Decoder tab decodes the Morse and announces finished text blocks; the
Telemetry tab turns the blocks that are valid frames (ARICA-2's HK1/HK2/HK3)
into received frames and Decoded Fields. These tests drive the tab with those
blocks directly, so they need no CW model, SDR or scipy.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from ui.telemetry_tab import _MODE_CW, TelemetryTab

HK1 = "2FFE8594EB880124"
HK3 = "00D7C2A8D6B8EA"
END = datetime(2026, 9, 20, 7, 1, 48, tzinfo=UTC)
START = datetime(2026, 9, 20, 7, 1, 31, tzinfo=UTC)


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


class _FakeCwTab(QObject):
    frame_block_ready = Signal(str, object, object)


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
    return c


def _add(
    conn: sqlite3.Connection, norad: int, name: str, description: str, mode: str = "CW"
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO satellites (norad_cat_id, name, is_hidden) VALUES (?, ?, 0)",
        (norad, name),
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES (?, ?, ?, ?, NULL, 1)",
        (f"{norad}-{description}", norad, description, mode),
    )


@pytest.fixture
def cw_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    _add(conn, 41847, "CAS-2T", "CW Telemetry")
    _add(conn, 68796, "ARICA-2", "Mode U - CW")
    _add(conn, 68796, "ARICA-2", "Mode U - GMSK4k8 - AX.25", mode="GMSK")
    _add(conn, 99999, "LINEAR", "Linear transponder", mode="USB")
    return conn


def _make_tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    return tab


def _combo_items(tab: TelemetryTab) -> list[tuple[int, str]]:
    combo = tab._combo_cw_sat
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def _select_cw_mode(tab: TelemetryTab) -> None:
    tab._combo_mode.setCurrentIndex(tab._combo_mode.findText(_MODE_CW))


def _running_tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    """A tab in CW TLM mode with ARICA-2 selected and started."""
    tab = _make_tab(qtbot, conn)
    _select_cw_mode(tab)
    tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(68796))
    tab._on_start()
    return tab


def _decode_value(tab: TelemetryTab, frame: str, field: str) -> str:
    table = tab._decode_tables[frame]
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and item.text() == field:
            value = table.item(row, 1)
            return value.text() if value is not None else ""
    raise AssertionError(f"no row {field!r} in {frame}")


class TestSatelliteCombo:
    def test_lists_the_satellites_the_db_search_finds(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        norads = [n for n, _ in _combo_items(tab)]
        # ARICA-2 via its CW frame definition ("Mode U - CW" has no TLM in it),
        # CAS-2T via "CW Telemetry"; the linear transponder and the GMSK entry do not count.
        assert set(norads) == {41847, 68796}

    def test_satellites_that_can_be_decoded_come_first(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        assert [n for n, _ in _combo_items(tab)] == [68796, 41847]

    def test_hidden_and_dead_transmitters_are_left_out(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        cw_conn.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = 41847")
        tab = _make_tab(qtbot, cw_conn)
        assert [n for n, _ in _combo_items(tab)] == [68796]


class TestModeSwitch:
    def test_cw_mode_shows_its_own_combo_and_hides_the_direwolf_controls(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        tab.show()
        _select_cw_mode(tab)
        assert tab._combo_cw_sat.isVisible()
        assert not tab._combo_afsk_sat.isVisible()
        assert not tab._combo_gr_sat.isVisible()
        assert not tab._baud_combo.isVisible()
        assert not tab._btn_backend_log.isVisible()

    def test_switching_to_cw_mode_selects_the_satellite(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        with qtbot.waitSignal(tab.satellite_selected) as blocker:
            _select_cw_mode(tab)
        assert blocker.args == [68796, "cw_tlm"]

    def test_changing_the_cw_satellite_reports_it(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        _select_cw_mode(tab)
        with qtbot.waitSignal(tab.satellite_selected) as blocker:
            tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(41847))
        assert blocker.args == [41847, "cw_tlm"]


class TestStartStop:
    def test_start_asks_for_the_cw_decoder_and_stop_releases_it(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        _select_cw_mode(tab)
        tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(68796))

        with qtbot.waitSignal(tab.cw_tlm_start_requested):
            tab._on_start()
        assert tab._cw_tlm_norad == 68796
        assert not tab._btn_start.isEnabled()
        assert tab._btn_stop.isEnabled()
        assert tab._decode_tabs_norad == 68796  # HK1..HK3 tabs are ready

        with qtbot.waitSignal(tab.cw_tlm_stop_requested):
            tab._on_stop()
        assert tab._cw_tlm_norad is None
        assert tab._btn_start.isEnabled()

    def test_a_satellite_without_a_frame_format_does_not_start(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        _select_cw_mode(tab)
        tab._combo_cw_sat.setCurrentIndex(tab._combo_cw_sat.findData(41847))
        requested: list[bool] = []
        tab.cw_tlm_start_requested.connect(lambda: requested.append(True))

        tab._on_start()

        assert requested == []
        assert tab._cw_tlm_norad is None
        assert tab._btn_start.isEnabled()
        assert "No CW telemetry frame format is defined" in tab._lbl_status.text()

    def test_stopping_when_not_running_sends_nothing(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        stops: list[bool] = []
        tab.cw_tlm_stop_requested.connect(lambda: stops.append(True))
        tab._on_stop()
        assert stops == []


class TestReceivedBlocks:
    def test_a_valid_frame_becomes_a_row_decoded_fields_and_a_log_entry(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)

        tab._on_cw_block(HK1, START, END)

        assert tab._table.rowCount() == 1
        item = lambda col: tab._table.item(0, col).text()  # noqa: E731
        assert (
            item(0) == "2026-09-20 07:01:31"
        )  # the block's START (transmission start), UTC, with the date
        assert item(1) == "JS1YSD"
        assert item(2) == "ARICA-2"
        assert item(3) == f"[HK1] {HK1}"
        assert tab._frame_count == 1
        assert _decode_value(tab, "HK1", "SBD error") == "Abnormal"
        assert _decode_value(tab, "HK1", "Angular Velocity X") == "-2.591 ≦ Gx ＜ -2.061"
        row = cw_conn.execute(
            "SELECT received_at, norad_cat_id, raw_hex FROM telemetry_log"
        ).fetchone()
        assert row["received_at"].startswith("2026-09-20T07:01:31")
        assert (row["norad_cat_id"], row["raw_hex"]) == (68796, HK1)

    def test_hk3_updates_its_own_tab(self, qtbot: QtBot, cw_conn: sqlite3.Connection) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK3, START, END)
        assert _decode_value(tab, "HK3", "Battery Voltage").startswith("8.2009")
        assert _decode_value(tab, "HK3", "Power Flow Status") == "charge"

    def test_a_frame_that_fails_the_plausibility_checks_is_greyed_out_and_not_used(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        bits = list(f"{int(HK1, 16):064b}")
        bits[54] = "1"  # inside the always-zero not_used field
        bad = f"{int(''.join(bits), 2):016X}"

        tab._on_cw_block(bad, START, END)

        assert tab._table.rowCount() == 1
        assert tab._table.item(0, 3).text().startswith(f"[?] HK1 {bad}")
        assert tab._frame_count == 0  # not counted
        assert _decode_value(tab, "HK1", "SBD error") == "—"  # Decoded Fields untouched
        assert cw_conn.execute("SELECT COUNT(*) FROM telemetry_log").fetchone()[0] == 0

    def test_a_misread_frame_length_is_listed_as_a_candidate_only(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1[:-1], START, END)  # a dropped digit
        assert tab._table.item(0, 3).text().startswith(f"[?] {HK1[:-1]}")
        assert tab._frame_count == 0
        assert cw_conn.execute("SELECT COUNT(*) FROM telemetry_log").fetchone()[0] == 0

    def test_the_id_text_and_noise_are_ignored(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block("DE JS1YSD ARICA2", START, END)
        tab._on_cw_block("TTNNEE", START, END)
        assert tab._table.rowCount() == 0

    def test_blocks_are_ignored_when_not_running(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        assert tab._table.rowCount() == 0


class TestCwTabConnection:
    def test_blocks_from_the_attached_cw_tab_arrive(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        cw = _FakeCwTab()
        tab.attach_cw_tab(cw)

        cw.frame_block_ready.emit(HK1, START, END)

        assert tab._table.rowCount() == 1

    def test_attaching_another_cw_tab_replaces_the_first(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        first, second = _FakeCwTab(), _FakeCwTab()
        tab.attach_cw_tab(first)
        tab.attach_cw_tab(first)  # same tab again: still one connection
        tab.attach_cw_tab(second)

        first.frame_block_ready.emit(HK1, START, END)
        assert tab._table.rowCount() == 0
        second.frame_block_ready.emit(HK1, START, END)
        assert tab._table.rowCount() == 1


class TestMainWindowWiring:
    """MainWindow's handlers, called unbound with a stand-in for self."""

    def test_start_opens_the_cw_tab_attaches_and_starts_it(self) -> None:
        from ui.main_window import MainWindow

        cw_tab, telemetry_tab = MagicMock(), MagicMock()
        fake: Any = MagicMock()
        fake._comms_tab_keys = {telemetry_tab: "telemetry", cw_tab: "cw"}
        fake._find_comms_tab = lambda key: MainWindow._find_comms_tab(fake, key)

        MainWindow._on_telemetry_cw_tlm_start(fake, telemetry_tab)

        fake._on_open_cw.assert_called_once_with()
        telemetry_tab.attach_cw_tab.assert_called_once_with(cw_tab)
        cw_tab.start_decoding.assert_called_once_with()
        # ... and the Telemetry tab, not the CW tab, is left in front.
        fake._tab_widget.setCurrentWidget.assert_called_with(telemetry_tab)

    def test_stop_stops_the_cw_tab(self) -> None:
        from ui.main_window import MainWindow

        cw_tab = MagicMock()
        fake: Any = MagicMock()
        fake._comms_tab_keys = {cw_tab: "cw"}
        fake._find_comms_tab = lambda key: MainWindow._find_comms_tab(fake, key)

        MainWindow._on_telemetry_cw_tlm_stop(fake)

        cw_tab.stop_decoding.assert_called_once_with()

    def test_start_without_an_openable_cw_tab_does_not_crash(self) -> None:
        from ui.main_window import MainWindow

        telemetry_tab = MagicMock()
        fake: Any = MagicMock()
        fake._comms_tab_keys = {telemetry_tab: "telemetry"}
        fake._find_comms_tab = lambda key: MainWindow._find_comms_tab(fake, key)

        MainWindow._on_telemetry_cw_tlm_start(fake, telemetry_tab)

        telemetry_tab.attach_cw_tab.assert_not_called()

    def test_choosing_a_cw_tlm_satellite_keeps_the_telemetry_tab_in_front(self) -> None:
        from ui.main_window import MainWindow

        fake: Any = MagicMock()
        current = object()
        fake._tab_widget.currentWidget.return_value = current

        MainWindow._on_telemetry_satellite_requested(fake, 68796, "cw_tlm")

        fake._select_telemetry_satellite.assert_called_once_with(68796, "cw_tlm")
        fake._tab_widget.setCurrentWidget.assert_called_once_with(current)

    def test_other_modes_do_not_touch_the_current_tab(self) -> None:
        from ui.main_window import MainWindow

        fake: Any = MagicMock()
        MainWindow._on_telemetry_satellite_requested(fake, 25544, "afsk")
        fake._select_telemetry_satellite.assert_called_once_with(25544, "afsk")
        fake._tab_widget.setCurrentWidget.assert_not_called()


# --------------------------------------------------------------------------- #
# Date in the table / CSV, the clock of a played-back recording, SatNOGS upload
# --------------------------------------------------------------------------- #


class _FakeUploader:
    """Stands in for the SatNOGS uploader: records what would be sent, and lets a
    test deliver SatNOGS's answer."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, int, datetime, bool]] = []
        self.callbacks: list[Any] = []

    def submit(
        self,
        conn: Any,
        raw: bytes,
        norad: int | None,
        received_at: datetime,
        force: bool = False,
        on_result: Any = None,
    ) -> bool:
        assert norad is not None
        self.sent.append((raw, norad, received_at, force))
        self.callbacks.append(on_result)
        return True


@pytest.fixture
def uploader(monkeypatch: pytest.MonkeyPatch) -> _FakeUploader:
    fake = _FakeUploader()
    monkeypatch.setattr("ui.telemetry_tab.get_satnogs_uploader", lambda: fake)
    return fake


def _configure_upload(conn: sqlite3.Connection, *, enabled: bool = True) -> None:
    import json

    from comms.telemetry.satnogs_uploader import save_satnogs_upload_settings

    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('callsign', 'jf9som')")
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('observer_location', ?)",
        (json.dumps({"latitude_deg": 36.1, "longitude_deg": 136.4}),),
    )
    save_satnogs_upload_settings(conn, {"enabled": enabled, "api_key": "KEY"})
    conn.commit()


class _CwTabWithTime(_FakeCwTab):
    def __init__(self, reliable: bool) -> None:
        super().__init__()
        self._reliable = reliable

    def signal_time_reliable(self) -> bool:
        return self._reliable


def _running_with(qtbot: QtBot, conn: sqlite3.Connection, reliable: bool = True) -> TelemetryTab:
    tab = _running_tab(qtbot, conn)
    tab.attach_cw_tab(_CwTabWithTime(reliable))
    return tab


class TestDateInTheTable:
    def test_the_time_column_shows_the_date_too(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        assert tab._table.item(0, 0).text() == "2026-09-20 07:01:31"

    def test_the_csv_export_has_the_date(
        self,
        qtbot: QtBot,
        cw_conn: sqlite3.Connection,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        target = tmp_path / "out.csv"
        monkeypatch.setattr(
            "ui.telemetry_tab.QFileDialog.getSaveFileName", lambda *a, **k: (str(target), "")
        )

        tab._on_export_csv()

        assert "2026-09-20 07:01:31" in target.read_text(encoding="utf-8")


class TestClockOfARecording:
    def _replaying(self, tab: TelemetryTab, confirmed: bool) -> None:
        pipeline = MagicMock()
        pipeline._device.start_time_utc = datetime(2026, 9, 20, 6, 55, 51, tzinfo=UTC)
        pipeline._device.position_s = 357.5
        pipeline._device.start_time_confirmed = confirmed
        tab._sdr_pipeline = pipeline

    def test_frames_are_timed_by_the_recording_not_by_now(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        self._replaying(tab, confirmed=True)
        assert tab._frame_time() == (datetime(2026, 9, 20, 7, 1, 48, 500000, tzinfo=UTC), True)

    def test_a_live_input_uses_the_wall_clock(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        when, reliable = tab._frame_time()
        assert reliable
        assert abs((when - datetime.now(UTC)).total_seconds()) < 5.0

    def test_an_ax25_frame_is_uploaded_with_the_recording_time(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        self._replaying(tab, confirmed=True)
        tab._submit_raw_frame(b"\x01\x02", 25544, *tab._frame_time())
        ((raw, norad, when, forced),) = uploader.sent
        assert (raw, norad, forced) == (b"\x01\x02", 25544, False)
        assert when == datetime(2026, 9, 20, 7, 1, 48, 500000, tzinfo=UTC)

    def test_a_placeholder_start_time_is_never_uploaded(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn)
        tab = _make_tab(qtbot, cw_conn)
        self._replaying(tab, confirmed=False)

        tab._submit_raw_frame(b"\x01\x02", 25544, *tab._frame_time())

        assert uploader.sent == []
        assert "start time" in tab._lbl_status.text()


class TestAutomaticUpload:
    def test_a_frame_is_sent_only_once_a_repeat_confirms_it(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn)
        tab = _running_with(qtbot, cw_conn)

        tab._on_cw_block(HK1, START, END)
        assert uploader.sent == []  # one reading: could be a mis-read digit

        tab._on_cw_block(HK1, START + timedelta(seconds=125), END + timedelta(seconds=125))
        assert [s[0] for s in uploader.sent] == [b"arica-2\x01" + bytes.fromhex(HK1)] * 2
        assert [s[2] for s in uploader.sent] == [START, START + timedelta(seconds=125)]
        assert "2 queued" in tab._lbl_status.text()

    def test_nothing_is_sent_with_the_switch_off(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn, enabled=False)
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        tab._on_cw_block(HK1, START + timedelta(seconds=125), END + timedelta(seconds=125))
        assert uploader.sent == []

    def test_a_placeholder_time_is_logged_as_unreliable_and_not_sent(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn)
        tab = _running_with(qtbot, cw_conn, reliable=False)
        tab._on_cw_block(HK1, START, END)
        tab._on_cw_block(HK1, START + timedelta(seconds=125), END + timedelta(seconds=125))

        assert uploader.sent == []
        rows = cw_conn.execute("SELECT time_reliable FROM telemetry_log").fetchall()
        assert [r[0] for r in rows] == [0, 0]
        assert "start time" in tab._lbl_status.text()

    def test_the_log_id_is_kept_on_the_table_row(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _running_tab(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        log_id = cw_conn.execute("SELECT id FROM telemetry_log").fetchone()[0]
        from PySide6.QtCore import Qt

        assert tab._table.item(0, 0).data(Qt.ItemDataRole.UserRole) == log_id


class TestManualSend:
    def test_send_selected_sends_a_single_reading_even_with_the_switch_off(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn, enabled=False)
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        tab._table.selectRow(0)

        tab._on_send_selected()

        ((raw, norad, when, forced),) = uploader.sent
        assert raw == b"arica-2\x01" + bytes.fromhex(HK1)
        assert (norad, when, forced) == (68796, START, True)
        assert "1 queued" in tab._lbl_status.text()

    def test_send_selected_needs_a_selection(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn)
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        tab._on_send_selected()
        assert uploader.sent == []

    def test_send_selected_reports_a_missing_api_key(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        from comms.telemetry.satnogs_uploader import save_satnogs_upload_settings

        _configure_upload(cw_conn)
        save_satnogs_upload_settings(cw_conn, {"enabled": True, "api_key": ""})
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        tab._table.selectRow(0)

        tab._on_send_selected()

        assert uploader.sent == []
        assert "API key" in tab._lbl_status.text()

    def test_send_unsent_asks_and_sends_the_confirmed_frames(
        self,
        qtbot: QtBot,
        cw_conn: sqlite3.Connection,
        uploader: _FakeUploader,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Received while the switch was off, so nothing was sent then.
        _configure_upload(cw_conn, enabled=False)
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        tab._on_cw_block(HK1, START + timedelta(seconds=125), END + timedelta(seconds=125))
        tab._on_cw_block(
            HK3, START + timedelta(seconds=300), END + timedelta(seconds=300)
        )  # single reading
        assert uploader.sent == []
        asked: list[str] = []

        def yes(_parent: object, _title: str, text: str) -> Any:
            from PySide6.QtWidgets import QMessageBox

            asked.append(text)
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr("ui.telemetry_tab.QMessageBox.question", yes)

        tab._on_send_unsent()

        assert "2 logged frame(s) (1 different)" in asked[0]
        assert len(uploader.sent) == 2  # HK1 twice; the single HK3 reading stays

    def test_send_unsent_does_nothing_when_the_user_says_no(
        self,
        qtbot: QtBot,
        cw_conn: sqlite3.Connection,
        uploader: _FakeUploader,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        _configure_upload(cw_conn, enabled=False)
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)
        tab._on_cw_block(HK1, START + timedelta(seconds=125), END + timedelta(seconds=125))
        monkeypatch.setattr(
            "ui.telemetry_tab.QMessageBox.question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        tab._on_send_unsent()
        assert uploader.sent == []

    def test_send_unsent_with_nothing_ready_says_so(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        _configure_upload(cw_conn)
        tab = _running_with(qtbot, cw_conn)
        tab._on_cw_block(HK1, START, END)  # a single reading
        tab._on_send_unsent()
        assert uploader.sent == []
        assert "Nothing ready" in tab._lbl_status.text()

    def test_the_send_buttons_exist_only_in_cw_tlm_mode(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        tab.show()
        assert not tab._btn_satnogs_send.isVisible()
        _select_cw_mode(tab)
        assert tab._btn_satnogs_send.isVisible()
        assert tab._btn_satnogs_send_unsent.isVisible()


class TestSatnogsAnswer:
    """What the user sees, and what is remembered, once SatNOGS has answered."""

    def _sent_tab(
        self, qtbot: QtBot, conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> TelemetryTab:
        _configure_upload(conn, enabled=False)
        tab = _running_with(qtbot, conn)
        tab._on_cw_block(HK1, START, END)
        tab._table.selectRow(0)
        tab._on_send_selected()
        assert len(uploader.sent) == 1
        return tab

    @staticmethod
    def _marked(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT satnogs_uploaded_at FROM telemetry_log").fetchone()
        return bool(row[0])

    def test_queued_is_not_sent(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        tab = self._sent_tab(qtbot, cw_conn, uploader)
        assert "1 queued" in tab._lbl_status.text()
        assert not self._marked(cw_conn)  # nothing is marked before SatNOGS answered

    def test_an_accepted_frame_is_marked_and_confirmed_on_screen(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        tab = self._sent_tab(qtbot, cw_conn, uploader)

        uploader.callbacks[0](True, 201, "ok")  # from the uploader's thread
        qtbot.waitUntil(lambda: self._marked(cw_conn), timeout=2000)

        assert "accepted" in tab._lbl_status.text()
        assert tab._upload_pending == set()

    def test_a_rejected_key_is_shown_and_the_frame_stays_unsent(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        tab = self._sent_tab(qtbot, cw_conn, uploader)

        uploader.callbacks[0](False, 401, '{"detail":"Invalid token."}')
        qtbot.waitUntil(lambda: "401" in tab._lbl_status.text(), timeout=2000)

        text = tab._lbl_status.text()
        assert "Invalid token." in text
        assert "API key" in text  # 401 means the key is wrong: say where to fix it
        assert not self._marked(cw_conn)
        assert tab._upload_pending == set()  # it can be sent again

        tab._on_send_selected()
        assert len(uploader.sent) == 2

    def test_a_network_error_is_shown(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        tab = self._sent_tab(qtbot, cw_conn, uploader)
        uploader.callbacks[0](False, 0, "no route to host")
        qtbot.waitUntil(lambda: "no route to host" in tab._lbl_status.text(), timeout=2000)
        assert not self._marked(cw_conn)

    def test_the_answer_of_an_ax25_upload_shows_failures_only_once(
        self, qtbot: QtBot, cw_conn: sqlite3.Connection, uploader: _FakeUploader
    ) -> None:
        tab = _make_tab(qtbot, cw_conn)
        tab._submit_raw_frame(b"\x01", 25544, START, True)
        cb = uploader.callbacks[0]

        cb(True, 201, "ok")  # success of an AX.25 frame: silent
        assert "accepted" not in tab._lbl_status.text()

        cb(False, 401, '{"detail":"Invalid token."}')
        qtbot.waitUntil(lambda: "401" in tab._lbl_status.text(), timeout=2000)
        tab._lbl_status.setText("cleared")
        cb(False, 401, '{"detail":"Invalid token."}')
        qtbot.wait(100)
        assert tab._lbl_status.text() == "cleared"  # not repeated for every frame
