"""Telemetry frame decoder.

Loads JSON format definitions from src/data/telemetry_formats/{norad}.json
and decodes raw AX.25 payloads into structured key-value dictionaries.

For satellites with no definition file the raw payload is returned as hex.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from i18n import _, get_language

# ---------------------------------------------------------------------------
# Format definition loader
# ---------------------------------------------------------------------------


def _formats_dir() -> Path:
    """Return the telemetry_formats directory (works frozen and dev)."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).parent.parent.parent  # src/
    return base / "data" / "telemetry_formats"


def _localize(node: Any, lang: str) -> Any:
    """Pick the text of *lang* in a format definition.

    A definition keeps its English text under the usual key (``label``) and the Japanese
    text next to it under ``<key>_ja`` (``label_ja``). English is used everywhere except in
    the Japanese UI, so the English UI can never show Japanese. The ``*_ja`` keys are
    consumed here; the code that reads a definition never sees them.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.endswith("_ja"):
                continue
            twin = node.get(key + "_ja")
            out[key] = _localize(twin if lang == "ja" and twin is not None else value, lang)
        return out
    if isinstance(node, list):
        return [_localize(item, lang) for item in node]
    return node


def load_format(norad: int) -> dict[str, Any] | None:
    """Load the JSON format definition for *norad*, or None if not found.

    Texts are chosen for the current UI language (see _localize()).
    """
    path = _formats_dir() / f"{norad}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data: dict[str, Any] = _localize(json.load(f), get_language())
            return data
    except Exception:
        return None


def list_formats() -> list[dict[str, Any]]:
    """Return all available format definitions (texts for the current UI language)."""
    results: list[dict[str, Any]] = []
    fmt_dir = _formats_dir()
    if not fmt_dir.exists():
        return results
    lang = get_language()
    for p in sorted(fmt_dir.glob("*.json")):
        try:
            with p.open(encoding="utf-8") as f:
                results.append(_localize(json.load(f), lang))
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TelemetryField:
    """One decoded telemetry field."""

    name: str
    label: str
    raw_value: int | float
    scaled_value: float
    unit: str
    is_integer: bool = False
    # True for a fundamentally textual value (an ASCII "ascii"-type binary
    # field, or a "str"-type CSV field, e.g. a callsign) — the display
    # string then lives in `unit` (raw_value/scaled_value are unused
    # placeholders), so the UI must not run it through numeric formatting.
    is_string: bool = False


@dataclass
class TelemetryFrame:
    """Decoded telemetry packet."""

    norad: int | None
    callsign: str
    satellite_name: str
    raw_hex: str
    fields: list[TelemetryField] = field(default_factory=list)
    signal_db: float | None = None
    telemetry_id: int | str | None = None
    telemetry_label: str = ""
    # For a frame carrying several records (see the ``records`` schema), the id of every
    # record found, in order; ``telemetry_id`` is then the first of them.
    record_ids: list[str] = field(default_factory=list)

    @property
    def has_fields(self) -> bool:
        return bool(self.fields)

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        if not self.fields:
            shown_hex = self.raw_hex[:40]
            text = f"[raw] {shown_hex}"
            if len(shown_hex) < len(self.raw_hex):
                # Flag the truncation explicitly — a lone "[raw] <hex>" line
                # otherwise looks like the whole received frame, when this
                # payload's full data (used in full by the Decoded Fields
                # tab, once a telemetry_ids format exists for it) may run
                # to hundreds of bytes.
                note = _("({shown}/{total} hex chars — rest omitted)").format(
                    shown=len(shown_hex), total=len(self.raw_hex)
                )
                text += f" {note}"
            return text
        parts = [
            f"{f.label}: {f.unit}" if f.is_string else f"{f.label}: {f.scaled_value:.2f}{f.unit}"
            for f in self.fields[:4]
        ]
        return "  ".join(parts)


# ---------------------------------------------------------------------------
# Type parsers
# ---------------------------------------------------------------------------

_STRUCT_MAP: dict[str, str] = {
    "uint8": ">B",
    "int8": ">b",
    "uint16_be": ">H",
    "int16_be": ">h",
    "uint16_le": "<H",
    "int16_le": "<h",
    "uint32_be": ">I",
    "uint32_le": "<I",
    "int32_le": "<i",
    "int32_be": ">i",
    "uint64_le": "<Q",
    "float32_be": ">f",
    "float32_le": "<f",
    "float64_be": ">d",
}

_FLOAT_TYPES = {"float32_be", "float32_le", "float64_be"}


def _text_field(field_def: dict[str, Any], label: str, text: str) -> TelemetryField:
    """A textual field: the display string lives in ``unit`` (see TelemetryField.is_string)."""
    return TelemetryField(
        name=field_def["name"],
        label=label,
        raw_value=0,
        scaled_value=0.0,
        unit=text,
        is_string=True,
    )


# A 12-bit quantized quaternion component covers [-1/sqrt(2), +1/sqrt(2)].
_QUAT_HALF_RANGE = 1.0 / math.sqrt(2.0)


def _decode_quat5(chunk: bytes, field_def: dict[str, Any], label: str) -> TelemetryField | None:
    """One value of a 5-byte "smallest three" compressed quaternion (FORESAIL-1p's ADCS).

    Bytes 0-3 are a little-endian u32 ``low`` and byte 4 is ``high``; the 40-bit number
    ``(low << 8) | high`` holds three 12-bit components (bits 39-28, 27-16, 15-4) and a 4-bit
    tail: bits 3-2 = index of the omitted (largest) component, bit 1 = its sign (1 = negative).
    ``field_def["component"]`` picks ``q1``/``q2``/``q3`` (each mapped from 0..4095 onto
    +-1/sqrt(2)), ``index``, ``sign`` or ``largest`` (the omitted component, computed so the
    quaternion has unit length). This is the SatNOGS ``foresail1p.ksy`` layout.
    """
    if len(chunk) < 5:
        return None
    combined = (int.from_bytes(chunk[:4], "little") << 8) | chunk[4]
    raws = [(combined >> 28) & 0xFFF, (combined >> 16) & 0xFFF, (combined >> 4) & 0xFFF]
    values = [(r / 4095.0 * 2.0 - 1.0) * _QUAT_HALF_RANGE for r in raws]
    component = str(field_def.get("component", ""))
    name = field_def["name"]
    unit = str(field_def.get("unit", ""))
    if component in ("q1", "q2", "q3"):
        i = int(component[1]) - 1
        return TelemetryField(name, label, raws[i], values[i], unit)
    meta = combined & 0xF
    if component == "index":
        return TelemetryField(name, label, (meta >> 2) & 0x3, float((meta >> 2) & 0x3), unit, True)
    if component == "sign":
        return TelemetryField(name, label, (meta >> 1) & 0x1, float((meta >> 1) & 0x1), unit, True)
    if component == "largest":
        magnitude = math.sqrt(max(0.0, 1.0 - sum(v * v for v in values)))
        value = -magnitude if (meta >> 1) & 0x1 else magnitude
        return TelemetryField(name, label, 0, value, unit)
    return None


def _decode_field(payload: bytes, field_def: dict[str, Any]) -> TelemetryField | None:
    offset: int = field_def["offset"]
    ftype: str = field_def["type"]
    scale: float = field_def.get("scale", 1.0)
    unit: str = field_def.get("unit", "")
    label: str = field_def.get("label", field_def["name"])
    length: int = field_def.get("length", 0)
    if field_def.get("to_end"):
        # Runs to the end of the payload, less ``trim_end`` trailing bytes (e.g. a CRC).
        length = len(payload) - offset - int(field_def.get("trim_end", 0))
        if length < 0:
            return None

    if offset + length > len(payload):
        return None

    chunk = payload[offset : offset + length]

    if ftype == "hex":
        return _text_field(field_def, label, chunk.hex(" "))

    if ftype == "base64":
        return _text_field(field_def, label, base64.b64encode(chunk).decode("ascii"))

    if ftype == "ax25_call":
        # An AX.25 address: every byte is the ASCII character shifted left by one bit.
        call = bytes(b >> 1 for b in chunk).decode("ascii", errors="replace").strip()
        return _text_field(field_def, label, call)

    if ftype == "quat5":
        return _decode_quat5(chunk, field_def, label)

    if ftype == "ascii":
        raw_val: int | float = 0
        scaled = chunk.decode("ascii", errors="replace").strip("\x00").strip()
        unit = ""
        # For ascii just use string as label extension — not ideal but functional
        return TelemetryField(
            name=field_def["name"],
            label=label,
            raw_value=raw_val,
            scaled_value=0.0,
            unit=scaled,
            is_string=True,
        )

    if ftype in ("unix_time", "unix_time_le"):
        # uint32 seconds since 1970-01-01 UTC (big-endian, or little-endian for
        # "unix_time_le"), shown as a date. This is the satellite's own clock, not the
        # reception time.
        (seconds,) = struct.unpack_from(">I" if ftype == "unix_time" else "<I", chunk)
        try:
            shown = datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (OverflowError, OSError, ValueError):
            shown = str(seconds)
        return TelemetryField(
            name=field_def["name"],
            label=label,
            raw_value=seconds,
            scaled_value=float(seconds),
            unit=shown,
            is_integer=True,
            is_string=True,
        )

    if ftype == "unix_time_ms_le":
        # uint64 milliseconds since 1970-01-01 UTC, little-endian: the satellite's own clock.
        (millis,) = struct.unpack_from("<Q", chunk)
        try:
            shown = (
                datetime.fromtimestamp(millis / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                + " UTC"
            )
        except (OverflowError, OSError, ValueError):
            shown = str(millis)
        return TelemetryField(
            name=field_def["name"],
            label=label,
            raw_value=millis,
            scaled_value=float(millis),
            unit=shown,
            is_integer=True,
            is_string=True,
        )

    fmt = _STRUCT_MAP.get(ftype)
    if fmt is None:
        return None

    try:
        (raw,) = struct.unpack_from(fmt, chunk)
    except struct.error:
        return None

    mask = field_def.get("mask")
    if mask is not None and isinstance(raw, int):
        # A bit field inside the integer: keep the masked bits, shifted down to bit 0.
        mask = int(mask)
        shift = (mask & -mask).bit_length() - 1 if mask else 0
        raw = (raw & mask) >> shift

    return TelemetryField(
        name=field_def["name"],
        label=label,
        raw_value=raw,
        scaled_value=float(raw) * scale,
        unit=unit,
        # An integer-typed field with no scaling applied is still a whole
        # number after decoding (e.g. a uint8 status byte) — worth telling
        # apart from a genuinely fractional measurement (e.g. a float32
        # voltage, or an integer scaled into physical units) so the UI can
        # skip pointless trailing zeros for the former.
        is_integer=(ftype not in _FLOAT_TYPES and float(scale).is_integer()),
    )


# (name, label) of the AX.25 header fields a ``text_message`` with ``header_fields`` shows.
AX25_HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("ax25_dest", "Destination"),
    ("ax25_src", "Source"),
    ("ax25_via", "Digipeater path (* = already repeated)"),
    ("ax25_pid", "PID"),
)


def _ax25_header_fields(ax25: Any) -> list[TelemetryField]:
    """The AX.25 header of *ax25* (an ``Ax25Frame``) as text fields."""
    values = (
        str(ax25.dest),
        str(ax25.src),
        ",".join(ax25.via) or "—",
        f"0x{int(ax25.pid):02X}",
    )
    return [
        TelemetryField(
            name=name,
            label=label,
            raw_value=0,
            scaled_value=0.0,
            unit=value,
            is_string=True,
        )
        for (name, label), value in zip(AX25_HEADER_FIELDS, values, strict=True)
    ]


def _is_number(token: str) -> bool:
    """True if *token* parses as a number (used to tell numeric CSV telemetry from free text)."""
    try:
        float(token)
    except ValueError:
        return False
    return True


def _csv_message_matches(text: str, key: str, msg_def: dict[str, Any]) -> bool:
    """Whether *text* is the csv_messages entry *key*.

    Normally the entry's key is a text prefix ("OBC," ...). An entry with ``match_count`` is
    for satellites whose messages carry no identifier (e.g. KOSTKA): it matches a message
    with exactly that many comma-separated fields, all of them numbers.
    """
    count = msg_def.get("match_count")
    if count is None:
        return text.startswith(f"{key},")
    tokens = text.split(",")
    return len(tokens) == count and all(_is_number(t.strip()) for t in tokens)


def _decode_csv_field(tokens: list[str], field_def: dict[str, Any]) -> TelemetryField | None:
    """Decode one field from a comma-split ASCII telemetry message.

    Used for satellites (e.g. Marina) whose downlink is plain ASCII text
    (``"OBC,1234,5678,..."``) rather than a packed binary struct — fields
    are located by their position in the comma-split token list (``index``)
    instead of a byte ``offset``/``length``.
    """
    index: int = field_def["index"]
    ftype: str = field_def["type"]
    label: str = field_def.get("label", field_def["name"])
    unit: str = field_def.get("unit", "")

    if index >= len(tokens):
        return None
    token = tokens[index].strip()

    if ftype == "str":
        return TelemetryField(
            name=field_def["name"],
            label=label,
            raw_value=0,
            scaled_value=0.0,
            unit=token,
            is_string=True,
        )

    if ftype == "float":
        try:
            value = float(token)
        except ValueError:
            return None
        return TelemetryField(
            name=field_def["name"],
            label=label,
            raw_value=int(value),
            scaled_value=value,
            unit=unit,
            is_integer=False,
        )

    raw: int
    if ftype == "int":
        nan_sentinel = field_def.get("nan_sentinel")
        if nan_sentinel is not None and token.lower() == "nan":
            raw = int(nan_sentinel)
        else:
            try:
                raw = int(token)
            except ValueError:
                return None
    elif ftype == "hex_int":
        try:
            raw = int(token, 16)
        except ValueError:
            return None
    elif ftype == "bitflag":
        base: int = field_def.get("base", 16)
        bit: int = field_def["bit"]
        try:
            raw_full = int(token, base)
        except ValueError:
            return None
        raw = (raw_full >> bit) & 1
    else:
        return None

    scale: float = field_def.get("scale", 1.0)
    add: float = field_def.get("add", 0.0)
    return TelemetryField(
        name=field_def["name"],
        label=label,
        raw_value=raw,
        scaled_value=float(raw) * scale + add,
        unit=unit,
        is_integer=(scale == 1.0 and add == 0.0),
    )


# ---------------------------------------------------------------------------
# Main decode function
# ---------------------------------------------------------------------------


# Sub-tab key of a flat ``fields`` list restricted by ``match`` (see _payload_matches()).
BEACON_ID = "beacon"


def get_telemetry_id_defs(norad: int | None) -> dict[str, Any] | None:
    """Return the per-telemetry-ID mapping from *norad*'s format file, if any.

    Checks both the binary-offset ``telemetry_ids`` schema (keyed by a
    numeric telemetry-ID byte, e.g. OrigamiSat-2) and the ASCII/CSV
    ``csv_messages`` schema (keyed by a text prefix, e.g. Marina's "OBC"; an entry with
    ``match_count`` is matched by its number of fields instead, e.g. KOSTKA's OBC).
    Satellites with only the older single flat ``fields`` list return None
    here — only a per-ID schema gives each ID's own field layout up front,
    before any frame of that ID has actually been received.

    A ``cw_frames`` schema (bit-packed Morse-coded hex frames, see
    comms.telemetry.cw_frames) is merged into the same {key: {"label",
    "fields"}} shape, listing only the fields a table shows (not the hidden
    helper bits), so the "Decoded Fields" tabs need no special case. A
    satellite can have both an AX.25 ``telemetry_ids`` schema *and* a
    ``cw_frames`` schema (e.g. OrigamiSat-2, whose CW beacon is a distinct,
    shorter frame from its FM/AX.25 housekeeping) — both sets of keys are
    returned together; cw_frames keys ("TLM", "HK1", ...) never collide with
    telemetry_ids' numeric-string keys or csv_messages' text-prefix keys.
    """
    fmt = load_format(norad) if norad is not None else None
    if not fmt:
        return None

    defs: dict[str, Any] = {}
    for key in ("telemetry_ids", "csv_messages"):
        candidate = fmt.get(key)
        if isinstance(candidate, dict) and candidate:
            defs = dict(candidate)
            break

    # A ``text_message`` entry (free-text frames, e.g. LASARsat) gets its own sub-tab.
    text_def = fmt.get("text_message")
    if defs and isinstance(text_def, dict):
        defs[str(text_def.get("id", "MSG"))] = {
            "label": text_def.get("label", ""),
            "fields": (
                [{"name": n, "label": label} for n, label in AX25_HEADER_FIELDS]
                if text_def.get("header_fields")
                else []
            )
            + [{"name": "text_message", "label": text_def.get("field_label", "Message")}],
        }

    # A flat ``fields`` list with a ``match`` pattern is one beacon type among
    # several frame types: give it its own "Decoded Fields" sub-tab.
    if not defs and fmt.get("match") and fmt.get("fields"):
        defs = {BEACON_ID: {"label": fmt.get("beacon_label", "Beacon"), "fields": fmt["fields"]}}

    # A ``frame_types`` schema (e.g. Lobachevsky): one sub-tab per frame type.
    frame_types = fmt.get("frame_types")
    if isinstance(frame_types, list):
        for ft in frame_types:
            defs[str(ft["id"])] = {"label": ft.get("label", ft["id"]), "fields": ft["fields"]}

    # A ``records`` schema (Sputnix Unified Protocol, e.g. UmKA-1): one sub-tab per record type.
    records = fmt.get("records")
    if isinstance(records, dict) and isinstance(records.get("types"), dict):
        for rec in records["types"].values():
            defs[str(rec["id"])] = {"label": rec.get("label", rec["id"]), "fields": rec["fields"]}

    cw_frames = fmt.get("cw_frames")
    if isinstance(cw_frames, dict) and cw_frames:
        for k, v in cw_frames.items():
            defs[str(k)] = {
                "label": v.get("label", k),
                "fields": [f for f in v.get("fields", []) if not f.get("hidden")],
            }

    return defs or None


def _payload_matches(match: dict[str, Any] | None, payload: bytes) -> bool:
    """Whether *payload* is the frame type a flat ``fields`` list describes.

    A satellite with several frame types and no telemetry-ID byte can tell the
    beacon by a fixed byte pattern: ``"match": {"offset": 0, "hex": "0000"}``.
    No ``match`` means every payload is decoded with the list. An optional ``mask`` (hex,
    as long as ``hex``) restricts the comparison to the bits set in it.
    """
    if not match:
        return True
    try:
        pattern = bytes.fromhex(str(match["hex"]))
        offset = int(match["offset"])
        mask = bytes.fromhex(str(match["mask"])) if "mask" in match else None
    except (KeyError, ValueError, TypeError):
        return True
    chunk = payload[offset : offset + len(pattern)]
    if len(chunk) < len(pattern):
        return False
    if mask is not None:
        if len(mask) != len(pattern):
            return True
        # Only the bits set in ``mask`` are compared (e.g. a 2-bit field inside a flags byte).
        return all((c & m) == (p & m) for c, p, m in zip(chunk, pattern, mask, strict=True))
    return chunk == pattern


# Record header of the ``records`` schema: u16 type, u16, u16, u16 body length (little-endian).
_RECORD_HEADER = struct.Struct("<HHHH")


def _decode_records(
    records: dict[str, Any], payload: bytes
) -> tuple[list[str], list[TelemetryField]]:
    """Walk the records in *payload* and decode those the format file defines.

    Each record is ``u16 type, u16, u16, u16 length`` (little-endian) followed by
    *length* body bytes; the file's ``records.types`` maps the type (4 hex digits)
    to ``{"id", "label", "fields"}`` whose field offsets are relative to the body.
    Records of an unknown type are skipped. Returns the ids found and all fields.
    """
    types = records.get("types", {})
    ids: list[str] = []
    fields: list[TelemetryField] = []
    pos = 0
    while pos + _RECORD_HEADER.size <= len(payload):
        rtype, _skip1, _skip2, length = _RECORD_HEADER.unpack_from(payload, pos)
        body = payload[pos + _RECORD_HEADER.size : pos + _RECORD_HEADER.size + length]
        pos += _RECORD_HEADER.size + length
        rec = types.get(f"{rtype:04X}")
        if rec is None or len(body) < length:
            continue
        ids.append(str(rec["id"]))
        for fd in rec["fields"]:
            result = _decode_field(body, fd)
            if result is not None:
                fields.append(result)
    return ids, fields


def decode_telemetry(
    callsign: str,
    payload: bytes,
    norad: int | None = None,
    ax25: Any = None,
) -> TelemetryFrame:
    """Decode *payload* bytes using the JSON definition for *norad*.

    Always returns a TelemetryFrame; falls back to raw hex when no
    definition exists or decoding fails. Format files may define:

    - a single flat ``fields`` list (byte offset/length, the original
      schema — one structure per satellite);
    - an optional ``match`` ({"offset", "hex"}) restricting that flat list to
      payloads starting with a fixed byte pattern (e.g. KNACKSAT-2's beacon);
    - a ``telemetry_ids`` mapping keyed by a numeric telemetry-ID byte
      (``payload[2]`` in this project's common FM header layout), for
      satellites whose downlink carries several distinct *binary*
      structures (e.g. OrigamiSat-2); or
    - a ``frame_types`` list (e.g. Lobachevsky): each entry has an ``id``, a ``label``,
      a ``match`` ({"offset", "hex"}, see _payload_matches()) and its own ``fields``
      (offsets from the start of the payload); the first entry that matches decodes it;
    - a ``records`` schema (a sequence of typed, length-prefixed records in one
      frame, e.g. UmKA-1's Sputnix Unified Protocol; see _decode_records());
    - a ``text_message`` entry with ``header_fields``: a free-text frame also shows its
      AX.25 header (destination, source, digipeater path, PID) taken from *ax25* (an
      ``Ax25Frame``; e.g. LASARsat's digipeated messages);
    - a ``csv_messages`` mapping keyed by a text prefix (e.g. "OBC"), for
      satellites whose downlink is plain comma-separated ASCII text
      (e.g. Marina) — fields are located by token position, not byte
      offset. An entry with ``match_count`` has no prefix: it matches a message of exactly
      that many numeric fields (e.g. KOSTKA).
    """
    raw_hex = payload.hex()
    fmt = load_format(norad) if norad is not None else None
    sat_name = fmt["name"] if fmt else (f"NORAD {norad}" if norad else callsign)

    decoded_fields: list[TelemetryField] = []
    telemetry_id: int | str | None = None
    telemetry_label = ""
    record_ids: list[str] = []

    csv_messages = fmt.get("csv_messages") if fmt else None
    telemetry_ids = fmt.get("telemetry_ids") if fmt else None
    records = fmt.get("records") if fmt else None
    frame_types = fmt.get("frame_types") if fmt else None

    frame_type_matched = False
    if isinstance(frame_types, list) and frame_types:
        for ft in frame_types:
            if not _payload_matches(ft.get("match"), payload):
                continue
            frame_type_matched = True
            telemetry_id = str(ft["id"])
            telemetry_label = ft.get("label", "")
            for fd in ft["fields"]:
                result = _decode_field(payload, fd)
                if result is not None:
                    decoded_fields.append(result)
            break
    # A frame no ``frame_types`` entry claims falls through to the other schemas, so a satellite
    # can have binary frame types *and* ASCII ``csv_messages`` (e.g. GRBBeta).
    if frame_type_matched:
        pass
    elif isinstance(records, dict):
        record_ids, decoded_fields = _decode_records(records, payload)
        if record_ids:
            telemetry_id = record_ids[0]
    elif csv_messages:
        # Some satellites (e.g. LASARsat) NUL-terminate the last field.
        text = payload.decode("ascii", errors="ignore").rstrip("\x00")
        for key, msg_def in csv_messages.items():
            if not _csv_message_matches(text, key, msg_def):
                continue
            telemetry_id = key
            telemetry_label = msg_def.get("label", "")
            tokens = text.split(",")
            for fd in msg_def.get("fields", []):
                result = _decode_csv_field(tokens, fd)
                if result is not None:
                    decoded_fields.append(result)
            break
        else:
            # Free-text frames (e.g. LASARsat's greetings and digipeated messages)
            # fall through to the format's optional "text_message" definition.
            text_def = fmt.get("text_message") if fmt else None
            if text_def and text.strip() and all(c.isprintable() for c in text):
                telemetry_id = str(text_def.get("id", "MSG"))
                telemetry_label = text_def.get("label", "")
                if text_def.get("header_fields") and ax25 is not None:
                    decoded_fields.extend(_ax25_header_fields(ax25))
                decoded_fields.append(
                    TelemetryField(
                        name="text_message",
                        label=text_def.get("field_label", "Message"),
                        raw_value=0,
                        scaled_value=0.0,
                        unit=text.strip(),
                        is_string=True,
                    )
                )
    elif telemetry_ids and len(payload) >= 3:
        telemetry_id = payload[2]
        id_def = telemetry_ids.get(str(telemetry_id))
        if id_def:
            telemetry_label = id_def.get("label", "")
            for fd in id_def.get("fields", []):
                result = _decode_field(payload, fd)
                if result is not None:
                    decoded_fields.append(result)
    elif fmt and fmt.get("fields") and _payload_matches(fmt.get("match"), payload):
        if fmt.get("match"):
            telemetry_id = BEACON_ID
            telemetry_label = fmt.get("beacon_label", "Beacon")
        for fd in fmt["fields"]:
            result = _decode_field(payload, fd)
            if result is not None:
                decoded_fields.append(result)

    return TelemetryFrame(
        norad=norad,
        callsign=callsign,
        satellite_name=sat_name,
        raw_hex=raw_hex,
        fields=decoded_fields,
        telemetry_id=telemetry_id,
        telemetry_label=telemetry_label,
        record_ids=record_ids,
    )
