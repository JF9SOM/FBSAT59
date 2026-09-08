"""AX.25 frame decoder and APRS payload parser.

Handles the minimum APRS data types needed for satellite operations:
  - Position (! = @ /)
  - Message (:)
  - Status (>)
  - Raw hex fallback for unknown types

Reference: APRS Protocol Reference v1.0 (www.aprs.org/doc/APRS101.PDF)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# AX.25 constants
# ---------------------------------------------------------------------------

_UI_FRAME_CTRL = 0x03
_NO_LAYER3_PID = 0xF0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Ax25Frame:
    """Decoded AX.25 UI frame."""

    dest: str
    src: str
    via: list[str]
    pid: int
    payload: bytes

    @property
    def via_str(self) -> str:
        return ",".join(self.via)


@dataclass
class AprsPacket:
    """Decoded APRS packet derived from an AX.25 frame."""

    callsign: str  # source callsign (e.g. "JA1XYZ-9")
    dest: str  # destination (e.g. "APRS")
    via: str  # digipeater path (e.g. "ARISS*")
    data_type: str  # single-char APRS data-type identifier
    raw_info: str  # raw information field (UTF-8 best-effort)
    comment: str  # short summary (legacy: half-decoded, used for DB / ADIF / TX echo)
    plain: str = ""  # full plain-language line for the APRS tab's "Plain" view
    latitude: float | None = None
    longitude: float | None = None
    message_addressee: str | None = None
    message_text: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AX.25 decoder
# ---------------------------------------------------------------------------


def decode_ax25(data: bytes) -> Ax25Frame | None:
    """Decode a raw AX.25 UI frame from *data*.

    Returns None when the frame is malformed or not a UI frame.
    """
    if len(data) < 16:
        return None

    offset = 0

    def _decode_addr(buf: bytes, pos: int) -> tuple[str, bool]:
        """Decode one 7-byte AX.25 address field.

        Returns (callsign-ssid, has_more) where has_more is False when the
        LSB of the last byte is 1 (end-of-address-field marker).
        """
        chars = "".join(chr(b >> 1) for b in buf[pos : pos + 6]).rstrip()
        ssid = (buf[pos + 6] >> 1) & 0x0F
        end = bool(buf[pos + 6] & 0x01)
        label = f"{chars}-{ssid}" if ssid else chars
        return label, not end

    dest, _ = _decode_addr(data, offset)
    offset += 7
    src, has_more = _decode_addr(data, offset)
    offset += 7

    via: list[str] = []
    while has_more and offset + 7 <= len(data):
        rep, has_more = _decode_addr(data, offset)
        # Mark repeated digipeaters (H-bit set in SSID byte)
        h_bit = bool(data[offset + 6] & 0x80)
        via.append(rep + ("*" if h_bit else ""))
        offset += 7

    # Control + PID
    if offset + 2 > len(data):
        return None
    ctrl = data[offset]
    pid = data[offset + 1]
    offset += 2

    if ctrl != _UI_FRAME_CTRL:
        return None  # not a UI frame

    payload = data[offset:]
    return Ax25Frame(dest=dest, src=src, via=via, pid=pid, payload=payload)


# ---------------------------------------------------------------------------
# APRS payload parser
# ---------------------------------------------------------------------------

# Compressed / uncompressed position regex (DDMM.mmN/DDDMM.mmE)
_POS_RE = re.compile(r"(\d{2})(\d{2}\.\d+)([NS])(.)(\d{3})(\d{2}\.\d+)([EW])")


def _parse_position(info: str) -> tuple[float | None, float | None]:
    """Extract lat/lon from an uncompressed APRS position string."""
    m = _POS_RE.search(info)
    if not m:
        return None, None
    lat_deg = float(m.group(1)) + float(m.group(2)) / 60.0
    if m.group(3) == "S":
        lat_deg = -lat_deg
    lon_deg = float(m.group(5)) + float(m.group(6)) / 60.0
    if m.group(7) == "W":
        lon_deg = -lon_deg
    return lat_deg, lon_deg


def parse_aprs(frame: Ax25Frame) -> AprsPacket:
    """Parse an AX.25 frame into an AprsPacket.

    Never raises — unknown / malformed payloads fall back to raw hex display.
    """
    via_str = ",".join(frame.via)
    try:
        info = frame.payload.decode("utf-8", errors="replace")
    except Exception:
        info = frame.payload.hex()

    if not info:
        raw_hex = f"[raw] {frame.payload.hex()}"
        return AprsPacket(
            callsign=frame.src,
            dest=frame.dest,
            via=via_str,
            data_type="?",
            raw_info=info,
            comment=raw_hex,
            plain=raw_hex,
        )

    data_type = info[0]
    lat: float | None = None
    lon: float | None = None
    msg_addr: str | None = None
    msg_text: str | None = None
    comment = info

    # -- Position reports --
    if data_type in ("!", "=", "@", "/"):
        lat, lon = _parse_position(info[1:])
        comment = re.sub(r"^[\d./NSEWnsew]+", "", info[1:]).strip()
        if lat is not None:
            comment = f"Pos {lat:.4f},{lon:.4f}  {comment}"

    # -- Message --
    elif data_type == ":":
        # :CALLSIGN :message text{seq}
        body = info[1:]
        if len(body) >= 10 and body[9] == ":":
            msg_addr = body[:9].strip()
            msg_text = body[10:]
            # strip optional message ID {nnn}
            msg_text = re.sub(r"\{[0-9A-Za-z]+\}$", "", msg_text).strip()
            comment = f"MSG→{msg_addr}: {msg_text}"

    # -- Status --
    elif data_type == ">":
        comment = info[1:].strip()

    # -- Object --
    elif data_type == ";":
        comment = info[1:10].strip() + ": " + info[10:].strip()

    # -- Mic-E / Telemetry / Weather: show raw --
    else:
        comment = f"[{data_type}] {info[1:]}"

    # Full plain-language rendering for the APRS tab's "Plain" view. Imported
    # lazily (and only here, never at module load) so the _() calls in
    # humanize's label tables bind to the language set at startup, not to
    # whatever was active when this module first imported (see docs/i18n.md
    # pitfall #2). Falls back to the raw info field when it can't help.
    plain = info
    try:
        from comms.aprs.humanize import humanize_frame

        rendered = humanize_frame(frame)
        if rendered:
            plain = rendered
    except Exception:
        pass

    return AprsPacket(
        callsign=frame.src,
        dest=frame.dest,
        via=via_str,
        data_type=data_type,
        raw_info=info,
        comment=comment,
        plain=plain,
        latitude=lat,
        longitude=lon,
        message_addressee=msg_addr,
        message_text=msg_text,
    )
