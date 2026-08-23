"""Tests for AutotrackRecordDialog's Recording checkbox persistence.

GitHub Issue #27 follow-up (2026-08-22): the Audio Record / IQ Record /
METEOR-HRPT Reception checkboxes reset to unchecked on every app restart
(nothing persisted their state). A user had "METEOR / HRPT Reception"
enabled in one session, restarted the app before a later pass, and
Autotrack correctly reported "Tracking: METEOR M2-4" at AOS but never
opened the METEOR tab -- because the checkbox had silently reverted to
off. These checkboxes are now persisted to app_settings.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from pytestqt.qtbot import QtBot

from data.database import SCHEMA_SQL
from ui.autotrack_record_dialog import AutotrackRecordDialog
from ui.main_window import MainWindow


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _no_background_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """See the identical fixture in test_main_window.py: MainWindow's
    constructor spawns several daemon threads doing real network I/O unless
    _start_scheduler() is disabled. TestMainWindowSyncsInitialListSelection
    below constructs MainWindow directly, so needs the same guard."""
    monkeypatch.setattr(MainWindow, "_start_scheduler", lambda self: None)


class TestRecordingSettingsPersistence:
    def test_defaults_to_all_unchecked_when_nothing_saved(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        assert dlg.is_audio_record_enabled() is False
        assert dlg.is_iq_record_enabled() is False
        assert dlg.is_meteor_record_enabled() is False

    def test_toggling_meteor_checkbox_persists_to_app_settings(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        dlg._meteor_rec_cb.setChecked(True)

        row = db.execute(
            "SELECT value FROM app_settings WHERE key = 'autotrack_recording_settings'"
        ).fetchone()
        assert row is not None
        saved = json.loads(row["value"])
        assert saved["meteor_record"] is True
        assert saved["audio_record"] is False
        assert saved["iq_record"] is False

    def test_new_dialog_instance_restores_saved_state(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        first = AutotrackRecordDialog(db)
        qtbot.addWidget(first)
        first._audio_rec_cb.setChecked(True)
        first._meteor_rec_cb.setChecked(True)
        # iq_record left unchecked

        second = AutotrackRecordDialog(db)
        qtbot.addWidget(second)

        assert second.is_audio_record_enabled() is True
        assert second.is_iq_record_enabled() is False
        assert second.is_meteor_record_enabled() is True

    def test_unchecking_persists_too(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        first = AutotrackRecordDialog(db)
        qtbot.addWidget(first)
        first._meteor_rec_cb.setChecked(True)
        first._meteor_rec_cb.setChecked(False)

        second = AutotrackRecordDialog(db)
        qtbot.addWidget(second)

        assert second.is_meteor_record_enabled() is False

    def test_corrupt_saved_value_is_ignored_not_raised(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('autotrack_recording_settings', ?)",
            ("not valid json",),
        )
        db.commit()

        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        assert dlg.is_meteor_record_enabled() is False

    def test_restoring_checked_state_does_not_emit_changed_signal(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        """Restoration uses blockSignals() -- MainWindow instead reads the
        already-restored state via the getters right after connecting these
        signals (see MainWindow.__init__()), so a signal firing during
        __init__() (before anything is connected) would just be silently
        lost and is not the intended sync path."""
        first = AutotrackRecordDialog(db)
        qtbot.addWidget(first)
        first._meteor_rec_cb.setChecked(True)

        second = AutotrackRecordDialog(db)
        qtbot.addWidget(second)
        received: list[bool] = []
        second.meteor_record_changed.connect(received.append)

        # The restore already happened in __init__() above; re-verify no
        # signal fires by re-running the restore logic directly.
        second._load_recording_settings()

        assert received == []
        assert second.is_meteor_record_enabled() is True


class TestListComboSelectionSync:
    """populate_list_combo() must notify autotrack_list_changed whenever the
    combo's effective selection actually changes, even though the rebuild
    itself is wrapped in blockSignals() (GitHub Issue #27 follow-up,
    2026-08-23): a user created a single Autotrack list, added a satellite
    entry, and checked "Enable Autotrack" -- the combo clearly showed that
    list selected, but MainWindow's AutotrackManager.set_list() had never
    actually been called for it (blockSignals() swallowed the selection
    change), so entries() stayed empty and Autotrack silently did nothing.
    """

    def test_first_list_created_emits_its_id(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from core.autotrack import AutotrackManager

        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)
        received: list[object] = []
        dlg.autotrack_list_changed.connect(received.append)

        list_id = AutotrackManager.create_list(db, "Met")
        dlg._reload_at_lists()  # same call _on_at_add_list() makes

        assert received == [list_id]
        assert dlg.current_list_id() == list_id

    def test_unchanged_selection_does_not_re_emit(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)
        received: list[object] = []
        dlg.autotrack_list_changed.connect(received.append)

        # Renaming triggers the same reload path but the selection itself
        # (still the one and only list) does not change.
        list_id = dlg.current_list_id()
        assert list_id is not None
        AutotrackManager.rename_list(db, list_id, "Met (renamed)")
        dlg._reload_at_lists()

        assert received == []

    def test_current_list_id_none_when_no_lists(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        assert dlg.current_list_id() is None

    def test_deleting_selected_list_emits_new_selection(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)
        first_id = dlg.current_list_id()
        assert first_id is not None

        received: list[object] = []
        dlg.autotrack_list_changed.connect(received.append)
        AutotrackManager.delete_list(db, first_id)
        dlg._reload_at_lists()

        assert received == [None]
        assert dlg.current_list_id() is None


class TestMainWindowSyncsInitialListSelection:
    """MainWindow must sync AutotrackManager to whatever list the dialog's
    combo already selected in its own __init__(), since that happens before
    autotrack_list_changed is connected (same class of bug as the Recording
    checkbox restore, and the combo-selection bug above)."""

    def _make_window(self, qtbot: QtBot, db: sqlite3.Connection) -> MainWindow:
        from data.tle_manager import TLEManager

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        return w

    def test_sole_existing_list_is_synced_on_startup(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        from core.autotrack import AutotrackManager

        list_id = AutotrackManager.create_list(db, "Met")
        AutotrackManager.add_entry(db, list_id, 57166, "test-xpdr-uuid")

        w = self._make_window(qtbot, db)

        assert w._autotrack.entries()
        assert w._autotrack.entries()[0].norad_cat_id == 57166

    def test_no_lists_leaves_autotrack_with_no_entries(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        w = self._make_window(qtbot, db)

        assert w._autotrack.entries() == []
