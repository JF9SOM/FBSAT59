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


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


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
