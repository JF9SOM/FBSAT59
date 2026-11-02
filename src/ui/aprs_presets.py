"""Canned messages for the APRS tab's Send form.

The list lives in ``app_settings`` as JSON. ``{grid}`` in a preset stands for the
operator's own 4-character grid square and is filled in when the preset is chosen.
"""

from __future__ import annotations

import json
from typing import Any

PRESETS_KEY = "aprs_message_presets"
GRID_TOKEN = "{grid}"
MAX_MESSAGE_LEN = 67  # same limit as the Message box

DEFAULT_PRESETS: tuple[str, ...] = (
    GRID_TOKEN,
    f"599 {GRID_TOKEN}",
    "599",
    "R TU 73",
    "RR73",
    "73",
)


def load_presets(conn: Any) -> list[str]:
    """The saved presets, or the defaults when none are saved (or the row is unreadable)."""
    if not hasattr(conn, "execute"):
        return list(DEFAULT_PRESETS)
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (PRESETS_KEY,)
        ).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, list):
                return clean_presets(data)
    except Exception:  # noqa: BLE001 - a broken row must not break the tab
        pass
    return list(DEFAULT_PRESETS)


def save_presets(conn: Any, presets: list[str]) -> None:
    """Persist *presets* (an empty list is kept, so the user can clear them all)."""
    if not hasattr(conn, "execute"):
        return
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (PRESETS_KEY, json.dumps(clean_presets(presets), ensure_ascii=False)),
    )
    conn.commit()


def clean_presets(items: list[Any]) -> list[str]:
    """Strip blanks and duplicates, keeping the order."""
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def expand_preset(preset: str, grid: str) -> str:
    """The message to put in the Message box: ``{grid}`` filled in, trimmed to the limit."""
    return preset.replace(GRID_TOKEN, grid).strip()[:MAX_MESSAGE_LEN]
