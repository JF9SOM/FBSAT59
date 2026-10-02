"""Detect the AX.25 baud rate (1200/4800/9600) from transmitter text."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

BAUD_CHOICES = ("1200", "4800", "9600")

# "1k2" / "9K6" / "FSK9k6" / "9600" / "4800bps"; digits must not continue on
# either side so e.g. "19200" or "12000" never match.
_BAUD_RE = re.compile(r"(?<!\d)(1k2|4k8|9k6|1200|4800|9600)(?!\d)", re.IGNORECASE)
_NORMALIZE = {
    "1k2": "1200",
    "4k8": "4800",
    "9k6": "9600",
    "1200": "1200",
    "4800": "4800",
    "9600": "9600",
}


def detect_baud_from_text(text: str | None) -> str | None:
    """Return "1200"/"4800"/"9600" if ``text`` contains a baud marker, else None."""
    if not text:
        return None
    m = _BAUD_RE.search(text)
    return _NORMALIZE[m.group(1).lower()] if m else None


def detect_baud_from_value(value: Any) -> str | None:
    """Map a numeric baud (int/str) to a choice string, or None if unsupported."""
    try:
        text = str(int(value))
    except (TypeError, ValueError):
        return None
    return text if text in BAUD_CHOICES else None


def detect_baud_from_transmitter(xpdr: dict[str, Any] | None) -> str | None:
    """Detect baud from a DB transmitter: description text first, then ``baud``."""
    if not xpdr:
        return None
    return detect_baud_from_text(xpdr.get("description")) or detect_baud_from_value(
        xpdr.get("baud")
    )


def detect_baud_from_satyaml(tx_names: Iterable[str], baudrates: Iterable[Any]) -> str | None:
    """Detect baud from gr-satellites transmitter names, then their baudrates."""
    for name in tx_names:
        found = detect_baud_from_text(name)
        if found:
            return found
    for rate in baudrates:
        found = detect_baud_from_value(rate)
        if found:
            return found
    return None
