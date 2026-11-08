"""ACARS message model for the Iridium tab.

iridium-toolkit's ``reassembler.py -m libacars -a json`` prints one JSON object
per line.  Only objects that carry an ``acars`` member are public ACARS traffic;
everything else (voice, SMS, pager, ...) is dropped here and never reaches the
UI or the disk.  That is the single privacy gate of the Iridium feature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IridiumAcarsMessage:
    """One decoded ACARS message received over Iridium."""

    timestamp: str  # ISO 8601, as reported by iridium-toolkit (UTC)
    direction: str  # "uplink" (ground to air) or "downlink" (air to ground)
    registration: str  # aircraft registration / tail number, without padding dots
    flight: str
    label: str
    block_id: str
    ack: str
    mode: str
    text: str
    crc_ok: bool
    # The complete "acars" object, kept for the detail view (decoded ADS-C etc.).
    details: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def summary(self) -> str:
        """Return a one-line summary for list views."""
        text = self.text.replace("\r", " ").replace("\n", " ").strip()
        return f"{self.registration} {self.flight} [{self.label}] {text}".strip()


def _clean(value: object) -> str:
    """Return *value* as a string with ACARS padding dots and NULs removed."""
    if value is None:
        return ""
    return str(value).strip().strip(".").replace("\x00", "")


def parse_acars_json_line(line: str) -> IridiumAcarsMessage | None:
    """Parse one line of reassembler JSON output.

    Returns None for anything that is not a well-formed ACARS message: blank
    lines, non-JSON text, and JSON objects without an ``acars`` member.
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    acars = obj.get("acars")
    if not isinstance(acars, dict):
        return None
    return IridiumAcarsMessage(
        timestamp=str(obj.get("timestamp", "")),
        direction=str(obj.get("link_direction", "")),
        registration=_clean(acars.get("reg")),
        flight=_clean(acars.get("flight")),
        label=_clean(acars.get("label")),
        block_id=_clean(acars.get("blk_id")),
        ack=_clean(acars.get("ack")),
        mode=_clean(acars.get("mode")),
        text=str(acars.get("msg_text", "")),
        crc_ok=bool(acars.get("crc_ok", False)),
        details=acars,
    )
