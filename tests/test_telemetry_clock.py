"""Telemetry tab: frames from the Direwolf (AX.25) and gr-satellites modes are timed
by the signal clock -- the wall clock live, a played-back recording's own clock
(start time + playback position) for a replay -- and are only published with a
time that can be trusted.

Before, these modes stamped every frame -- and every SatNOGS upload -- with now(), so
replaying an old recording published its frames with today's time.
"""

from __future__ import annotations

import sqlite3
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import ui.telemetry_tab as telemetry_tab_mod
from comms.telemetry.satnogs_uploader import save_satnogs_upload_settings
from ui.telemetry_tab import TelemetryTab

START = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
RAW = bytes.fromhex("9c86aa8ea662e0a08a82a49886e103f000112233")


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int | None, datetime]] = []

    def submit(
        self,
        conn: Any,
        raw: bytes,
        norad: int | None,
        received_at: datetime,
        force: bool = False,
        on_result: Any = None,
    ) -> bool:
        self.calls.append((raw, norad, received_at))
        return True


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


@pytest.fixture
def uploader(monkeypatch: pytest.MonkeyPatch) -> _RecordingUploader:
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    return rec


def _make_tab(
    qtbot: QtBot, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> TelemetryTab:
    monkeypatch.setattr(
        telemetry_tab_mod,
        "decode_ax25",
        lambda raw: types.SimpleNamespace(src="JY1SAT", payload=b"\x00\x11\x22"),
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    monkeypatch.setattr(tab, "_callsign_to_norad", lambda src: 43803)
    return tab


def _replay(tab: TelemetryTab, position_s: float, confirmed: bool = True) -> MagicMock:
    pipeline = MagicMock()
    pipeline._device.start_time_utc = START
    pipeline._device.position_s = position_s
    pipeline._device.start_time_confirmed = confirmed
    tab._sdr_pipeline = pipeline
    return pipeline


def _row_times(tab: TelemetryTab) -> list[str]:
    return [tab._table.item(r, 0).text() for r in range(tab._table.rowCount())]


class TestAx25Replay:
    def test_a_frame_is_shown_logged_and_uploaded_with_the_recording_time(
        self,
        qtbot: QtBot,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        uploader: _RecordingUploader,
    ) -> None:
        tab = _make_tab(qtbot, conn, monkeypatch)
        _replay(tab, 4.26)

        tab._on_ax25_frame(RAW)

        assert _row_times(tab) == ["2026-09-20 12:00:04"]  # not today's date
        row = conn.execute("SELECT received_at, time_reliable FROM telemetry_log").fetchone()
        assert row["received_at"].startswith("2026-09-20T12:00:04.26")
        assert row["time_reliable"] == 1
        ((raw, norad, when),) = uploader.calls
        assert (raw, norad) == (RAW, 43803)
        assert when == datetime(2026, 9, 20, 12, 0, 4, 260000, tzinfo=UTC)

    def test_the_time_follows_the_playback_position(
        self,
        qtbot: QtBot,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        uploader: _RecordingUploader,
    ) -> None:
        tab = _make_tab(qtbot, conn, monkeypatch)
        pipeline = _replay(tab, 4.0)
        tab._on_ax25_frame(RAW)
        pipeline._device.position_s = 125.5  # the user seeked
        tab._on_ax25_frame(RAW)
        assert _row_times(tab) == ["2026-09-20 12:00:04", "2026-09-20 12:02:05"]

    def test_a_placeholder_start_time_is_logged_as_unreliable_and_never_uploaded(
        self,
        qtbot: QtBot,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        uploader: _RecordingUploader,
    ) -> None:
        save_satnogs_upload_settings(conn, {"enabled": True, "api_key": "KEY"})
        tab = _make_tab(qtbot, conn, monkeypatch)
        _replay(tab, 4.0, confirmed=False)

        tab._on_ax25_frame(RAW)
        tab._on_ax25_frame(RAW)

        assert uploader.calls == []
        rows = conn.execute("SELECT time_reliable FROM telemetry_log").fetchall()
        assert [r[0] for r in rows] == [0, 0]
        assert "start time" in tab._lbl_status.text()  # said once, not per frame

    def test_a_live_frame_uses_the_wall_clock_and_is_uploaded(
        self,
        qtbot: QtBot,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        uploader: _RecordingUploader,
    ) -> None:
        tab = _make_tab(qtbot, conn, monkeypatch)  # no recording attached

        tab._on_ax25_frame(RAW)

        ((_raw, _norad, when),) = uploader.calls
        assert abs((when - datetime.now(UTC)).total_seconds()) < 5.0


class TestGrSatellitesReplay:
    def test_a_raw_frame_is_uploaded_with_the_recording_time(
        self,
        qtbot: QtBot,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        uploader: _RecordingUploader,
    ) -> None:
        tab = _make_tab(qtbot, conn, monkeypatch)
        _replay(tab, 10.35)
        monkeypatch.setattr(type(tab._gr_backend), "started_norad", property(lambda self: 60237))

        tab._on_gr_raw_frame(RAW)

        ((raw, norad, when),) = uploader.calls
        assert (raw, norad) == (RAW, 60237)
        assert when == datetime(2026, 9, 20, 12, 0, 10, 350000, tzinfo=UTC)

    def test_a_raw_frame_with_a_placeholder_start_time_is_not_uploaded(
        self,
        qtbot: QtBot,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        uploader: _RecordingUploader,
    ) -> None:
        tab = _make_tab(qtbot, conn, monkeypatch)
        _replay(tab, 10.35, confirmed=False)
        monkeypatch.setattr(type(tab._gr_backend), "started_norad", property(lambda self: 60237))
        tab._on_gr_raw_frame(RAW)
        assert uploader.calls == []

    def test_the_text_row_shows_the_recording_time(
        self, qtbot: QtBot, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tab = _make_tab(qtbot, conn, monkeypatch)
        _replay(tab, 16.4)

        tab._on_gr_telemetry("-> Packet from 9k6 FSK downlink\nContainer:\n  x = 1")

        assert _row_times(tab) == ["2026-09-20 12:00:16"]
