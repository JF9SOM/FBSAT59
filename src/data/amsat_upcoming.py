"""
AMSAT Upcoming Satellites -- "In Testing" list scraper

Fetches the "In Testing:" satellite list from https://www.amsat.org/upcoming-satellites/
and saves it to the DB. Requires beautifulsoup4. Scraping is skipped if it is not installed.

Mirrors the design of data.amsat_status.AMSATStatusFetcher (same cache/staleness
pattern), but this page only needs a plain name list -- there is no per-satellite
operational status to track, just membership in the "In Testing" table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AMSAT_UPCOMING_URL = "https://www.amsat.org/upcoming-satellites/"

_SETTINGS_KEY = "amsat_upcoming_data"
_TIMESTAMP_KEY = "amsat_upcoming_updated_at"


class AMSATUpcomingFetcher:
    """Fetches, saves, and provides AMSAT's "In Testing" upcoming-satellite list."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """
        Args:
            conn: SQLite connection
        """
        self._conn = conn

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_and_update(self) -> list[str]:
        """
        Scrape the AMSAT Upcoming Satellites page's "In Testing" table and return
        the satellite names found there. Results are saved to the DB.

        Returns:
            ["RS83S (Lobachevsky)", "HADES-SA (SpinnyONE)", ...]
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(AMSAT_UPCOMING_URL)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            logger.warning("AMSAT upcoming-satellites fetch failed: %s", exc)
            return self.load_cached() or []

        names = self._parse_html(html)
        if names:
            self._save(names)
            logger.info("AMSAT upcoming (In Testing) list updated: %d satellites", len(names))
        return names

    def load_cached(self) -> list[str] | None:
        """Return the cached "In Testing" name list. Returns None if not saved."""
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_SETTINGS_KEY,),
        ).fetchone()
        if row is None:
            return None
        try:
            return list(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def is_stale(self, max_age_hours: int = 24) -> bool:
        """Return whether the cache is stale (or not yet fetched)."""
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_TIMESTAMP_KEY,),
        ).fetchone()
        if row is None:
            return True
        try:
            ts = datetime.fromisoformat(str(row[0]))
            age_h = (datetime.now(UTC) - ts).total_seconds() / 3600
            return age_h >= max_age_hours
        except (ValueError, TypeError):
            return True

    # ------------------------------------------------------------------ #
    # HTML parser
    # ------------------------------------------------------------------ #

    def _parse_html(self, html: str) -> list[str]:
        """Extract the "In Testing" satellite name list from the page HTML."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning(
                "beautifulsoup4 not installed; AMSAT upcoming-satellites scraping disabled"
            )
            return []

        soup = BeautifulSoup(html, "html.parser")
        return self._parse_in_testing_table(soup)

    def _parse_in_testing_table(self, soup: Any) -> list[str]:
        """
        Extract satellite names from the "In Testing:" table.

        Page structure (confirmed live 2026-09-05): a heading element (a <p> or
        <strong>, not a consistent <h*> level) whose text is exactly "In Testing:"
        is immediately followed in document order by a <table> with columns
        Satellite / Uplink / Downlink / Comments -- one row per satellite, header
        row first. Only the first table after that heading is parsed, so later
        sections ("Upcoming:", "In Orbit, Unknown Status:") are never picked up.
        """
        heading = None
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "strong", "b"]):
            text = tag.get_text(strip=True).lower()
            if text.startswith("in testing"):
                heading = tag
                break
        if heading is None:
            return []

        table = heading.find_next("table")
        if table is None:
            return []

        names: list[str] = []
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip the "Satellite / Uplink / Downlink / Comments" header
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            name = cells[0].get_text(" ", strip=True)
            if name:
                names.append(name)
        return names

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _save(self, names: list[str]) -> None:
        """Save the "In Testing" name list to app_settings."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (_SETTINGS_KEY, json.dumps(names), now),
        )
        self._conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (_TIMESTAMP_KEY, now, now),
        )
        self._conn.commit()
