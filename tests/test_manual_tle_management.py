"""Tests for removing manual TLEs and warning when they get old (2026-10-09).

A manual TLE is never overwritten by an automatic update, so without these it would stay
in place and silently age.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pytestqt.qtbot import QtBot

from data.database import SCHEMA_SQL
from data.tle_manager import MANUAL_TLE_STALE_DAYS, TLEManager

_LINE1 = "1 25544U 98067A   24001.50000000  .00016717  00000+0  10270-3 0  9994"
_LINE2 = "2 25544  51.6400 208.9163 0006828  86.9922 273.1770 15.49212693420559"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _put(db: sqlite3.Connection, norad: int, name: str, age_days: float, source: str) -> None:
    epoch = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (?, ?)", (norad, name))
    db.execute(
        "INSERT INTO tle_data (norad_cat_id, name, line1, line2, epoch, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (norad, name, _LINE1, _LINE2, epoch, source),
    )
    db.commit()


def test_list_manual_tles_only_returns_manual_ones_oldest_first(db: sqlite3.Connection) -> None:
    _put(db, 98248, "JAMX01", 3.0, "manual")
    _put(db, 25544, "ISS", 1.0, "celestrak")
    _put(db, 99999, "OLD", 20.0, "manual")
    entries = TLEManager(db).list_manual_tles()
    assert [e["norad_cat_id"] for e in entries] == [99999, 98248]
    assert entries[0]["age_days"] == pytest.approx(20.0, abs=0.01)


def test_stale_flag_follows_the_threshold(db: sqlite3.Connection) -> None:
    _put(db, 1, "FRESH", MANUAL_TLE_STALE_DAYS - 1, "manual")
    _put(db, 2, "OLD", MANUAL_TLE_STALE_DAYS + 1, "manual")
    stale = {e["norad_cat_id"]: e["stale"] for e in TLEManager(db).list_manual_tles()}
    assert stale == {1: False, 2: True}


def test_unreadable_epoch_counts_as_stale(db: sqlite3.Connection) -> None:
    _put(db, 3, "BROKEN", 1.0, "manual")
    db.execute("UPDATE tle_data SET epoch = 'garbage' WHERE norad_cat_id = 3")
    db.commit()
    (entry,) = TLEManager(db).list_manual_tles()
    assert entry["stale"] is True and entry["epoch"] is None


def test_remove_manual_tle_deletes_only_a_manual_row(db: sqlite3.Connection) -> None:
    _put(db, 98248, "JAMX01", 3.0, "manual")
    _put(db, 25544, "ISS", 1.0, "celestrak")
    mgr = TLEManager(db)
    assert mgr.remove_manual_tle(98248) is True
    assert mgr.get_tle(98248) is None
    assert mgr.remove_manual_tle(25544) is False  # an automatic TLE is never removed here
    assert mgr.get_tle(25544) is not None
    assert mgr.remove_manual_tle(98248) is False  # already gone
    # the satellite itself stays
    assert db.execute("SELECT 1 FROM satellites WHERE norad_cat_id = 98248").fetchone()


def test_a_removed_tle_is_seen_by_the_running_engine(db: sqlite3.Connection) -> None:
    import core.engine as engine_mod

    mgr = TLEManager(db)
    assert mgr.add_manual_tle(25544, "ISS", _LINE1, _LINE2)
    engine = engine_mod.SatelliteEngine(mgr, 35.0, 139.0)
    at = datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC)
    assert engine.observe(25544, at=at) is not None
    mgr.remove_manual_tle(25544)
    old = engine_mod._TLE_RECHECK_INTERVAL_S
    engine_mod._TLE_RECHECK_INTERVAL_S = 0.0
    try:
        assert engine.observe(25544, at=at) is None
    finally:
        engine_mod._TLE_RECHECK_INTERVAL_S = old


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


def test_dialog_lists_manual_tles_and_marks_old_ones(qtbot: QtBot, db: sqlite3.Connection) -> None:
    from ui.remove_manual_tle_dialog import RemoveManualTLEDialog

    _put(db, 98248, "JAMX01", 3.0, "manual")
    _put(db, 99999, "OLD", 20.0, "manual")
    dlg = RemoveManualTLEDialog(TLEManager(db))
    qtbot.addWidget(dlg)
    assert dlg._table.rowCount() == 2
    assert dlg._table.item(0, 0).text() == "OLD"
    assert "⚠" in dlg._table.item(0, 3).text()
    assert "⚠" not in dlg._table.item(1, 3).text()
    assert not dlg._remove_btn.isEnabled()  # nothing selected yet


def test_dialog_removes_the_selected_tle_after_confirmation(
    qtbot: QtBot, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from ui.remove_manual_tle_dialog import RemoveManualTLEDialog

    _put(db, 98248, "JAMX01", 3.0, "manual")
    mgr = TLEManager(db)
    dlg = RemoveManualTLEDialog(mgr)
    qtbot.addWidget(dlg)
    dlg._table.selectRow(0)
    assert dlg._remove_btn.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg._remove_btn.click()
    assert mgr.get_tle(98248) is not None and dlg.removed_norads == []

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg._remove_btn.click()
    assert mgr.get_tle(98248) is None
    assert dlg.removed_norads == [98248]
    assert dlg._table.rowCount() == 0


# ---------------------------------------------------------------------------
# Main window's stale warning (called unbound: no MainWindow is built)
# ---------------------------------------------------------------------------


def _fake_window(db: sqlite3.Connection) -> tuple[SimpleNamespace, MagicMock]:
    bar = MagicMock()
    return SimpleNamespace(_tle_manager=TLEManager(db), statusBar=lambda: bar), bar


def test_stale_warning_names_the_old_manual_tle(db: sqlite3.Connection) -> None:
    from ui.main_window import MainWindow

    _put(db, 98248, "JAMX01", 20.0, "manual")
    win, bar = _fake_window(db)
    MainWindow._warn_stale_manual_tles(win)  # type: ignore[arg-type]
    text = bar.showMessage.call_args.args[0]
    assert "JAMX01" in text and "20 d" in text


def test_no_warning_for_a_fresh_manual_tle_or_none_at_all(db: sqlite3.Connection) -> None:
    from ui.main_window import MainWindow

    win, bar = _fake_window(db)
    MainWindow._warn_stale_manual_tles(win)  # type: ignore[arg-type]
    _put(db, 98248, "JAMX01", 2.0, "manual")
    MainWindow._warn_stale_manual_tles(win)  # type: ignore[arg-type]
    bar.showMessage.assert_not_called()
