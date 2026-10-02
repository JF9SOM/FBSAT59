"""TX audio level helpers shared by the FT4 and Q65 tabs.

The TX Level slider is calibrated in dB below full scale rather than as a
linear percentage: rigs with a sensitive USB audio input (e.g. FT-991A in
data mode) only leave ALC saturation around -25 dB, which a linear 1-100
scale squeezes into its bottom few percent.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

TX_LEVEL_MIN_DB: int = -60
TX_LEVEL_MAX_DB: int = 0


def clamp_db(db: float) -> int:
    """Round *db* to a whole dB and clamp it to the slider range."""
    return max(TX_LEVEL_MIN_DB, min(TX_LEVEL_MAX_DB, round(db)))


def db_to_gain(db: float) -> float:
    """Convert a level in dB (relative to full scale) to a linear amplitude gain."""
    return float(10.0 ** (db / 20.0))


def pct_to_db(pct: float) -> int:
    """Convert a legacy linear percentage setting to the nearest slider dB value."""
    if pct <= 0.0:
        return TX_LEVEL_MIN_DB
    return clamp_db(20.0 * math.log10(pct / 100.0))


def load_level_db(data: Mapping[str, object]) -> int:
    """Read the saved level from a settings dict, migrating the legacy percent key."""
    raw_db = data.get("tx_level_db")
    if isinstance(raw_db, (int, float)):
        return clamp_db(float(raw_db))
    raw_pct = data.get("tx_level_pct")
    if isinstance(raw_pct, (int, float)):
        return pct_to_db(float(raw_pct))
    return TX_LEVEL_MAX_DB


def format_db(db: int) -> str:
    """Format a slider value for the level label (e.g. ``-23 dB``)."""
    return f"{db} dB"
