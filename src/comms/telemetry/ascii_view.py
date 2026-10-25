"""ASCII form of a received frame, the way satellite logs (e.g. JJ1WTK's) list a packet.

An AX.25 frame whose payload is text becomes ``SRC>DEST[,DIGI*...]:payload``; payload bytes
that are not printable ASCII are written ``<0xNN>`` (a trailing NUL shows as ``<0x00>``).

A frame whose payload is binary (HADES FSK, CSP, a binary AX.25 telemetry packet) has no
text form -- dotted ASCII is only noise. It is shown as a one-line summary of its first
decoded fields (``[PSU] battery: 7.4 V  temperature: 21 °C ...``), or ``(binary N bytes)``
when no definition decodes it. The text of an AX.25 row can be pasted into Help > ASCII Decode.
"""

from __future__ import annotations

import re

from comms.telemetry.decoder import TelemetryField, TelemetryFrame

_HEX_TEXT_RE = re.compile(r"[0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*")
_CALL_OK = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")
_MAX_ADDRESSES = 10
_NO_ASCII = "—"
# A payload is text when at least this share of its bytes (trailing NULs ignored) are printable.
_TEXT_RATIO = 0.85
# How many decoded fields a binary frame's summary lists, and which fields are never listed
# (protocol headers say nothing about the satellite).
_SUMMARY_FIELDS = 4
_SUMMARY_SKIP_PREFIXES = ("csp_", "ax25_")
# A text field in a summary (a message of a HADES frame, say) is cut to this many characters;
# bytes that do not decode to printable text show as ".".
_SUMMARY_TEXT_MAX = 40


def _printable(byte: int) -> bool:
    return 0x20 <= byte < 0x7F


def escaped_ascii(data: bytes) -> str:
    """*data* as text with every non-printable byte written ``<0xNN>``."""
    return "".join(chr(b) if _printable(b) else f"<0x{b:02X}>" for b in data)


def dotted_ascii(data: bytes) -> str:
    """*data* as text with ``.`` for every non-printable byte (a hex dump's ASCII column)."""
    return "".join(chr(b) if _printable(b) else "." for b in data)


def looks_like_text(data: bytes) -> bool:
    """True when *data* is (almost) all printable ASCII; empty counts as text."""
    body = data.rstrip(b"\x00\r\n")
    if not body:
        return True
    return sum(_printable(b) for b in body) / len(body) >= _TEXT_RATIO


def hex_text_to_bytes(text: str) -> bytes | None:
    """Bytes of a hex dump such as ``86 A2 40 ...``; None if *text* is anything else."""
    text = text.strip()
    if not _HEX_TEXT_RE.fullmatch(text):
        return None
    return bytes.fromhex("".join(text.split()))


def ax25_split(frame: bytes) -> tuple[str, bytes] | None:
    """(``SRC>DEST,DIGI*`` header text, info field) of an AX.25 UI frame, else None.

    *frame* has its FCS removed. The info field is what follows the control and PID bytes.
    """
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
    return f"{src}>{dest}{path}", frame[pos + 2 :]


def ax25_to_ascii(frame: bytes) -> str | None:
    """``SRC>DEST,DIGI*:payload`` for an AX.25 UI frame (FCS already removed), else None."""
    parts = ax25_split(frame)
    if parts is None:
        return None
    header, info = parts
    return f"{header}:{escaped_ascii(info)}"


def format_field_value(f: TelemetryField) -> str:
    """A decoded field's value as shown in the "Decoded Fields" tables."""
    if f.is_string:
        return f.unit
    value = f.scaled_value
    if f.is_integer:
        text = f"{round(value):,}"
    elif abs(value) >= 1000:
        text = f"{value:,.2f}"
    elif abs(value) >= 1:
        text = f"{value:.4f}"
    else:
        text = f"{value:.6f}"
    return f"{text} {f.unit}" if f.unit else text


def _summary_value(f: TelemetryField) -> str:
    """A field's value for a summary line: text fields are made printable and short."""
    value = format_field_value(f)
    if not f.is_string:
        return value
    clean = "".join(c if c.isprintable() and c != "\ufffd" else "." for c in value)
    return clean if len(clean) <= _SUMMARY_TEXT_MAX else clean[:_SUMMARY_TEXT_MAX] + "…"


def summary_text(frame: TelemetryFrame | None, limit: int = _SUMMARY_FIELDS) -> str | None:
    """A one-line summary of *frame*'s first decoded fields, or None if it has none.

    ``[PSU] label: value  label: value ...``; protocol-header fields (CSP, AX.25) are skipped.
    """
    if frame is None or not frame.has_fields:
        return None
    shown = [f for f in frame.fields if not f.name.startswith(_SUMMARY_SKIP_PREFIXES)][:limit]
    if not shown:
        return None
    body = "  ".join(f"{f.label}: {_summary_value(f)}" for f in shown)
    kind = frame.telemetry_label or (str(frame.telemetry_id) if frame.telemetry_id else "")
    return f"[{kind}] {body}" if kind else body


def _binary_text(size: int, summary: str | None) -> str:
    return summary or f"(binary {size} bytes)"


def describe_parts(header: str | None, info: bytes, summary: str | None = None) -> str:
    """The ASCII View text for a frame given as an optional AX.25 *header* and its *info* bytes.

    Text payloads are written out; binary ones become the *summary* (or a size note).
    """
    if looks_like_text(info):
        return f"{header}:{escaped_ascii(info)}" if header is not None else dotted_ascii(info)
    body = _binary_text(len(info), summary)
    return f"{header}: {body}" if header is not None else body


def frame_ascii(raw: bytes, summary: str | None = None) -> str:
    """The ASCII View text of *raw*: the log line for AX.25 text, else the binary summary."""
    parts = ax25_split(raw)
    if parts is None:
        return describe_parts(None, raw, summary)
    return describe_parts(parts[0], parts[1], summary)


def row_ascii(raw: bytes | None, shown_text: str, summary: str | None = None) -> str:
    """The ASCII View text of one Received Frames row.

    *raw* is the frame's bytes when the row kept them; otherwise *shown_text* (the row's
    Data column) is used if it is a plain hex dump. *summary* (a decoded frame's
    :func:`summary_text`) stands in for a binary payload. A row with neither bytes nor
    summary (a CW frame's ``[HK1] ...`` text that did not decode) shows a dash.
    """
    if not raw:  # None, or a row that kept no bytes (gr-satellites without a raw frame)
        raw = hex_text_to_bytes(shown_text)
    if not raw:
        return summary or _NO_ASCII
    return frame_ascii(raw, summary)


def addressed_ascii(addresses: list[str], info: bytes, summary: str | None = None) -> str | None:
    """The ASCII View text from already-split addresses (destination first) and the info field.

    gr-satellites prints a decoded AX.25 packet as separate addresses and info bytes; this
    puts them back into the log form. None when there is no destination and source.
    """
    if len(addresses) < 2:
        return None
    path = "".join("," + a for a in addresses[2:])
    return describe_parts(f"{addresses[1]}>{addresses[0]}{path}", info, summary)
