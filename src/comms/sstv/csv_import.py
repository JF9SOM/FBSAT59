"""SSDV packets from a CSV of frames downloaded from SatNOGS DB.

SatNOGS DB (the satellite page's "Download frames") gives one line per frame the stations
of the network decoded::

    2026-10-18 11:17:08|B316F17A...89||KM7DOS-CN87xi

(timestamp, frame as hex, an empty field, observer). This turns the frames that carry SSDV
into valid packets so that the picture can be built from other stations' receptions, the
same way as from our own (Stored Images, comms.sstv.packet_store):

* HADES-SA (AMSAT-EA 2FSK): a 251-byte frame of type 10 / address 3; ``55 66 BF 35 FB`` in
  front makes it a standard packet (see comms.sstv.fsk_ssdv);
* CCSDS / DSLWP (ASRTU-1): a 223-byte frame (find_dslwp_packet());
* anything else: a standard ``55 66`` packet somewhere in the frame (AX.25 SSDV).

Only packets that pass validate_packet() (CRC-32, or Reed-Solomon repair) are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from comms.sstv.packet_store import KIND_DSLWP, KIND_STANDARD, validate_packet
from comms.sstv.ssdv import find_dslwp_packet, find_ssdv_packet

HADES_SA_NORAD = 68446
ASRTU1_NORAD = 61781
_HADES_FRAME_LEN = 251
_HADES_PREFIX = b"\x55\x66\xbf\x35\xfb"
_CCSDS_FRAME_LEN = 223


@dataclass(frozen=True)
class CsvPacket:
    """One valid SSDV packet found in the CSV."""

    when: datetime
    norad: int
    kind: str
    packet: bytes


@dataclass(frozen=True)
class CsvExtract:
    """What was found in a CSV file."""

    lines: int  # non-empty lines
    frames: int  # lines with a usable timestamp and hex frame
    packets: list[CsvPacket]  # valid SSDV packets, oldest first
    invalid: int  # SSDV-looking frames that failed the CRC / repair


def parse_line(line: str) -> tuple[datetime, bytes] | None:
    """(time, frame) of one CSV line, or None if it is not a frame line."""
    parts = line.strip().split("|")
    if len(parts) < 2:
        return None
    try:
        when = datetime.fromisoformat(parts[0].strip().replace("Z", "+00:00"))
        frame = bytes.fromhex(parts[1].strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when, frame


def extract_packet(frame: bytes, default_norad: int) -> tuple[int, str, bytes] | None | bool:
    """``(norad, kind, packet)`` for a frame carrying a valid SSDV packet; None if the frame
    carries no SSDV; False if it looks like SSDV but is damaged beyond repair."""
    if len(frame) == _HADES_FRAME_LEN and frame[0] >> 4 == 10 and frame[0] & 15 == 3:
        valid = validate_packet(_HADES_PREFIX + frame)
        return (HADES_SA_NORAD, KIND_STANDARD, valid) if valid is not None else False
    if len(frame) == _CCSDS_FRAME_LEN:
        packet = find_dslwp_packet(frame)
        return (ASRTU1_NORAD, KIND_DSLWP, packet) if packet is not None else None
    found = find_ssdv_packet(frame)
    if found is None:
        return None
    valid = validate_packet(found)
    return (default_norad, KIND_STANDARD, valid) if valid is not None else False


def extract_from_csv(path: str | Path, default_norad: int = 0) -> CsvExtract:
    """Read a SatNOGS frames CSV and return its valid SSDV packets, oldest first."""
    lines = frames = invalid = 0
    packets: list[CsvPacket] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            lines += 1
            parsed = parse_line(line)
            if parsed is None:
                continue
            frames += 1
            when, frame = parsed
            found = extract_packet(frame, default_norad)
            if found is False:
                invalid += 1
            elif found:
                norad, kind, packet = found  # type: ignore[misc]
                packets.append(CsvPacket(when, norad, kind, packet))
    packets.sort(key=lambda p: p.when)  # oldest first: the store splits reused image ids by time
    return CsvExtract(lines, frames, packets, invalid)
