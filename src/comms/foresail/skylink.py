"""Skylink frames and the AX.25 frames of Foresail-1p's repeater channel (VC 3).

Foresail-1p (Aalto University, OH2F1S) talks Skylink, a protocol of its own, on
437.125 MHz. A Skylink frame is::

    [version<<3 | identity length] [identity] [flags] [sequence u16 BE] [extension length]
    [extension headers] [payload] [authentication, 4 B, if flagged] [frame CRC-32, 4 B, if flagged]

``flags``: bits 0-1 virtual channel (VC), bit 2 ARQ on, bit 3 authenticated, bit 4 frame
CRC, bits 5-6 sequence control. VC 0/1 carry CCSDS PUS telemetry/commands (authenticated,
not usable by other stations); **VC 3 is the amateur packet repeater** and carries AX.25
UI frames, unauthenticated (ICD "FS1p Space/Ground Interface Control Sheet" section 4, the
SatNOGS ``foresail1p.ksy`` and https://github.com/aaltosatellite/skylink).

The repeater's AX.25 frames are *not* standard, because of unpatchable onboard bugs: there
are no HDLC flags or bit stuffing, the FCS is CRC-16/X.25 sent most significant byte first
(and in the satellite's repeats only the second byte is right), and a Skylink frame CRC must
not be set on frames sent to the satellite.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from comms.aprs.g3ruh_tx import fcs16

SATELLITE_IDENTITY = "OH2F1S"
PROTOCOL_VERSION = 12  # the 5 version bits: first byte 0x63..0x67 = (12 << 3) | identity length
MAX_IDENTITY_LEN = 7
VC_COMMAND = 0
VC_DATA = 1
VC_SERVICE = 2
VC_REPEATER = 3

# Extension header types (low nibble of an extension's first byte; the high nibble is the
# length of its data).
EXT_ARQ_SEQUENCE = 0
EXT_ARQ_REQUEST = 1
EXT_ARQ_CONTROL = 2
EXT_ARQ_HANDSHAKE = 3
EXT_TDD_CONTROL = 4
EXT_HMAC_SEQUENCE_RESET = 5

# ICD: the satellite repeats frames addressed to these destinations (``*`` = any suffix).
ACCEPTED_DESTINATIONS = ("OH2F1S", "APRS", "ALL", "BEACON", "CQ")
CONTROL_UI = 0x03
PID_NO_LAYER3 = 0xF0
# ICD 4.2: the whole AX.25 frame (header, information field and FCS) is at most 128 bytes.
MAX_AX25_FRAME_LEN = 128


@dataclass(frozen=True)
class SkylinkExtension:
    """One extension header of a Skylink frame."""

    type: int
    data: bytes


@dataclass(frozen=True)
class SkylinkFrame:
    """A parsed Skylink frame."""

    identity: str
    version: int
    vc: int
    arq_on: bool
    authenticated: bool
    crced: bool
    sequence_control: int
    frame_sequence: int
    extensions: tuple[SkylinkExtension, ...]
    payload: bytes
    auth: bytes | None
    frame_crc: bytes | None
    crc_ok: bool | None  # None when the frame carries no CRC
    raw: bytes = field(repr=False, default=b"")

    @property
    def tdd(self) -> tuple[int, int] | None:
        """``(window_ms, remaining_ms)`` of the TDD control extension, if the frame has one.

        ``window`` is the sender's transmit window length and ``remaining`` how much of it is
        left (both in milliseconds).
        """
        for ext in self.extensions:
            if ext.type == EXT_TDD_CONTROL and len(ext.data) == 4:
                return int.from_bytes(ext.data[:2], "big"), int.from_bytes(ext.data[2:], "big")
        return None


def _parse_extensions(block: bytes) -> tuple[SkylinkExtension, ...] | None:
    out: list[SkylinkExtension] = []
    pos = 0
    while pos < len(block):
        length = block[pos] >> 4
        ext_type = block[pos] & 0x0F
        data = block[pos + 1 : pos + 1 + length]
        if len(data) != length:
            return None
        out.append(SkylinkExtension(ext_type, bytes(data)))
        pos += 1 + length
    return tuple(out)


def skylink_crc32(data: bytes) -> int:
    """The Skylink frame CRC-32 (standard CRC-32, init and XOR-out 0xFFFFFFFF)."""
    return zlib.crc32(data) & 0xFFFFFFFF


def parse_skylink_frame(raw: bytes) -> SkylinkFrame | None:
    """Parse *raw* (the payload of a decoded ASM frame), or None if it is not Skylink."""
    if len(raw) < 6:
        return None
    version = raw[0] >> 3
    ident_len = raw[0] & 0x07
    if version != PROTOCOL_VERSION or not 1 <= ident_len <= MAX_IDENTITY_LEN:
        return None
    pos = 1 + ident_len
    if len(raw) < pos + 4:
        return None
    identity = raw[1:pos].decode("ascii", errors="replace")
    flags = raw[pos]
    sequence = int.from_bytes(raw[pos + 1 : pos + 3], "big")
    ext_len = raw[pos + 3]
    body_start = pos + 4 + ext_len
    if len(raw) < body_start:
        return None
    extensions = _parse_extensions(raw[pos + 4 : body_start])
    if extensions is None:
        return None
    payload = raw[body_start:]
    authenticated = bool(flags & 0x08)
    crced = bool(flags & 0x10)
    frame_crc: bytes | None = None
    crc_ok: bool | None = None
    if crced and len(payload) >= 4:
        frame_crc = bytes(payload[-4:])
        crc_ok = skylink_crc32(raw[:-4]) == int.from_bytes(frame_crc, "big")
        payload = payload[:-4]
    auth: bytes | None = None
    if authenticated and len(payload) >= 4:
        auth = bytes(payload[-4:])
        payload = payload[:-4]
    return SkylinkFrame(
        identity=identity,
        version=version,
        vc=flags & 0x03,
        arq_on=bool(flags & 0x04),
        authenticated=authenticated,
        crced=crced,
        sequence_control=(flags >> 5) & 0x03,
        frame_sequence=sequence,
        extensions=extensions,
        payload=bytes(payload),
        auth=auth,
        frame_crc=frame_crc,
        crc_ok=crc_ok,
        raw=raw,
    )


def build_skylink_frame(
    payload: bytes,
    *,
    vc: int = VC_REPEATER,
    identity: str = "PyGS",
    sequence: int = 0,
    extensions: tuple[SkylinkExtension, ...] = (),
) -> bytes:
    """A Skylink frame for the satellite: unauthenticated, no ARQ, **no frame CRC**.

    The onboard CRC check is broken, so a frame with the CRC flag is not accepted. *identity*
    must differ from ``OH2F1S`` (a node drops frames carrying its own identity); the
    SatNOGS/Aalto test tools use ``PyGS``. The default carries no extension headers.
    """
    ident = identity.encode("ascii")
    if not 1 <= len(ident) <= MAX_IDENTITY_LEN:
        raise ValueError(f"identity must be 1-{MAX_IDENTITY_LEN} ASCII characters")
    if identity.upper() == SATELLITE_IDENTITY:
        raise ValueError("identity must differ from the satellite's")
    if not 0 <= vc <= 3:
        raise ValueError("vc must be 0-3")
    ext = b"".join(bytes([(len(e.data) << 4) | e.type]) + e.data for e in extensions)
    if len(ext) > 255:
        raise ValueError("extension headers too long")
    header = bytes([(PROTOCOL_VERSION << 3) | len(ident)]) + ident
    header += bytes([vc]) + (sequence & 0xFFFF).to_bytes(2, "big") + bytes([len(ext)])
    return header + ext + payload


def tdd_extension(window_ms: int, remaining_ms: int) -> SkylinkExtension:
    """A TDD control extension header (window length and time remaining, in ms)."""
    return SkylinkExtension(
        EXT_TDD_CONTROL,
        (window_ms & 0xFFFF).to_bytes(2, "big") + (remaining_ms & 0xFFFF).to_bytes(2, "big"),
    )


# ---------------------------------------------------------------------------
# AX.25 UI frames of the repeater channel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ax25Address:
    """An AX.25 address: callsign, SSID and (for digipeaters) the has-been-repeated bit."""

    callsign: str
    ssid: int = 0
    repeated: bool = False

    def __str__(self) -> str:
        text = self.callsign if self.ssid == 0 else f"{self.callsign}-{self.ssid}"
        return text + ("*" if self.repeated else "")


@dataclass(frozen=True)
class RepeaterFrame:
    """The AX.25 UI frame inside a VC 3 Skylink frame."""

    destination: Ax25Address
    source: Ax25Address
    digipeaters: tuple[Ax25Address, ...]
    control: int
    pid: int
    info: bytes
    fcs: bytes
    fcs_status: str  # "ok", "second-byte-ok" (the satellite's repeats) or "bad"

    @property
    def via(self) -> str:
        """The digipeater path as text (``*`` marks a station that has repeated the frame)."""
        return ",".join(str(d) for d in self.digipeaters)

    @property
    def text(self) -> str:
        """The information field as text (non-printable bytes shown as ``.``)."""
        return "".join(chr(b) if 32 <= b < 127 else "." for b in self.info)


def encode_ax25_address(callsign: str, ssid: int = 0, *, last: bool = False) -> bytes:
    """7 bytes: the callsign (space padded to 6) shifted left one bit, then the SSID byte."""
    call = callsign.upper().strip()
    if not call or len(call) > 6 or not all(c.isalnum() for c in call):
        raise ValueError(f"invalid callsign {callsign!r} (1-6 letters or digits)")
    if not 0 <= ssid <= 15:
        raise ValueError("SSID must be 0-15")
    shifted = bytes(ord(c) << 1 for c in call.ljust(6))
    return shifted + bytes([0x60 | (ssid << 1) | (1 if last else 0)])


def _decode_address(chunk: bytes) -> tuple[Ax25Address, bool]:
    call = bytes(b >> 1 for b in chunk[:6]).decode("ascii", errors="replace").strip()
    ssid_byte = chunk[6]
    return Ax25Address(call, (ssid_byte >> 1) & 0x0F, bool(ssid_byte & 0x80)), bool(ssid_byte & 1)


def parse_repeater_frame(payload: bytes) -> RepeaterFrame | None:
    """Parse the AX.25 UI frame of a VC 3 payload (no flags; the last 2 bytes are the FCS)."""
    if len(payload) < 14 + 2 + 2:
        return None
    addresses: list[Ax25Address] = []
    pos = 0
    while True:
        if pos + 7 > len(payload):
            return None
        address, last = _decode_address(payload[pos : pos + 7])
        addresses.append(address)
        pos += 7
        if last:
            break
        if len(addresses) >= 10:
            return None
    if len(addresses) < 2 or pos + 2 + 2 > len(payload):
        return None
    control, pid = payload[pos], payload[pos + 1]
    info = payload[pos + 2 : -2]
    fcs = payload[-2:]
    expected = fcs16(payload[:-2]).to_bytes(2, "big")
    if bytes(fcs) == expected:
        status = "ok"
    elif fcs[1] == expected[1]:
        status = "second-byte-ok"
    else:
        status = "bad"
    return RepeaterFrame(
        destination=addresses[0],
        source=addresses[1],
        digipeaters=tuple(addresses[2:]),
        control=control,
        pid=pid,
        info=bytes(info),
        fcs=bytes(fcs),
        fcs_status=status,
    )


def max_info_length(digipeaters: int = 0) -> int:
    """The longest information field that fits the satellite's 128-byte AX.25 limit."""
    return MAX_AX25_FRAME_LEN - (14 + 7 * digipeaters) - 2 - 2


