"""Bit-packed CW housekeeping frames (ARICA-2 style "CW TLM").

Some satellites send their housekeeping (HK) as Morse-coded hexadecimal
digits: a frame is a fixed number of hex digits, i.e. a few bytes whose fields
are packed at *bit* level (a 4-bit command id, a run of 1-bit flags, ...).
The binary schemas in decoder.py address whole bytes, so this module adds a
separate ``cw_frames`` schema to a satellite's telemetry_formats/{norad}.json:

    "cw_frames": {
      "HK1": {"label": "...", "hex_digits": 16, "fields": [ {...}, ... ]},
      ...
    },
    "tables": {"angvel_edges": [0.0, 0.173, ...]}

Each entry of ``fields`` is consumed in order, MSB first. Kinds:

  flag      1 bit; ``labels`` maps "0"/"1" to the text shown.
  uint      ``bits`` wide integer, shown as ``raw * scale + add`` (``unit``).
            ``sign_from`` names an earlier flag whose bit 1 means "+" and
            bit 0 means "-" (GPS latitude / longitude).
  angvel    5-bit sign-magnitude code (top bit = sign) shown as the range the
            code stands for, looked up in ``tables[table]`` (see below).
  hms       derived (no bits): "HH:MM:SS" from three earlier fields.

``hidden`` fields are parsed but not shown. ``expect`` (a value the field must
have, e.g. an unused field that is always 0) and ``range`` ([min, max]) are the
plausibility checks: CW has no CRC and the Morse decode of a weak signal is
error-prone, so a frame that breaks them is reported as *not valid* and must
not be shown as measured data.

The hex text must have exactly ``hex_digits`` digits. Which fields exist, their
order/width and the conversions come from the public arica2.ksy of the SatNOGS
decoders; the wording of the labels and the flag/range texts follow the
operator's own published data tables. Nothing here guesses beyond that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from comms.telemetry.decoder import TelemetryField, load_format

_HEX_RE = re.compile(r"^[0-9A-F]+$")


@dataclass
class CwFrameDecode:
    """A decoded CW housekeeping frame."""

    key: str
    label: str
    hex_text: str
    fields: list[TelemetryField] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """True when every plausibility check passed."""
        return not self.problems


def normalize_block(text: str) -> str:
    """Upper-case *text* and drop all whitespace (the Morse decoder adds spurious spaces)."""
    return "".join(text.split()).upper()


def load_cw_frames(norad: int | None) -> dict[str, Any] | None:
    """Return the ``cw_frames`` mapping of *norad*'s format file, if it has one."""
    fmt = load_format(norad) if norad is not None else None
    if not fmt:
        return None
    frames = fmt.get("cw_frames")
    return frames if isinstance(frames, dict) and frames else None


def display_field_defs(frame_def: dict[str, Any]) -> list[dict[str, Any]]:
    """The field definitions a "Decoded Fields" table shows, in display order."""
    return [fd for fd in frame_def.get("fields", []) if not fd.get("hidden")]


def match_frame_key(norad: int | None, text: str) -> str | None:
    """Key of the frame whose length *text* has exactly (all hex digits), else None."""
    frames = load_cw_frames(norad)
    block = normalize_block(text)
    if not frames or not _HEX_RE.match(block):
        return None
    for key, frame_def in frames.items():
        if len(block) == int(frame_def["hex_digits"]):
            return str(key)
    return None


def is_near_miss(norad: int | None, text: str) -> bool:
    """True if *text* looks like a frame that was mis-read: a length off by one
    digit from a known frame (a dropped or inserted character) or a frame-length
    block holding a few non-hex characters."""
    frames = load_cw_frames(norad)
    block = normalize_block(text)
    if not frames or not block:
        return False
    hex_chars = sum(1 for c in block if c in "0123456789ABCDEF")
    if hex_chars < 0.75 * len(block):
        return False
    lengths = {int(fd["hex_digits"]) for fd in frames.values()}
    if len(block) in lengths:
        return not _HEX_RE.match(block)
    return any(abs(len(block) - n) == 1 for n in lengths)


def _format_edge(value: float) -> str:
    return f"{value:g}"


