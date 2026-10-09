"""CelesTrak OMM (JSON) support and 6-digit NORAD catalogue numbers.

Since mid-2026 the NORAD catalogue numbers have passed 99999. A classic TLE has
only five columns for the number, so CelesTrak cannot serve such objects as
``FORMAT=TLE`` (the group lists silently omit them, ``CATNR=`` answers "No GP
data found"); it serves them as OMM (``FORMAT=JSON``) only.

This module converts OMM records into ordinary three-line TLE text, writing a
6-digit number in the **Alpha-5** form that the TLE tools (python-sgp4, Skyfield,
Space-Track) use: the first digit is replaced by a letter, ``A`` = 10 ... so
100470 is ``A0470``. The rest of the app keeps working on TLE text and
``EarthSatellite(line1, line2)``; only code that reads the number back out of
line 1 must use :func:`parse_tle_norad` instead of ``int(line1[2:7])``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sgp4 import omm as _sgp4_omm
from sgp4.api import Satrec
from sgp4.exporter import export_tle

logger = logging.getLogger(__name__)

# SATNOGS hands out 90000-99999 as provisional placeholders; real catalogue
# numbers from 100000 on are *not* provisional (see docs/tle.md).
PROVISIONAL_NORAD_MIN: int = 90000
PROVISIONAL_NORAD_MAX: int = 99999

# Alpha-5: letters stand for 10..33, skipping I and O (they look like 1 and 0).
_ALPHA5_LETTERS: str = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def is_provisional_norad(norad: int) -> bool:
    """True for a SATNOGS provisional placeholder id (90000-99999)."""
    return PROVISIONAL_NORAD_MIN <= norad <= PROVISIONAL_NORAD_MAX


def alpha5_to_int(field: str) -> int:
    """Decode the 5-character catalogue-number field of a TLE (plain or Alpha-5)."""
    field = field.strip()
    if len(field) != 5:
        raise ValueError(f"catalogue number field must be 5 characters: {field!r}")
    first = field[0]
    if first.isdigit():
        return int(field)
    if first.upper() not in _ALPHA5_LETTERS:
        raise ValueError(f"invalid Alpha-5 catalogue number: {field!r}")
    return (10 + _ALPHA5_LETTERS.index(first.upper())) * 10000 + int(field[1:])


def parse_tle_norad(line1: str) -> int:
    """NORAD catalogue number of a TLE, from its line 1 (handles Alpha-5)."""
    return alpha5_to_int(line1[2:7])


def omm_to_tle(omm: dict[str, Any]) -> tuple[str, str, str]:
    """Convert one CelesTrak OMM record to ``(name, line1, line2)``.

    A catalogue number above 99999 comes out in Alpha-5 form. Raises
    ValueError/KeyError for a malformed record.
    """
    rec = Satrec()
    _sgp4_omm.initialize(rec, omm)
    line1, line2 = export_tle(rec)
    return str(omm.get("OBJECT_NAME") or omm.get("OBJECT_ID") or "").strip(), line1, line2


def omm_json_to_tle_text(text: str) -> str:
    """Convert a CelesTrak ``FORMAT=JSON`` response to three-line TLE text.

    Records that cannot be converted are skipped (and logged) rather than
    failing the whole group. Returns an empty string for an empty list.
    """
    records = json.loads(text)
    if isinstance(records, dict):
        records = [records]
    out: list[str] = []
    for rec in records:
        try:
            name, line1, line2 = omm_to_tle(rec)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("OMM record skipped (%s): %s", rec.get("OBJECT_NAME", "?"), exc)
            continue
        out.extend((name, line1, line2))
    return "\n".join(out) + ("\n" if out else "")


def celestrak_to_tle_text(text: str) -> str:
    """Normalise a CelesTrak GP response to three-line TLE text.

    JSON (``[`` ... ``]``) is converted from OMM; anything else (already TLE
    text, or an error message such as "No GP data found") is returned as is, so
    the callers' existing parsing keeps handling it.
    """
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            return omm_json_to_tle_text(stripped)
        except (ValueError, TypeError) as exc:
            logger.warning("CelesTrak JSON could not be parsed: %s", exc)
            return ""
    return text
