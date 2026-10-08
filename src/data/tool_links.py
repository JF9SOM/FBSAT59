"""User-editable list of useful web sites shown in the Tools menu."""

from __future__ import annotations

import json
import sqlite3

SETTING_KEY = "tool_links"

DEFAULT_TOOL_LINKS: list[tuple[str, str]] = [
    ("Hams.at", "https://hams.at/"),
    ("Sked Prediction", "https://sat.fg8oj.com/sked.php"),
]


def normalize_url(url: str) -> str:
    """Strip whitespace and prepend ``https://`` when no scheme is present."""
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def load_tool_links(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return the saved (name, url) list, or the defaults when nothing is saved."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTING_KEY,)).fetchone()
    if row is None:
        return list(DEFAULT_TOOL_LINKS)
    try:
        data = json.loads(str(row["value"]))
        return [
            (str(d["name"]), str(d["url"]))
            for d in data
            if str(d.get("name", "")).strip() and str(d.get("url", "")).strip()
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        return list(DEFAULT_TOOL_LINKS)


def save_tool_links(conn: sqlite3.Connection, links: list[tuple[str, str]]) -> None:
    """Persist *links*, dropping blank rows and normalizing URLs."""
    clean = [
        {"name": n.strip(), "url": normalize_url(u)} for n, u in links if n.strip() and u.strip()
    ]
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (SETTING_KEY, json.dumps(clean, ensure_ascii=False)),
    )
    conn.commit()
