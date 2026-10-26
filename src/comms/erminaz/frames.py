"""ERMINAZ-2 digipeater frames: the CCSDS TC uplink and the APID 600 downlink.

The uplink layout is read from the ground station of DL7NDR (the GNU Radio flowgraph posted at
https://community.libre.space/t/erminaz-2-digipeaters/15503) and from the satellite firmware
(gitlab.com/amsat-dl/erminaz/erminaz-comms-sw, ``digipeater.c`` / ``telemetry.c`` / ``conf.h``).
It has not been checked on the air.

A TC transfer frame (bypass, no sequence control) carries one space packet with APID 600::

    20 <scid> 04 <len-1> 00          TC primary header (version 0, bypass, SCID, VCID 1, length)
    c6                               segment header
    12 58 c0 00 <len-1 of packet>    space packet header (TC, APID 600, unsegmented)
    <radio config u16> <to 6> <from 6> <message> 00
    <CRC-16/CCITT-FALSE over everything before>

The downlink is a 223 octet TM transfer frame whose space packet (at offset 6) holds the same
``<radio config> <to> <from> <message>`` data.
"""

from __future__ import annotations

from dataclasses import dataclass

# SANA spacecraft IDs and the catalogue numbers this app uses.
SPACECRAFT: dict[str, tuple[int, int]] = {
    "ERMINAZ-2U": (102, 98207),
    "ERMINAZ-2V": (100, 98208),
    "ERMINAZ-2X": (79, 98209),
}
NORAD_IDS = frozenset(norad for _scid, norad in SPACECRAFT.values())

APID_DIGIPEATER = 600
VCID_USER = 1
CALL_SIZE = 6
# Radio configuration the satellite repeats with (and its own beacon uses): PA on, power index 5,
# FSK, RS coding, 9600 baud. The value DL7NDR's ground station sends.
DEFAULT_RADIO_CONF = 0xA80C
# The firmware buffer holds 81 octets: conf (2) + two callsigns (12) + message + NUL.
MAX_MESSAGE_LENGTH = 81 - 2 - 2 * CALL_SIZE - 1

_TX_POWER_LUT = (-10, -8, -6, -4, -2, 0, 2, 4, 6, 8, 10, 11, 12, 13, 14, 15)
_BAUDS = (600, 1200, 2400, 4800, 9600, 19200, 38400, 125000)
_ENCODINGS = ("raw", "RS", "CC 1/2 + RS", "SSDV")
_MODULATIONS = ("FSK", "BPSK", "BPSK (residual carrier)", "QPSK", "RILDOS", "CW")


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (init 0xFFFF, poly 0x1021, no reflection)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _bit in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def describe_radio_conf(word: int) -> str:
    """The radio settings word the satellite retransmits with, as text."""
    baud = _BAUDS[word & 0x7]
    enc = _ENCODINGS[(word >> 3) & 0x3]
    mod_index = (word >> 5) & 0x7
    mod = _MODULATIONS[mod_index] if mod_index < len(_MODULATIONS) else f"mod {mod_index}"
    power = _TX_POWER_LUT[(word >> 11) & 0xF]
    pa = "PA on" if word >> 15 & 1 else "PA off"
    return f"{baud} baud {mod} {enc}, {power} dBm, {pa}"


def encode_callsign(call: str) -> bytes:
    """A callsign as 6 ASCII octets, NUL padded (the SSID is dropped: the field has no room)."""
    base = call.strip().upper().split("-")[0]
    if not base or len(base) > CALL_SIZE or not base.isascii() or not base.isprintable():
        raise ValueError(f"callsign must be 1-{CALL_SIZE} ASCII characters: {call!r}")
    return base.encode("ascii").ljust(CALL_SIZE, b"\x00")


def build_digipeater_tc_frame(
    scid: int,
    destination: str,
    source: str,
    message: str,
    radio_conf: int = DEFAULT_RADIO_CONF,
) -> bytes:
    """The TC transfer frame (with CRC) that makes the satellite repeat *message*."""
    if not 0 <= scid <= 0x3FF:
        raise ValueError(f"spacecraft ID out of range: {scid}")
    try:
        text = message.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("message must be ASCII") from exc
    if b"\x00" in text:
        raise ValueError("message must not contain NUL")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"message too long ({len(text)} > {MAX_MESSAGE_LENGTH})")
    data = (
        radio_conf.to_bytes(2, "big")
        + encode_callsign(destination)
        + encode_callsign(source)
        + text
        + b"\x00"
    )
    packet = (0x1000 | APID_DIGIPEATER).to_bytes(2, "big") + b"\xc0\x00"
    packet += (len(data) - 1).to_bytes(2, "big") + data
    # primary header (5) + segment header (1) + packet + CRC (2)
    total = 5 + 1 + len(packet) + 2
    length_field = total - 1
    frame = (
        (0x2000 | scid).to_bytes(2, "big")
        + bytes([(VCID_USER << 2) | (length_field >> 8), length_field & 0xFF, 0x00, 0xC6])
        + packet
    )
    return frame + crc16_ccitt_false(frame).to_bytes(2, "big")


@dataclass(frozen=True)
class DigipeaterMessage:
    """A message of a downlink APID 600 packet."""

    spacecraft_id: int
    radio_conf: int
    destination: str
    source: str
    text: str


def _call(raw: bytes) -> str:
    return "".join(chr(b) if 0x20 < b < 0x7F else "" for b in raw)


def parse_digipeater_frame(frame: bytes) -> DigipeaterMessage | None:
    """Read the digipeater message of a 223 octet TM frame, or None if it holds another packet."""
    if len(frame) < 6 + 6 + 2 + 2 * CALL_SIZE:
        return None
    if int.from_bytes(frame[6:8], "big") & 0x7FF != APID_DIGIPEATER:
        return None
    length = int.from_bytes(frame[10:12], "big") + 1
    data = frame[12 : 12 + length]
    if len(data) < 2 + 2 * CALL_SIZE:
        return None
    body = data[2 + 2 * CALL_SIZE :]
    text = body.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return DigipeaterMessage(
        spacecraft_id=(int.from_bytes(frame[0:2], "big") >> 4) & 0x3FF,
        radio_conf=int.from_bytes(data[0:2], "big"),
        destination=_call(data[2 : 2 + CALL_SIZE]),
        source=_call(data[2 + CALL_SIZE : 2 + 2 * CALL_SIZE]),
        text=text,
    )
