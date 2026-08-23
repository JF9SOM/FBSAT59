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


@pytest.fixture(autouse=True)
def _no_restart_warning_popup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toggling Enable Autotrack shows a modal "restart required" warning
    (GitHub Issue #27 follow-up, 2026-08-23) that would otherwise block
    every test exercising _at_enable_cb.setChecked()/_on_enable_toggled()
    on dlg.exec()."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)


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


class TestReloadAtEntriesSignalsListsModified:
    """_reload_at_entries() must emit lists_modified whenever the selected
    list's entries change (add/remove/reorder), not just when lists
    themselves are added/removed/renamed -- main_window.py uses this signal
    to retry the Autotrack pass-prediction warm-up, which is otherwise never
    re-attempted once a list's first _start_autotrack_warmup() call ran as a
    silent no-op against an empty list (GitHub Issue #27 follow-up,
    2026-08-23)."""

    def test_add_entry_emits_lists_modified(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from core.autotrack import AutotrackManager

        list_id = AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)
        # current_list_id() (the combo, used by main_window.py) is
        # auto-selected on construction, but the separate list *widget*
        # (_at_selected_list_id, used by _reload_at_entries() itself) is
        # not -- mirror the real click a user makes on the list widget.
        dlg._at_list_widget.setCurrentRow(0)
        received = 0

        def _bump() -> None:
            nonlocal received
            received += 1

        dlg.lists_modified.connect(_bump)

        AutotrackManager.add_entry(db, list_id, 57166, "test-xpdr-uuid")
        dlg._reload_at_entries()

        assert received == 1

    def test_reload_with_no_selected_list_does_not_crash(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)
        assert dlg.current_list_id() is None

        # Should be a no-op (early return before the lists_modified emit),
        # not an exception.
        dlg._reload_at_entries()


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


class TestStatusLabelHeight:
    """The status label must reserve enough height for two wrapped lines --
    "Next: METEOR M2-3 in 463 min" wraps to two lines under some fonts
    (confirmed on macOS; the same text fits on one line on Windows) and the
    second line was clipped by the row below it (GitHub Issue #27
    follow-up, 2026-08-23)."""

    def test_status_label_reserves_two_lines(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        line_height = dlg._at_status_label.fontMetrics().height()
        assert dlg._at_status_label.minimumHeight() >= line_height * 2


class TestAutotrackEnabledPersistence:
    """Enable Autotrack must be saved and restored across restarts (GitHub
    Issue #27 follow-up, 2026-08-23): a user unchecked it and closed the
    dialog, but the next app launch showed it checked again anyway -- see
    also TestMainWindowRestoresAutotrackEnabled below for the other half
    of this fix (the Autotrack Timer's auto-start logic re-enabling it on
    the very next tick if this alone were fixed)."""

    def test_defaults_to_unchecked_when_nothing_saved(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        assert dlg.is_autotrack_enabled() is False

    def test_checking_persists_to_app_settings(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        dlg._at_enable_cb.setChecked(True)

        row = db.execute(
            "SELECT value FROM app_settings WHERE key = 'autotrack_enabled'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "1"

    def test_new_dialog_instance_restores_checked_state(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        first = AutotrackRecordDialog(db)
        qtbot.addWidget(first)
        first._at_enable_cb.setChecked(True)

        second = AutotrackRecordDialog(db)
        qtbot.addWidget(second)

        assert second.is_autotrack_enabled() is True

    def test_unchecking_persists_too(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        first = AutotrackRecordDialog(db)
        qtbot.addWidget(first)
        first._at_enable_cb.setChecked(True)
        first._at_enable_cb.setChecked(False)

        second = AutotrackRecordDialog(db)
        qtbot.addWidget(second)

        assert second.is_autotrack_enabled() is False


class TestRestartRequiredWarning:
    """Toggling Enable Autotrack by hand must always warn the user to
    restart the app (2026-08-23, GitHub Issue #27): intermittent rig/SDR
    "device already claimed" failures at AOS traced back to a stale
    handle from a previous session or a previous toggle, and a clean
    restart after every toggle is the chosen mitigation while the
    underlying handle-lifecycle issue is tracked down. Programmatic
    updates (set_autotrack_enabled(), used to restore saved state or sync
    from the Autotrack Timer) must NOT warn -- only a user's own click."""

    def test_checking_by_hand_warns(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from unittest.mock import patch

        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        with patch("ui.autotrack_record_dialog.QMessageBox.warning") as mock_warn:
            dlg._at_enable_cb.setChecked(True)

        mock_warn.assert_called_once()

    def test_unchecking_by_hand_warns(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from unittest.mock import patch

        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)
        dlg._at_enable_cb.setChecked(True)

        with patch("ui.autotrack_record_dialog.QMessageBox.warning") as mock_warn:
            dlg._at_enable_cb.setChecked(False)

        mock_warn.assert_called_once()

    def test_programmatic_set_does_not_warn(self, qtbot: QtBot, db: sqlite3.Connection) -> None:
        from unittest.mock import patch

        from core.autotrack import AutotrackManager

        AutotrackManager.create_list(db, "Met")
        dlg = AutotrackRecordDialog(db)
        qtbot.addWidget(dlg)

        with patch("ui.autotrack_record_dialog.QMessageBox.warning") as mock_warn:
            dlg.set_autotrack_enabled(True)
            dlg.set_autotrack_enabled(False)

        mock_warn.assert_not_called()


class TestMainWindowRestoresAutotrackEnabled:
    """MainWindow must sync its own _autotrack_enabled flag from the
    dialog's already-restored checkbox state, and the Autotrack Timer's
    auto-start "armed" guard must not immediately re-trigger and clobber
    a restored-as-unchecked state on the very first tick after restart."""

    def _make_window(self, qtbot: QtBot, db: sqlite3.Connection) -> MainWindow:
        from data.tle_manager import TLEManager

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        return w

    def test_restored_enabled_state_is_reflected_in_mainwindow_flag(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        from core.autotrack import AutotrackManager

        list_id = AutotrackManager.create_list(db, "Met")
        AutotrackManager.add_entry(db, list_id, 57166, "test-xpdr-uuid")
        db.execute("INSERT INTO app_settings (key, value) VALUES ('autotrack_enabled', '1')")
        db.commit()

        w = self._make_window(qtbot, db)

        assert w._autotrack_enabled is True
        assert w._at_dialog.is_autotrack_enabled() is True

    def test_no_saved_state_defaults_to_disabled(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        w = self._make_window(qtbot, db)

        assert w._autotrack_enabled is False

    def test_timer_auto_start_does_not_fire_on_the_first_tick_after_restart(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        """The Autotrack Timer's start-time field defaults to "now" every
        time the dialog is rebuilt, so a naive ">=" check on the very
        first _check_autotrack() tick after a restart would immediately
        re-enable Autotrack even though it was restored as unchecked
        (GitHub Issue #27 follow-up, 2026-08-23)."""
        from core.autotrack import AutotrackManager

        list_id = AutotrackManager.create_list(db, "Met")
        AutotrackManager.add_entry(db, list_id, 57166, "test-xpdr-uuid")

        w = self._make_window(qtbot, db)
        assert w._autotrack_enabled is False

        w._check_autotrack()

        assert w._autotrack_enabled is False

    def test_timer_auto_start_fires_after_arming_then_crossing_start_time(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        """The positive case for the armed-guard above: a start time the
        user actually sets in the future must still trigger auto-start
        once it's genuinely reached, not just on a restart's default."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from core.autotrack import AutotrackManager

        list_id = AutotrackManager.create_list(db, "Met")
        AutotrackManager.add_entry(db, list_id, 57166, "test-xpdr-uuid")

        w = self._make_window(qtbot, db)
        w._autotrack.mark_searches_ready()  # bypass the async warmup race
        assert w._autotrack_enabled is False

        future = datetime.now(UTC) + timedelta(hours=1)
        past = datetime.now(UTC) - timedelta(minutes=1)

        with patch.object(w._at_dialog, "get_timer_start_utc", return_value=future):
            w._check_autotrack()
        assert w._autotrack_enabled is False
        assert w._autotrack_timer_armed is True

        with patch.object(w._at_dialog, "get_timer_start_utc", return_value=past):
            w._check_autotrack()
        assert w._autotrack_enabled is True
        assert w._autotrack_timer_armed is False

    def test_manually_disabling_disarms_the_timer(
        self, qtbot: QtBot, db: sqlite3.Connection
    ) -> None:
        """Manually unchecking Enable Autotrack must not leave a still-armed
        timer to silently re-enable it once its (still future) start time
        is reached -- that would be the same "can't turn it off" symptom
        this whole fix targets."""
        from core.autotrack import AutotrackManager

        list_id = AutotrackManager.create_list(db, "Met")
        AutotrackManager.add_entry(db, list_id, 57166, "test-xpdr-uuid")

        w = self._make_window(qtbot, db)
        w._autotrack_timer_armed = True

        w._on_autotrack_toggled(False)

        assert w._autotrack_timer_armed is False
