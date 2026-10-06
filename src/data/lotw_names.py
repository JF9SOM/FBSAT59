"""Satellite names, bands and modes as LoTW wants them in an ADIF file.

LoTW accepts a satellite QSO only when ``SAT_NAME`` is exactly one of the IDs on its
list (``RS-44``, ``AO-91``, ``ARISS``, ...; see lotw_satellites.py). The satellites
table holds SATNOGS names (``DOSAAF-85``, ``FOX-1B``) with the OSCAR designator, when
there is one, among the alternative names, so the LoTW ID is looked up from those.
"""

from __future__ import annotations

import json
import re
import sqlite3

from data.lotw_satellites import LOTW_SATELLITES

# NORAD IDs whose names/alternative names never give the LoTW ID.
_OVERRIDES: dict[int, str] = {
    25544: "ARISS",  # the ISS; LoTW's list calls it ARISS
}

# Something that looks like a designator inside a longer name: "CAS 4A", "TO-108", "RS44".
# A designator followed by "-<digit>" is part of a longer name (TEVEL2-4), not one itself.
_DESIGNATOR_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{1,6})[- ]?(\d{1,3}[A-Z]?)(?![A-Z0-9]|-\d)")
# SATNOGS "TEVEL2-4" is LoTW's "TEV2-4" (the plain "TEVEL<n>" IDs are the first-generation Tevels).
_TEVEL2_RE = re.compile(r"^TEVEL2-(\d)$")


def _candidates(text: str) -> list[str]:
    """LoTW-style spellings of every designator found in *text* (best guess first)."""
    upper = text.strip().upper()
    found = []
    tevel2 = _TEVEL2_RE.match(upper)
    if tevel2:
        found.append(f"TEV2-{tevel2.group(1)}")
    found += [upper, upper.replace(" ", "-"), upper.replace("-", "").replace(" ", "")]
    for match in _DESIGNATOR_RE.finditer(upper):
        prefix, number = match.groups()
        found.append(f"{prefix}-{number}")
        found.append(f"{prefix}{number}")
    return found


def lotw_sat_id(norad: int | None, name: str, alt_names_json: str | None) -> str | None:
    """The LoTW satellite ID for a satellites-table row, or None if LoTW has no such satellite.

    Order: explicit override, then the alternative names (where the OSCAR designator
    lives), then the name itself. A spelling counts only if it is on LoTW's list.
    """
    if norad in _OVERRIDES:
        return _OVERRIDES[norad]
    try:
        alts = json.loads(alt_names_json) if alt_names_json else []
    except (TypeError, ValueError):
        alts = []
    for text in [*map(str, alts), name]:
        for candidate in _candidates(text):
            if candidate in LOTW_SATELLITES:
                return candidate
    return None


def lotw_sat_name(norad: int | None, name: str, alt_names_json: str | None) -> str:
    """The LoTW ID when there is one, otherwise *name* unchanged (LoTW will not take it)."""
    return lotw_sat_id(norad, name, alt_names_json) or name


def is_lotw_satellite(sat_name: str) -> bool:
    """True when *sat_name* is exactly an ID on LoTW's list."""
    return sat_name in LOTW_SATELLITES


def lotw_name_for_norad(conn: sqlite3.Connection, norad: int | None, fallback: str = "") -> str:
    """LoTW name of the satellite *norad* from the satellites table, else *fallback*."""
    if norad is None:
        return fallback
    try:
        row = conn.execute(
            "SELECT name, alt_names FROM satellites WHERE norad_cat_id = ?", (norad,)
        ).fetchone()
    except sqlite3.Error:
        return fallback
    if row is None:
        return fallback
    return lotw_sat_name(norad, str(row[0] or fallback), row[1])


def band_freq_hz(freq_hz: float | None) -> int:
    """Round a frequency down to its MHz (145988148 -> 145000000): enough to name the band."""
    if not freq_hz or freq_hz <= 0:
        return 0
    return int(freq_hz // 1_000_000) * 1_000_000


def normalize_logged_satellite_names(conn: sqlite3.Connection) -> int:
    """Rewrite the satellite name of already-logged FT4/Q65 QSOs to the LoTW ID.

    Rows saved before LoTW names were used carry the SATNOGS name (``DOSAAF-85``).
    Only rows with a NORAD ID whose satellite resolves to an ID are touched, so the
    call is idempotent. Returns the number of rows changed.
    """
    changed = 0
    for table in ("ft4_log", "q65_log"):
        try:
            rows = conn.execute(
                f"SELECT id, norad_cat_id, sat_name FROM {table} WHERE norad_cat_id IS NOT NULL"  # noqa: S608 - fixed table names
            ).fetchall()
        except sqlite3.Error:
            continue  # table not created yet
        for row_id, norad, sat in rows:
            new = lotw_name_for_norad(conn, norad, sat or "")
            if new and new != sat:
                conn.execute(f"UPDATE {table} SET sat_name = ? WHERE id = ?", (new, row_id))  # noqa: S608
                changed += 1
    if changed:
        conn.commit()
    return changed