def _angvel_text(code: int, bits: int, edges: list[float], axis: str) -> str:
    """Range text for a sign-magnitude angular-velocity code (the operator's table style)."""
    negative = bool(code >> (bits - 1))
    mag = code & ((1 << (bits - 1)) - 1)
    known = len(edges) - 1  # magnitudes 0 .. known-1 have both edges published
    if mag >= known:
        lower = _format_edge(edges[-1])
        sign = "-" if negative else ""
        op = "≧" if not negative else "≦"
        return f"|{axis}| {op} {sign}{lower} (range not confirmed)"
    lo, hi = edges[mag], edges[mag + 1]
    if not negative:
        return f"{_format_edge(lo)} ≦ {axis} ＜ {_format_edge(hi)}"
    hi_text = "-0" if mag == 0 else f"-{_format_edge(lo)}"
    return f"-{_format_edge(hi)} ≦ {axis} ＜ {hi_text}"


def decode_cw_frame(norad: int | None, text: str) -> CwFrameDecode | None:
    """Decode *text* (hex digits) as one of *norad*'s CW frames.

    Returns None when the satellite has no ``cw_frames`` schema or the text is
    not exactly a frame's number of hex digits. Otherwise always returns a
    result; ``result.valid`` says whether the plausibility checks passed.
    """
    key = match_frame_key(norad, text)
    if key is None:
        return None
    fmt = load_format(norad) if norad is not None else None
    assert fmt is not None
    frame_def = fmt["cw_frames"][key]
    tables: dict[str, Any] = fmt.get("tables", {})
    block = normalize_block(text)
    bitstring = bin(int(block, 16))[2:].zfill(4 * len(block))

    raw: dict[str, int] = {}
    pos = 0
    for fd in frame_def["fields"]:
        bits = fd.get("bits")
        if bits is None:
            continue
        raw[fd["name"]] = int(bitstring[pos : pos + int(bits)], 2)
        pos += int(bits)

    result = CwFrameDecode(key=str(key), label=str(frame_def.get("label", key)), hex_text=block)
    for fd in frame_def["fields"]:
        problem = _check(fd, raw)
        if problem:
            result.problems.append(problem)
        if fd.get("hidden"):
            continue
        result.fields.append(_build_field(fd, raw, tables))
    return result


def _check(fd: dict[str, Any], raw: dict[str, int]) -> str | None:
    """Plausibility problem of one field, or None."""
    name = str(fd["name"])
    if fd.get("bits") is None:
        return None
    value = raw[name]
    if "expect" in fd and value != fd["expect"]:
        return f"{name}={value} (expected {fd['expect']})"
    if "range" in fd:
        lo, hi = fd["range"]
        if not lo <= value <= hi:
            return f"{name}={value} (outside {lo}..{hi})"
    return None


def _build_field(fd: dict[str, Any], raw: dict[str, int], tables: dict[str, Any]) -> TelemetryField:
    name = str(fd["name"])
    label = str(fd.get("label", name))
    kind = fd.get("type", "uint")
    if kind == "hms":
        h, m, s = (raw[n] for n in fd["from"])
        return TelemetryField(
            name=name,
            label=label,
            raw_value=0,
            scaled_value=0.0,
            unit=f"{h:02d}:{m:02d}:{s:02d}",
            is_string=True,
        )
    value = raw[name]
    if kind == "flag":
        text = str(fd["labels"].get(str(value), str(value)))
        return TelemetryField(
            name=name, label=label, raw_value=value, scaled_value=float(value), unit=text,
            is_string=True,
        )  # fmt: skip
    if kind == "angvel":
        edges = [float(e) for e in tables[fd["table"]]]
        text = _angvel_text(value, int(fd["bits"]), edges, str(fd.get("axis", "G")))
        return TelemetryField(
            name=name, label=label, raw_value=value, scaled_value=float(value), unit=text,
            is_string=True,
        )  # fmt: skip
    scale = float(fd.get("scale", 1.0))
    add = float(fd.get("add", 0.0))
    scaled = value * scale + add
    sign_from = fd.get("sign_from")
    if sign_from is not None and raw[str(sign_from)] == 0:
        scaled = -scaled
    return TelemetryField(
        name=name,
        label=label,
        raw_value=value,
        scaled_value=scaled,
        unit=str(fd.get("unit", "")),
        is_integer=scale == 1.0 and add == 0.0,
    )
