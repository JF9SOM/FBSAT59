"""User-configurable save locations for received/recorded data.

Each data kind has a platform default and an optional user override stored in
``app_settings`` under ``data_dir_<kind>``.  An empty/missing override means
"use the default", so resetting a location is just clearing the key.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Kinds in display order.
KIND_IQ = "iq"
KIND_AUDIO = "audio"
KIND_METEOR = "meteor"
KIND_SSTV = "sstv"
KIND_SSDV = "ssdv"
KIND_VOICE = "voice"
ALL_KINDS: tuple[str, ...] = (KIND_IQ, KIND_AUDIO, KIND_METEOR, KIND_SSTV, KIND_SSDV, KIND_VOICE)

_KEY_PREFIX = "data_dir_"


def _setting_key(kind: str) -> str:
    return _KEY_PREFIX + kind


def default_dir(kind: str) -> Path:
    """Return the built-in default folder for *kind*."""
    if kind == KIND_IQ:
        return Path.home() / "iq_recordings"
    if kind == KIND_AUDIO:
        return Path.home() / "audio_recordings"
    from PySide6.QtCore import QStandardPaths

    loc = QStandardPaths.StandardLocation
    if kind in (KIND_METEOR, KIND_SSTV, KIND_SSDV):
        pics = QStandardPaths.writableLocation(loc.PicturesLocation)
        base = Path(pics) if pics else Path.home() / "Pictures"
        names = {
            KIND_METEOR: "fbsat59_meteor",
            KIND_SSTV: "FBSAT59-SSTV",
            KIND_SSDV: "FBSAT59-SSDV",
        }
        return base / names[kind]
    if kind == KIND_VOICE:
        music = QStandardPaths.writableLocation(loc.MusicLocation)
        base = Path(music) if music else Path.home() / "Music"
        return base / "FBSAT59-Voice"
    raise ValueError(f"unknown data kind: {kind}")


def _read_override(kind: str) -> str:
    try:
        from data.database import get_db_path

        db_path = get_db_path()
        if not db_path.exists():
            return ""
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_setting_key(kind),)
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]).strip() if row and row[0] else ""
    except Exception:
        logger.exception("data_paths: failed to read override for %s", kind)
        return ""


def get_dir(kind: str) -> Path:
    """Return the effective folder for *kind* (user override, else default)."""
    override = _read_override(kind)
    return Path(override).expanduser() if override else default_dir(kind)


def get_override(kind: str) -> str:
    """Return the raw user override for *kind* ('' when using the default)."""
    return _read_override(kind)


def set_override(conn: sqlite3.Connection, kind: str, path: str) -> None:
    """Persist *path* as the folder for *kind*; an empty path restores the default."""
    value = path.strip()
    if value:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (_setting_key(kind), value),
        )
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (_setting_key(kind),))
    conn.commit()


def migrate_legacy_iq_dir(conn: sqlite3.Connection) -> None:
    """Carry the old Rig Settings ``iq_save_dir`` over to the new setting once."""
    import json

    flag = "data_dir_migrated_v1"
    try:
        if conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (flag,)).fetchone():
            return
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'sdr_settings'").fetchone()
        if row and row[0]:
            legacy = str(json.loads(row[0]).get("iq_save_dir", "")).strip()
            if legacy and Path(legacy).expanduser() != default_dir(KIND_IQ):
                set_override(conn, KIND_IQ, legacy)
        # Mark done so a later "Reset to default" is not undone at next startup.
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, '1', CURRENT_TIMESTAMP)",
            (flag,),
        )
        conn.commit()
    except Exception:
        logger.exception("data_paths: legacy IQ dir migration failed")


def check_writable(path: Path) -> str | None:
    """Return an error message if *path* cannot be created/written, else None."""
    import os

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return str(exc)
    if not os.access(path, os.W_OK):
        return "Folder is not writable."
    return None
