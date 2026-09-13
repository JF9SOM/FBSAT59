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


@dataclass
class TelemetryFrame:
    """Decoded telemetry packet."""

    norad: int | None
    callsign: str
    satellite_name: str
    raw_hex: str
    fields: list[TelemetryField] = field(default_factory=list)
    signal_db: float | None = None
    telemetry_id: int | None = None
    telemetry_label: str = ""

    @property
    def has_fields(self) -> bool:
        return bool(self.fields)

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        if not self.fields:
            return f"[raw] {self.raw_hex[:40]}"
        parts = [f"{f.label}: {f.scaled_value:.2f}{f.unit}" for f in self.fields[:4]]
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
    )


# ---------------------------------------------------------------------------
# Main decode function
# ---------------------------------------------------------------------------


def get_telemetry_id_defs(norad: int | None) -> dict[str, Any] | None:
    """Return the ``telemetry_ids`` mapping from *norad*'s format file, if any.

    Satellites with a single flat ``fields`` list (the older format) return
    None here — only the newer per-telemetry-ID schema is exposed, since a
    telemetry ID sub-tab needs each ID's own field layout up front, before
    any frame of that ID has actually been received.
    """
    fmt = load_format(norad) if norad is not None else None
    if not fmt:
        return None
    telemetry_ids = fmt.get("telemetry_ids")
    return telemetry_ids if isinstance(telemetry_ids, dict) and telemetry_ids else None


def decode_telemetry(
    callsign: str,
    payload: bytes,
    norad: int | None = None,
) -> TelemetryFrame:
    """Decode *payload* bytes using the JSON definition for *norad*.

    Always returns a TelemetryFrame; falls back to raw hex when no
    definition exists or decoding fails. Format files may define either a
    single flat ``fields`` list, or a ``telemetry_ids`` mapping keyed by the
    telemetry ID byte (payload[2] in this project's common FM header
    layout) for satellites whose downlink carries several distinct
    telemetry structures.
    """
    raw_hex = payload.hex()
    fmt = load_format(norad) if norad is not None else None
    sat_name = fmt["name"] if fmt else (f"NORAD {norad}" if norad else callsign)

    decoded_fields: list[TelemetryField] = []
    telemetry_id: int | None = None
    telemetry_label = ""

    telemetry_ids = fmt.get("telemetry_ids") if fmt else None
    if telemetry_ids and len(payload) >= 3:
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
