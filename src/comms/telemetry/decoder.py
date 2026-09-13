"""Telemetry frame decoder.

Loads JSON format definitions from src/data/telemetry_formats/{norad}.json
and decodes raw AX.25 payloads into structured key-value dictionaries.

For satellites with no definition file the raw payload is returned as hex.
"""

from __future__ import annotations

import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from i18n import _

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


def load_format(norad: int) -> dict[str, Any] | None:
    """Load the JSON format definition for *norad*, or None if not found."""
    path = _formats_dir() / f"{norad}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
            return data
    except Exception:
        return None


def list_formats() -> list[dict[str, Any]]:
    """Return all available format definitions."""
    results: list[dict[str, Any]] = []
    fmt_dir = _formats_dir()
    if not fmt_dir.exists():
        return results
    for p in sorted(fmt_dir.glob("*.json")):
        try:
            with p.open(encoding="utf-8") as f:
                results.append(json.load(f))
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
    "float32_be": ">f",
    "float64_be": ">d",
}

_FLOAT_TYPES = {"float32_be", "float64_be"}


def _decode_field(payload: bytes, field_def: dict[str, Any]) -> TelemetryField | None:
    offset: int = field_def["offset"]
    length: int = field_def["length"]
    ftype: str = field_def["type"]
    scale: float = field_def.get("scale", 1.0)
    unit: str = field_def.get("unit", "")
    label: str = field_def.get("label", field_def["name"])

    if offset + length > len(payload):
        return None

    chunk = payload[offset : offset + length]

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

    fmt = _STRUCT_MAP.get(ftype)
    if fmt is None:
        return None

    try:
        (raw,) = struct.unpack_from(fmt, chunk)
    except struct.error:
        return None

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
        is_integer=(ftype not in _FLOAT_TYPES and scale == 1.0),
    )


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


def get_telemetry_id_defs(norad: int | None) -> dict[str, Any] | None:
    """Return the per-telemetry-ID mapping from *norad*'s format file, if any.

    Checks both the binary-offset ``telemetry_ids`` schema (keyed by a
    numeric telemetry-ID byte, e.g. OrigamiSat-2) and the ASCII/CSV
    ``csv_messages`` schema (keyed by a text prefix, e.g. Marina's "OBC").
    Satellites with only the older single flat ``fields`` list return None
    here — only a per-ID schema gives each ID's own field layout up front,
    before any frame of that ID has actually been received.
    """
    fmt = load_format(norad) if norad is not None else None
    if not fmt:
        return None
    for key in ("telemetry_ids", "csv_messages"):
        defs = fmt.get(key)
        if isinstance(defs, dict) and defs:
            return defs
    return None


def decode_telemetry(
    callsign: str,
    payload: bytes,
    norad: int | None = None,
) -> TelemetryFrame:
    """Decode *payload* bytes using the JSON definition for *norad*.

    Always returns a TelemetryFrame; falls back to raw hex when no
    definition exists or decoding fails. Format files may define:

    - a single flat ``fields`` list (byte offset/length, the original
      schema — one structure per satellite);
    - a ``telemetry_ids`` mapping keyed by a numeric telemetry-ID byte
      (``payload[2]`` in this project's common FM header layout), for
      satellites whose downlink carries several distinct *binary*
      structures (e.g. OrigamiSat-2); or
    - a ``csv_messages`` mapping keyed by a text prefix (e.g. "OBC"), for
      satellites whose downlink is plain comma-separated ASCII text
      (e.g. Marina) — fields are located by token position, not byte
      offset.
    """
    raw_hex = payload.hex()
    fmt = load_format(norad) if norad is not None else None
    sat_name = fmt["name"] if fmt else (f"NORAD {norad}" if norad else callsign)

    decoded_fields: list[TelemetryField] = []
    telemetry_id: int | str | None = None
    telemetry_label = ""

    csv_messages = fmt.get("csv_messages") if fmt else None
    telemetry_ids = fmt.get("telemetry_ids") if fmt else None

    if csv_messages:
        text = payload.decode("ascii", errors="ignore")
        for key, msg_def in csv_messages.items():
            if not text.startswith(f"{key},"):
                continue
            telemetry_id = key
            telemetry_label = msg_def.get("label", "")
            tokens = text.split(",")
            for fd in msg_def.get("fields", []):
                result = _decode_csv_field(tokens, fd)
                if result is not None:
                    decoded_fields.append(result)
            break
    elif telemetry_ids and len(payload) >= 3:
        telemetry_id = payload[2]
        id_def = telemetry_ids.get(str(telemetry_id))
        if id_def:
            telemetry_label = id_def.get("label", "")
            for fd in id_def.get("fields", []):
                result = _decode_field(payload, fd)
                if result is not None:
                    decoded_fields.append(result)
    elif fmt and fmt.get("fields"):
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
    )
