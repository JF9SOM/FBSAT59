"""Remember the main window's size, position and maximized state across restarts.

Uses Qt's own saveGeometry()/restoreGeometry(), which also pulls a window back onto
a screen when the monitor layout has changed since it was saved. The value is kept
in the app_settings table, like the other persisted UI settings.
"""

from __future__ import annotations

import logging
import sqlite3

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

GEOMETRY_KEY = "main_window_geometry"


def save_window_geometry(conn: sqlite3.Connection, widget: QWidget) -> None:
    """Store *widget*'s geometry in app_settings; a failure is logged, never raised."""
    try:
        value = bytes(widget.saveGeometry().toBase64().data()).decode("ascii")
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (GEOMETRY_KEY, value),
        )
        conn.commit()
    except Exception:
        logger.warning("Could not save the window geometry", exc_info=True)


def restore_window_geometry(conn: sqlite3.Connection, widget: QWidget) -> bool:
    """Apply the stored geometry to *widget*; False if none is stored or it is unusable."""
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (GEOMETRY_KEY,)
        ).fetchone()
        if not row or not row[0]:
            return False
        data = QByteArray.fromBase64(str(row[0]).encode("ascii"))
        return bool(widget.restoreGeometry(data))
    except Exception:
        logger.warning("Could not restore the window geometry", exc_info=True)
        return False