def build_repeater_frame(
    destination: str,
    source: str,
    info: bytes,
    digipeaters: tuple[str, ...] = (),
) -> bytes:
    """An AX.25 UI frame as the satellite expects it: no flags, FCS (CRC-16/X.25) MSB first.

    *destination* and *source* may carry an SSID as ``CALL-N``; up to 8 digipeaters.
    """
    if len(digipeaters) > 8:
        raise ValueError("at most 8 digipeaters")
    if len(info) > max_info_length(len(digipeaters)):
        raise ValueError(f"information field too long (max {max_info_length(len(digipeaters))})")

    def split(text: str) -> tuple[str, int]:
        call, _sep, ssid = text.strip().upper().partition("-")
        return call, int(ssid) if ssid else 0

    dest_call, dest_ssid = split(destination)
    src_call, src_ssid = split(source)
    frame = bytearray(encode_ax25_address(dest_call, dest_ssid))
    frame += encode_ax25_address(src_call, src_ssid, last=not digipeaters)
    for i, digi in enumerate(digipeaters):
        call, ssid = split(digi)
        frame += encode_ax25_address(call, ssid, last=i == len(digipeaters) - 1)
    frame += bytes([CONTROL_UI, PID_NO_LAYER3]) + info
    return bytes(frame) + fcs16(bytes(frame)).to_bytes(2, "big")
