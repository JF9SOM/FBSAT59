"""Tests for user-configurable data save locations (core.data_paths)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core import data_paths


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME)"
    )
    conn.commit()
    monkeypatch.setattr("data.database.get_db_path", lambda: path)
    return conn


def test_default_when_unset(db: sqlite3.Connection) -> None:
    assert data_paths.get_dir(data_paths.KIND_IQ) == Path.home() / "iq_recordings"
    assert data_paths.get_override(data_paths.KIND_IQ) == ""


def test_set_and_reset(db: sqlite3.Connection, tmp_path: Path) -> None:
    data_paths.set_override(db, data_paths.KIND_IQ, str(tmp_path / "ssd"))
    assert data_paths.get_dir(data_paths.KIND_IQ) == tmp_path / "ssd"
    data_paths.set_override(db, data_paths.KIND_IQ, "")
    assert data_paths.get_dir(data_paths.KIND_IQ) == data_paths.default_dir(data_paths.KIND_IQ)


def test_legacy_iq_dir_migrates_once(db: sqlite3.Connection, tmp_path: Path) -> None:
    legacy = str(tmp_path / "old")
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('sdr_settings', ?)",
        (json.dumps({"iq_save_dir": legacy}),),
    )
    db.commit()
    data_paths.migrate_legacy_iq_dir(db)
    assert data_paths.get_override(data_paths.KIND_IQ) == legacy
    data_paths.set_override(db, data_paths.KIND_IQ, "")
    data_paths.migrate_legacy_iq_dir(db)  # must not undo the user's reset
    assert data_paths.get_override(data_paths.KIND_IQ) == ""


def test_check_writable(tmp_path: Path) -> None:
    assert data_paths.check_writable(tmp_path / "new") is None
