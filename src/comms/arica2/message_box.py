"""ARICA-2 amateur message-box command/response frames.

ARICA-2 (JS1YSD, Aoyama Gakuin University) runs a store-and-forward message
box on 436.830 MHz (4800 baud G3RUH GMSK, uplink and downlink on the same
frequency): 20 slots, 8 characters per message, one slot per callsign.

The frame layout is NOT officially published. It was reverse engineered from
JI1IZR's Windows tool by https://github.com/N6RFM/ARICA2-linux-groundstation
(2026-09) and confirmed there "byte for byte" against the official tool; it
has been reported to work over the air. Treat every constant here as
unverified against an official specification.

Uplink payload handed to Direwolf's KISS port (19 bytes; Direwolf adds the
HDLC flags and FCS, KISS adds the FEND/command bytes)::

    42 F8 BD | type1 type2 | callsign (6, NUL padded) | message (8, NUL padded)

Downlink payloads (as delivered by Direwolf's KISS port, FCS stripped) look
like AX.25 UI frames, but the destination address is only 5 bytes::

    dest (5, ASCII << 1) | source (6, ASCII << 1) | source SSID octet |
    0x03 | 0xF0 | plain ASCII text
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

MAX_SLOT = 20
CALLSIGN_LEN = 6
MESSAGE_LEN = 8

_PREFIX = bytes([0x42, 0xF8, 0xBD])
_DEST_LEN = 5
_SRC_LEN = 6
# dest + source + SSID octet + control + PID.
_HEADER_LEN = _DEST_LEN + _SRC_LEN + 1 + 2
_CONTROL_UI = 0x03
_PID_NONE = 0xF0


class Command(enum.Enum):
    """Uplink command kinds."""

    UPLOAD = "upload"
    CONFIRM = "confirm"
    PARROT = "parrot"
    DOWNLOAD = "download"


def _ascii_field(text: str, length: int, what: str) -> bytes:
    """Encode *text* as upper-case ASCII, NUL padded to *length*."""
    value = text.strip().upper()
    if not value.isascii() or not value.isprintable():
        raise ValueError(f"{what} must be printable ASCII")
    if len(value) > length:
        raise ValueError(f"{what} is longer than {length} characters")
    return value.encode("ascii").ljust(length, b"\x00")


def _command_bytes(command: Command, slot: int | None) -> bytes:
    if command is Command.UPLOAD:
        return bytes([0x50, 0x00])
    if command is Command.CONFIRM:
        return bytes([0x60, 0x00])
    if command is Command.PARROT:
        return bytes([0x70, 0x00])
    if slot is None or not 1 <= slot <= MAX_SLOT:
        raise ValueError(f"slot must be 1..{MAX_SLOT}")
    return bytes([0x40 + slot // 2, (slot % 2) * 128])


def build_command(
    command: Command,
    callsign: str,
    message: str = "",
    slot: int | None = None,
) -> bytes:
    """Build the 19-byte uplink payload for Direwolf's KISS port.

    *slot* is only used by DOWNLOAD (the satellite assigns slots itself on
    UPLOAD). Callsign is limited to 6 characters, the message to 8.
    """
    if not callsign.strip():
        raise ValueError("callsign is required")
    return (
        _PREFIX
        + _command_bytes(command, slot)
        + _ascii_field(callsign, CALLSIGN_LEN, "callsign")
        + _ascii_field(message, MESSAGE_LEN, "message")
    )


@dataclass(frozen=True)
class Downlink:
    """One decoded ARICA-2 downlink frame."""

    dest: str
    source: str
    ssid_octet: int
    text: str


def _unshift(data: bytes) -> str:
    return "".join(chr(b >> 1) for b in data).rstrip()


def parse_downlink(payload: bytes) -> Downlink | None:
    """Parse a downlink frame; None if it does not have the expected shape."""
    if len(payload) < _HEADER_LEN:
        return None
    if payload[_HEADER_LEN - 2] != _CONTROL_UI or payload[_HEADER_LEN - 1] != _PID_NONE:
        return None
    dest = _unshift(payload[:_DEST_LEN])
    source = _unshift(payload[_DEST_LEN : _DEST_LEN + _SRC_LEN])
    ssid_octet = payload[_DEST_LEN + _SRC_LEN]
    text = payload[_HEADER_LEN:].decode("ascii", errors="replace").strip("\x00\r\n ")
    return Downlink(dest=dest, source=source, ssid_octet=ssid_octet, text=text)
