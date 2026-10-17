"""User-configurable save locations for received/recorded data.

Each data kind has a platform default and an optional user override stored in
``app_settings`` under ``data_dir_<kind>``.  An empty/missing override means
"use the default", so resetting a location is just clearing the key.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable
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


def is_usable(path: Path) -> bool:
    """Return True if *path* is (or can be made) a writable folder.

    A folder under a missing macOS ``/Volumes/<name>`` mount point counts as
    unusable even if it could technically be created, so a recording is never
    silently written to a stale local folder when the drive is unplugged.
    """
    import os
    import sys

    try:
        if sys.platform == "darwin" and len(path.parts) > 2 and path.parts[:2] == ("/", "Volumes"):
            if not Path(*path.parts[:3]).exists():
                return False
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


# Kinds currently saving to the default because the configured folder is unusable.
_fallback_active: set[str] = set()
_fallback_notifier: Callable[[str, Path, Path], None] | None = None


def set_fallback_notifier(notifier: Callable[[str, Path, Path], None] | None) -> None:
    """Register a callback ``(kind, configured, default)`` fired once per fallback episode."""
    global _fallback_notifier
    _fallback_notifier = notifier


def get_dir(kind: str) -> Path:
    """Return the folder to save *kind* into now.

    The user's folder when it is usable, otherwise the built-in default (the
    user is told once, and files can be moved over later from Settings).
    """
    override = _read_override(kind)
    if not override:
        _fallback_active.discard(kind)
        return default_dir(kind)
    configured = Path(override).expanduser()
    if is_usable(configured):
        _fallback_active.discard(kind)
        return configured
    fallback = default_dir(kind)
    if kind not in _fallback_active:
        _fallback_active.add(kind)
        logger.warning(
            "data_paths: %s folder %s unusable, saving to %s", kind, configured, fallback
        )
        if _fallback_notifier is not None:
            try:
                _fallback_notifier(kind, configured, fallback)
            except Exception:
                logger.exception("data_paths: fallback notifier failed")
    return fallback


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


def pending_files(source: Path, target: Path) -> list[Path]:
    """Files under *source* (relative paths) that are not yet present under *target*."""
    if not source.is_dir():
        return []
    out = []
    for f in sorted(source.rglob("*")):
        if f.is_file() and not (target / f.relative_to(source)).exists():
            out.append(f.relative_to(source))
    return out


def same_or_nested(a: Path, b: Path) -> bool:
    """True if *a* and *b* are the same folder or one contains the other."""
    ra, rb = a.expanduser().resolve(), b.expanduser().resolve()
    return ra == rb or ra in rb.parents or rb in ra.parents


def transfer_files(
    source: Path,
    target: Path,
    move: bool,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    """Copy (or move) files from *source* to *target*, skipping names that already exist.

    A move deletes each source file only after the copy's size matches, and
    removes emptied source subfolders at the end.  Returns ``(done, failed)``.
    """
    import shutil

    files = pending_files(source, target)
    done = failed = 0
    for i, rel in enumerate(files):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(i, len(files), str(rel))
        src, dst = source / rel, target / rel
        part = dst.with_name(dst.name + ".part")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, part)
            if part.stat().st_size != src.stat().st_size:
                raise OSError("size mismatch after copy")
            part.replace(dst)
            if move:
                src.unlink()
            done += 1
        except OSError:
            logger.exception("data_paths: transfer failed for %s", rel)
            failed += 1
            part.unlink(missing_ok=True)
    if progress is not None:
        progress(len(files), len(files), "")
    if move:
        for d in sorted((p for p in source.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):
                d.rmdir()  # only succeeds when empty
    return done, failed
