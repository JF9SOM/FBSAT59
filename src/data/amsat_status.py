"""
AMSAT satellite operational status scraper

Fetches satellite operational status from https://www.amsat.org/status/ and saves it to the DB.
Requires beautifulsoup4. Scraping is skipped if it is not installed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AMSAT_STATUS_URL = "https://www.amsat.org/status/"

_STATUS_MAP: dict[str, str] = {
    "operational": "operational",
    "partial": "partial",
    "partially operational": "partial",
    "non-operational": "non_operational",
    "non operational": "non_operational",
    "nonoperational": "non_operational",
    "not operational": "non_operational",
}

# bgcolor values that indicate an active/operational satellite
_OPERATIONAL_BG = {"#648fff", "#785ef0"}
# bgcolor value for "TLM/Beacon only" (confirmed via the page's own on-page legend,
# 2026-09-05: telemetry/beacon heard but the satellite's primary mode is not)
_TLM_BEACON_BG = {"#ffb000"}
# bgcolor values that mean "no report submitted" (treated as no data)
_EMPTY_BG = {"c0c0c0", "", "white"}

_SETTINGS_KEY = "amsat_status_data"
_TIMESTAMP_KEY = "amsat_status_updated_at"


class AMSATStatusFetcher:
    """Class that fetches, saves, and provides AMSAT operational status."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """
        Args:
            conn: SQLite connection
        """
        self._conn = conn

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_and_update(self) -> dict[str, str]:
        """
        Scrape the AMSAT status page and return a dict of satellite name → operational status.
        Results are saved to the DB.

        Returns:
            {"satellite_name_lower": "operational"|"partial"|"non_operational"}
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(AMSAT_STATUS_URL)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            logger.warning("AMSAT status fetch failed: %s", exc)
            return self.load_cached() or {}

        status_map = self._parse_html(html)
        if status_map:
            self._save(status_map)
            logger.info("AMSAT status updated: %d satellites", len(status_map))
        return status_map

    def load_cached(self) -> dict[str, str] | None:
        """Return the cached operational status. Returns None if not saved."""
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_SETTINGS_KEY,),
        ).fetchone()
        if row is None:
            return None
        try:
            return dict(json.loads(row[0]))
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

    def _parse_html(self, html: str) -> dict[str, str]:
        """Generate a dict of satellite name → operational status from HTML."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("beautifulsoup4 not installed; AMSAT status scraping disabled")
            return {}

        soup = BeautifulSoup(html, "html.parser")
        result = self._parse_tables(soup)
        if not result:
            result = self._parse_fallback(soup)
        return result

    def _parse_tables(self, soup: Any) -> dict[str, str]:
        """
        Extract satellite status from table-format HTML.

        Page structure (the page itself carries an on-page legend confirming this,
        checked live 2026-09-05):
          - Each satellite has multiple frequency rows (e.g. AO-7_[U/v], AO-7_[V/a])
          - Cells from index 1 onward contain time-series status entries, left=most recent
          - bgcolor #648fff (blue) = "Sat/Mode Active", #785ef0 (purple) = ISS Active
          - bgcolor #ffb000 (gold) = "TLM/Beacon only" (telemetry/beacon heard, primary
            mode not)
          - bgcolor #dc267f (magenta) = "Not Heard", #fe6100 (orange) = "Conflicting
            reports" -- both folded into "non_operational" below, same as no report at all
          - bgcolor C0C0C0 or empty = no report submitted (skip)

        Determination: check the most recent (leftmost) non-empty status for each
        frequency. A satellite is "operational" if at least one frequency is blue;
        else "partial" (used here for "TLM/Beacon only") if at least one is gold;
        else "non_operational". Blue takes priority over gold on the same satellite,
        so a satellite with one active mode and one beacon-only mode is counted as
        fully operational, not also listed as TLM/Beacon only.
        """
        result: dict[str, str] = {}

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue

            # Target tables that have a "Name" cell in the first row
            header_cells = rows[0].find_all(["td", "th"])
            if not header_cells:
                continue
            if header_cells[0].get_text(strip=True).lower() != "name":
                continue

            # sat_name → [active_count, tlm_beacon_count, total_freq_count]
            sat_freq: dict[str, list[int]] = {}

            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                name_raw = cells[0].get_text(strip=True)
                if not name_raw:
                    continue

                # "AO-7_[U/v]" → "AO-7"
                sat_name = re.sub(r"_\[.*?\]$", "", name_raw).strip()
                if not sat_name:
                    continue

                if sat_name not in sat_freq:
                    sat_freq[sat_name] = [0, 0, 0]
                sat_freq[sat_name][2] += 1

                # Find the bgcolor of the first non-empty cell from the left
                most_recent_bg: str | None = None
                for cell in cells[1:]:
                    bg = cell.get("bgcolor", "").strip().lower()
                    if bg not in _EMPTY_BG:
                        most_recent_bg = bg
                        break

                if most_recent_bg in {c.lower() for c in _OPERATIONAL_BG}:
                    sat_freq[sat_name][0] += 1
                elif most_recent_bg in {c.lower() for c in _TLM_BEACON_BG}:
                    sat_freq[sat_name][1] += 1

            for sat_name, (active_count, tlm_count, total_count) in sat_freq.items():
                if active_count > 0:
                    status = "operational"
                    print(
                        f"[AMSAT] {sat_name}: operational"
                        f" ({active_count}/{total_count} frequencies active)"
                    )
                elif tlm_count > 0:
                    status = "partial"
                    print(
                        f"[AMSAT] {sat_name}: TLM/Beacon only"
                        f" ({tlm_count}/{total_count} frequencies)"
                    )
                else:
                    status = "non_operational"
                    print(f"[AMSAT] {sat_name}: not operational")
                result[sat_name.lower()] = status

            # Stop after processing the target table
            break

        return result

    def _parse_fallback(self, soup: Any) -> dict[str, str]:
        """Fallback that extracts status via string matching when the table format is not found."""
        result: dict[str, str] = {}
        for elem in soup.find_all(["li", "p", "div", "tr"]):
            text = elem.get_text(strip=True)
            if not text or len(text) > 200:
                continue
            text_lower = text.lower()
            for status_key, status_val in _STATUS_MAP.items():
                if status_key in text_lower:
                    idx = text_lower.find(status_key)
                    name_candidate = text[:idx].strip(" :-–—/")
                    # Split by separator and take the last token as the name candidate
                    for sep in (":", "-", "–", "—", "/"):
                        if sep in name_candidate:
                            name_candidate = name_candidate.rsplit(sep, 1)[-1].strip()
                    if 2 <= len(name_candidate) <= 30:
                        result[name_candidate.lower()] = status_val
                    break
        return result

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _save(self, status_map: dict[str, str]) -> None:
        """Save the operational status to app_settings."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (_SETTINGS_KEY, json.dumps(status_map), now),
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
