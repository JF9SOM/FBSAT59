"""ARICA-2 amateur message-box command/response frames.

ARICA-2 (JS1YSD, Aoyama Gakuin University) runs a store-and-forward message
box on 436.830 MHz (4800 baud G3RUH GMSK, uplink and downlink on the same
frequency): 20 slots, 8 characters per message, one slot per callsign.

The frame layout is NOT officially published. It was reverse engineered from
JI1IZR's Windows tool by https://github.com/N6RFM/ARICA2-linux-groundstation
(2026-09) and confirmed there "byte for byte" against the official tool. The
lab's own page (sakamotolab.phys.aoyama.ac.jp/research/current_space/ARICA-2/
amateur, 2026-10) now documents the frame: 21 bytes = 0x42 + 18-byte satellite
frame + AX.25 FCS, callsign space padded, message NUL padded, with FCS-verified
hex examples (Download ID 5 matches this module). Its Upload example uses type
bytes 40 00 while its own type table implies 50 00 -- unresolved.

Uplink payload handed to Direwolf's KISS port (19 bytes; Direwolf adds the
HDLC flags and FCS, KISS adds the FEND/command bytes)::

    42 F8 BD | type1 type2 | callsign (6, space padded) | message (8, NUL padded)

Downlink payloads (as delivered by Direwolf's KISS port, FCS stripped) are
AX.25 UI frames with a plain ASCII text. A real over-the-air capture
(2026-10-02, a response to JI1IZR's upload) is a standard frame::

    dest (6, ASCII << 1) | SSID octet | source (6, ASCII << 1) | SSID octet |
    0x03 | 0xF0 | "saved 'DL7NDR73' at box: 1"

The reverse engineering notes describe a variant whose destination is only 5
bytes (``dest (5) | source (6) | SSID octet | 03 | F0 | text``); it is kept as a
fallback because it was seen for a 5-character operator callsign.
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
# Standard AX.25 UI frame: two 7-byte addresses + control + PID.
_STD_HEADER_LEN = 16
_CONTROL_UI = 0x03
_PID_NONE = 0xF0


class Command(enum.Enum):
    """Uplink command kinds."""

    UPLOAD = "upload"
    CONFIRM = "confirm"
    PARROT = "parrot"
    DOWNLOAD = "download"


def _ascii_field(text: str, length: int, what: str, pad: bytes = b"\x00") -> bytes:
    """Encode *text* as upper-case ASCII, padded to *length* with *pad*."""
    value = text.strip().upper()
    if not value.isascii() or not value.isprintable():
        raise ValueError(f"{what} must be printable ASCII")
    if len(value) > length:
        raise ValueError(f"{what} is longer than {length} characters")
    return value.encode("ascii").ljust(length, pad)


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
        + _ascii_field(callsign, CALLSIGN_LEN, "callsign", pad=b" ")
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


def _with_ssid(call: str, ssid_octet: int) -> str:
    ssid = (ssid_octet >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


def _text(payload: bytes) -> str:
    return payload.decode("ascii", errors="replace").strip("\x00\r\n ")


def _parse_standard(payload: bytes) -> Downlink | None:
    """Standard AX.25 UI frame: two 7-byte addresses, control 03, PID F0."""
    if len(payload) < _STD_HEADER_LEN:
        return None
    # Address bytes have bit 0 clear, except the last SSID octet which ends the field.
    if any(b & 1 for b in payload[:6]) or any(b & 1 for b in payload[7:13]):
        return None
    if payload[6] & 1 or not payload[13] & 1:
        return None
    if payload[14] != _CONTROL_UI or payload[15] != _PID_NONE:
        return None
    return Downlink(
        dest=_with_ssid(_unshift(payload[:6]), payload[6]),
        source=_with_ssid(_unshift(payload[7:13]), payload[13]),
        ssid_octet=payload[13],
        text=_text(payload[_STD_HEADER_LEN:]),
    )


def _parse_short_dest(payload: bytes) -> Downlink | None:
    """Variant with a 5-byte destination (from the reverse engineering notes)."""
    if len(payload) < _HEADER_LEN:
        return None
    if payload[_HEADER_LEN - 2] != _CONTROL_UI or payload[_HEADER_LEN - 1] != _PID_NONE:
        return None
    return Downlink(
        dest=_unshift(payload[:_DEST_LEN]),
        source=_unshift(payload[_DEST_LEN : _DEST_LEN + _SRC_LEN]),
        ssid_octet=payload[_DEST_LEN + _SRC_LEN],
        text=_text(payload[_HEADER_LEN:]),
    )


def parse_downlink(payload: bytes) -> Downlink | None:
    """Parse a downlink frame; None if it matches neither known layout."""
    return _parse_standard(payload) or _parse_short_dest(payload)
