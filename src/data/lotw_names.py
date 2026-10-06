"""Satellite names as LoTW / the ADIF Satellite_List expect them (``RS-44``, ``AO-91``, ...).

The satellites table stores SATNOGS names (``DOSAAF-85``, ``FOX-1B``) with the
OSCAR-style designator, when there is one, among the alternative names. LoTW
matches ``SAT_NAME`` against its own list of designators, so a log written
with the SATNOGS name is rejected.
"""

from __future__ import annotations

import json
import re

# NORAD IDs whose alternative names do not give the designator in a usable form.
_OVERRIDES: dict[int, str] = {
    25544: "ISS",  # alt names are ZARYA / RS0ISS / NA1SS
    43770: "AO-95",  # stored as "AO-95Fox-1Cliff"
}

# An OSCAR-style designator: letters, a hyphen or space, a number, optional letter.
_DESIGNATOR_RE = re.compile(r"^([A-Z]{1,4})[- ](\d+[A-Z]?)$")


def lotw_sat_name(norad: int | None, name: str, alt_names_json: str | None) -> str:
    """Return the LoTW satellite name for a satellites-table row.

    Order: explicit override, then the first alternative name that looks like
    an OSCAR designator (``CAS 4A`` becomes ``CAS-4A``), then *name* unchanged.
    """
    if norad in _OVERRIDES:
        return _OVERRIDES[norad]
    try:
        alts = json.loads(alt_names_json) if alt_names_json else []
    except (TypeError, ValueError):
        alts = []
    for alt in alts:
        match = _DESIGNATOR_RE.match(str(alt).strip().upper())
        if match:
            return f"{match.group(1)}-{match.group(2)}"
    return name


def band_freq_hz(freq_hz: float | None) -> int:
    """Round a frequency down to its MHz (145988148 -> 145000000): enough to name the band."""
    if not freq_hz or freq_hz <= 0:
        return 0
    return int(freq_hz // 1_000_000) * 1_000_000
