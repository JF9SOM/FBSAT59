"""GreenCube/MARMOTSat digipeater application-layer message format.

Per the GreenCube Digipeater Manual (Sapienza/S5Lab, Issue 1.1, Sept 2022),
the CSP payload carried by a digipeater/store-and-forward frame is a plain
ASCII line of the form::

    $SourceCallsign > $DestCallsign, $SatelliteName, STORE=$Time $Message

e.g. ``IU0POY > IU0BFO, GreenCube, STORE=5 This a message relayed...``

``STORE=0`` (or omitting the ``STORE=`` field entirely) requests immediate
real-time relay; a positive value requests the satellite hold the message
on board and re-transmit it after that many seconds (max 172800 = 2 days).
The satellite name and the "STORE" keyword are case sensitive. Total
message length is capped at 180 characters by the satellite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_MESSAGE_LENGTH = 180

_PATTERN = re.compile(
    r"^\s*(?P<source>\S+)\s*>\s*(?P<dest>\S+)\s*,\s*(?P<sat_name>[^,]+?)\s*,"
    r"\s*STORE=(?P<store>\d+)\s+(?P<content>.*)$"
)


@dataclass(frozen=True)
class DigiMessage:
    source: str
    dest: str
    sat_name: str
    store_seconds: int
    content: str

    def format(self) -> str:
        return (
            f"{self.source} > {self.dest}, {self.sat_name}, "
            f"STORE={self.store_seconds} {self.content}"
        )


def parse_message(text: str) -> DigiMessage | None:
    """Parse a received digipeater/telemetry payload as a store-and-forward
    message. Returns None if `text` does not match the expected format
    (e.g. it is a raw telemetry beacon instead)."""
    match = _PATTERN.match(text)
    if match is None:
        return None
    return DigiMessage(
        source=match.group("source"),
        dest=match.group("dest"),
        sat_name=match.group("sat_name"),
        store_seconds=int(match.group("store")),
        content=match.group("content"),
    )


def build_message(
    source: str, dest: str, sat_name: str, content: str, store_seconds: int = 0
) -> str:
    """Build an outgoing message string in the GreenCube/MARMOTSat format.

    Raises ValueError if the fully-formatted message would exceed the
    satellite's 180-character limit.
    """
    msg = DigiMessage(
        source=source,
        dest=dest,
        sat_name=sat_name,
        store_seconds=store_seconds,
        content=content,
    )
    formatted = msg.format()
    if len(formatted) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"message too long ({len(formatted)} chars, max {MAX_MESSAGE_LENGTH})")
    return formatted
