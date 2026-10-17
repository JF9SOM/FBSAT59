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


def test_unusable_override_falls_back_and_notifies_once(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")  # a file where a folder is needed -> unusable
    data_paths.set_override(db, data_paths.KIND_IQ, str(blocker / "iq"))
    calls: list[str] = []
    data_paths.set_fallback_notifier(lambda kind, _c, _d: calls.append(kind))
    data_paths._fallback_active.clear()
    try:
        default = data_paths.default_dir(data_paths.KIND_IQ)
        assert data_paths.get_dir(data_paths.KIND_IQ) == default
        assert data_paths.get_dir(data_paths.KIND_IQ) == default
        assert calls == ["iq"]  # notified once per episode
        data_paths.set_override(db, data_paths.KIND_IQ, str(tmp_path / "ok"))
        assert data_paths.get_dir(data_paths.KIND_IQ) == tmp_path / "ok"
    finally:
        data_paths.set_fallback_notifier(None)
        data_paths._fallback_active.clear()


def test_transfer_copy_and_move(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"aaa")
    (src / "sub" / "b.bin").write_bytes(b"bb")
    dst.mkdir()
    (dst / "a.bin").write_bytes(b"existing")  # same name is skipped, never overwritten
    assert data_paths.transfer_files(src, dst, move=False) == (1, 0)
    assert (dst / "a.bin").read_bytes() == b"existing"
    assert (src / "sub" / "b.bin").exists()
    (dst / "sub" / "b.bin").unlink()
    assert data_paths.transfer_files(src, dst, move=True) == (1, 0)
    assert not (src / "sub" / "b.bin").exists()
    assert not (src / "sub").exists()  # emptied subfolder removed
    assert (src / "a.bin").exists()  # skipped file is left alone


def test_same_or_nested(tmp_path: Path) -> None:
    assert data_paths.same_or_nested(tmp_path, tmp_path / "x")
    assert not data_paths.same_or_nested(tmp_path / "a", tmp_path / "b")
