"""ASCII form of a received frame, the way satellite logs (e.g. JJ1WTK's) list a packet.

An AX.25 frame becomes ``SRC>DEST[,DIGI*...]:payload``; payload bytes that are not printable
ASCII are written ``<0xNN>`` (a trailing NUL shows as ``<0x00>``). Anything else (a raw
CSP/HADES/AX100 frame) is shown with a ``.`` for each non-printable byte, like the ASCII
column of a hex dump. The text of an AX.25 row can be pasted into Help > ASCII Decode.
"""

from __future__ import annotations

import re

_HEX_TEXT_RE = re.compile(r"[0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*")
_CALL_OK = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")
_MAX_ADDRESSES = 10
_NO_ASCII = "—"


def _printable(byte: int) -> bool:
    return 0x20 <= byte < 0x7F


def escaped_ascii(data: bytes) -> str:
    """*data* as text with every non-printable byte written ``<0xNN>``."""
    return "".join(chr(b) if _printable(b) else f"<0x{b:02X}>" for b in data)


def dotted_ascii(data: bytes) -> str:
    """*data* as text with ``.`` for every non-printable byte (a hex dump's ASCII column)."""
    return "".join(chr(b) if _printable(b) else "." for b in data)


def hex_text_to_bytes(text: str) -> bytes | None:
    """Bytes of a hex dump such as ``86 A2 40 ...``; None if *text* is anything else."""
    text = text.strip()
    if not _HEX_TEXT_RE.fullmatch(text):
        return None
    return bytes.fromhex("".join(text.split()))


def ax25_to_ascii(frame: bytes) -> str | None:
    """``SRC>DEST,DIGI*:payload`` for an AX.25 UI frame (FCS already removed), else None."""
    addresses: list[tuple[str, bool]] = []
    pos = 0
    while len(addresses) < _MAX_ADDRESSES:
        field = frame[pos : pos + 7]
        if len(field) < 7:
            return None
        if not all((b >> 1) in _CALL_OK for b in field[:6]):
            return None
        call = bytes(b >> 1 for b in field[:6]).decode("ascii").strip()
        if not call:
            return None
        ssid = (field[6] >> 1) & 0x0F
        addresses.append((f"{call}-{ssid}" if ssid else call, bool(field[6] & 0x80)))
        pos += 7
        if field[6] & 0x01:
            break
    else:
        return None
    if len(addresses) < 2 or frame[pos : pos + 1] != b"\x03" or len(frame) < pos + 2:
        return None  # not a UI frame with a PID byte
    dest, src = addresses[0][0], addresses[1][0]
    path = "".join("," + call + ("*" if repeated else "") for call, repeated in addresses[2:])
    return f"{src}>{dest}{path}:{escaped_ascii(frame[pos + 2 :])}"


def frame_ascii(raw: bytes) -> str:
    """The ASCII form of *raw*: AX.25 header form when it is a UI frame, else a dotted dump."""
    return ax25_to_ascii(raw) or dotted_ascii(raw)


def row_ascii(raw: bytes | None, shown_text: str) -> str:
    """The ASCII form of one Received Frames row.

    *raw* is the frame's bytes when the row kept them; otherwise *shown_text* (the row's
    Data column) is used if it is a plain hex dump. Rows that are neither (a CW frame's
    ``[HK1] ...`` text) show a dash.
    """
    if not raw:  # None, or a row that kept no bytes (gr-satellites without a raw frame)
        raw = hex_text_to_bytes(shown_text)
    if not raw:
        return _NO_ASCII
    return frame_ascii(raw)


def addressed_ascii(addresses: list[str], info: bytes) -> str | None:
    """``SRC>DEST,DIGI:info`` from already-split addresses (destination first) and the info field.

    gr-satellites prints a decoded AX.25 packet as separate addresses and info bytes; this
    puts them back into the log form. None when there is no destination and source.
    """
    if len(addresses) < 2:
        return None
    path = "".join("," + a for a in addresses[2:])
    return f"{addresses[1]}>{addresses[0]}{path}:{escaped_ascii(info)}"
