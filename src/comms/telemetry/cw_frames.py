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

A frame may also carry ``id_prefix``: text the satellite always sends right
before the hex data, with no pause in between (e.g. OrigamiSat-2's beacon is
"call sign, satellite name, data section" back to back per its CW spec, so
the CW Decoder tab's block extractor -- which only cuts a block at a >3s
silence -- hands ``_on_cw_block()`` one block of "JS1YRUORIGAMI2" + the 56
hex digits run together, not two). When set, a block is matched by stripping
that *exact* leading text first (comparison is against the normalized
block: upper-case, no spaces -- see ``normalize_block()``) and checking the
remainder's length, rather than the whole block. ARICA-2's beacon sends bare
hex with no such prefix, so its frames simply omit the key.

Two more optional keys help against misread digits (CW has no CRC):

  clock         ``{"field": name, "tolerance_s": n}``: ``name`` is a UNIX-time field the
                satellite stamps its frame with. When the time the frame was received
                is known, a frame whose stamp is further than ``tolerance_s`` from it is
                not valid, and unreadable digits in the stamp's upper bytes are filled
                in from the clock (only if exactly one value fits).
  satnogs       how a frame is sent to the SatNOGS DB (see build_satnogs_frame()); with
                ``"require_repeat": false`` an automatic upload does not wait for the
                same frame to be received a second time (needed where every frame is
                different, e.g. it carries a time stamp).
  known_digits  ``{"46": "4B42483E00"}``: digits that normally never change (settings),
                keyed by their digit position. Only *unreadable* positions are filled in
                (a readable digit is never overridden), and a frame cut short in the middle
                of such a run is completed.

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

A frame may instead be decimal *text* (LASARsat's "de ok0lsr = u<up>r<rst>t<mcu>p<pa> ar"):
give it ``text_regex`` (matched against the whole part after ``id_prefix`` of the normalized,
upper-case, space-free block) instead of ``hex_digits``, and put ``"group": n`` (regex group)
on each field instead of ``bits``. An optional ``near_regex`` marks a rejected-candidate
look-alike. Such frames have no digit repair and no SatNOGS upload.

The hex text must have exactly ``hex_digits`` digits. Which fields exist, their
order/width and the conversions come from the public arica2.ksy of the SatNOGS
decoders; the wording of the labels and the flag/range texts follow the
operator's own published data tables. Nothing here guesses beyond that.
"""

from __future__ import annotations

import datetime
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


# How many edits (substitutions, drops, insertions) a callsign+name read may be off from the
# frame's ``id_prefix`` and still count as that prefix. The digits that follow are checked
# strictly anyway; the prefix only says where a frame starts.
_PREFIX_MAX_EDITS = 3


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance of two short strings."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _find_prefix(block: str, prefix: str, start: int = 0) -> tuple[int, int] | None:
    """(begin, end) of the first read of *prefix* in *block* at or after *start*, else None.

    An exact read wins; otherwise the closest read within _PREFIX_MAX_EDITS edits (a
    garbled callsign or name -- the Morse decoder has no CRC to tell). Text before
    *begin* is not part of this frame (the tail of the previous one).
    """
    exact = block.find(prefix, start)
    if exact >= 0:
        return exact, exact + len(prefix)
    best: tuple[int, int, int] | None = None  # (distance, begin, end)
    n = len(prefix)
    for begin in range(start, len(block) - n + _PREFIX_MAX_EDITS + 1):
        for length in range(n - _PREFIX_MAX_EDITS, n + _PREFIX_MAX_EDITS + 1):
            if length <= 0 or begin + length > len(block):
                continue
            dist = _edit_distance(block[begin : begin + length], prefix)
            if dist <= _PREFIX_MAX_EDITS and (best is None or dist < best[0]):
                best = (dist, begin, begin + length)
        if best is not None and best[0] <= 1:
            break
    return (best[1], best[2]) if best else None


def split_frame_blocks(norad: int | None, text: str) -> list[tuple[float, str]]:
    """Cut one block of CW text into the frames it holds: ``[(position 0..1, text)]``.

    The beacon sends frames back to back, so the block extractor (it cuts on a pause)
    often hands over the tail of one frame, then the next frame's ``id_prefix`` and
    digits, or even several whole frames, as one block. Each returned piece starts at
    an ``id_prefix`` read (see _find_prefix()); *position* is where in the block that is,
    for timing it. A satellite without an ``id_prefix``, or a block with none in it, comes
    back whole.
    """
    block = normalize_block(text)
    frames = load_cw_frames(norad) or {}
    prefixes = {str(f.get("id_prefix", "")) for f in frames.values()} - {""}
    if len(prefixes) != 1 or not block:
        return [(0.0, block)]
    prefix = next(iter(prefixes))
    spans: list[tuple[int, int]] = []
    pos = 0
    while (found := _find_prefix(block, prefix, pos)) is not None:
        spans.append(found)
        pos = found[1]
    if not spans:
        return [(0.0, block)]
    pieces: list[tuple[float, str]] = []
    for i, (begin, _end) in enumerate(spans):
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(block)
        pieces.append((begin / len(block), block[begin:stop]))
    return pieces


def _hex_part(frame_def: dict[str, Any], block: str) -> str | None:
    """The text after *frame_def*'s ``id_prefix`` in *block*, or None if it isn't there.

    The prefix may follow some leftover text (the previous frame's tail) and may be
    slightly misread; the text runs up to the next prefix, if the block holds several
    frames. Frames without an ``id_prefix`` (e.g. ARICA-2's bare-hex beacon) return
    *block* unchanged.
    """
    prefix = str(frame_def.get("id_prefix", ""))
    if not prefix:
        return block
    found = _find_prefix(block, prefix)
    if found is None:
        return None
    following = _find_prefix(block, prefix, found[1])
    return block[found[1] : following[0] if following else len(block)]


def _digit_offset(frame_def: dict[str, Any], name: str) -> int | None:
    """Position (in hex digits) of field *name*'s first bit, or None if absent / unaligned."""
    bits = 0
    for fd in frame_def["fields"]:
        if fd["name"] == name:
            return bits // 4 if bits % 4 == 0 else None
        bits += int(fd.get("bits") or 0)
    return None


def _epoch(when: datetime.datetime) -> float:
    """POSIX seconds of *when* (a naive time is taken as UTC)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return when.timestamp()


def _clock_candidates(
    cand: str, start: int, when: datetime.datetime, tolerance_s: float
) -> str | None:
    """*cand* with the unreadable digits among the 6 upper-byte digits of the 8-digit time
    stamp at *start* filled in from the clock, or None if that does not give exactly one
    answer. The two lowest digits (the second count) are never touched."""
    stamp = list(cand[start : start + 6])
    unknown = [i for i, ch in enumerate(stamp) if ch not in "0123456789ABCDEF"]
    if not unknown or len(unknown) > 2:
        return None
    target = _epoch(when)
    found: list[str] = []
    for combo in range(16 ** len(unknown)):
        digits = stamp[:]
        for k, pos in enumerate(unknown):
            digits[pos] = f"{(combo >> (4 * k)) & 0xF:X}"
        top = int("".join(digits), 16) * 256
        # does any second of this 256 s block fall within the tolerance of the clock?
        if top - tolerance_s <= target <= top + 255 + tolerance_s:
            found.append("".join(digits))
    if len(found) != 1:
        return None
    return cand[:start] + found[0] + cand[start + 6 :]


def _repair_digits(frame_def: dict[str, Any], cand: str, when: datetime.datetime | None) -> str:
    """Fill in digits that could not be read, from what the frame definition knows."""
    if "text_regex" in frame_def:
        return cand
    n = int(frame_def["hex_digits"])
    for key, run in (frame_def.get("known_digits") or {}).items():
        k = int(key)
        # a frame cut short somewhere in the run: complete it (the run is a settings block, so
        # the tail that did arrive must match it exactly)
        if k <= len(cand) < n and run.startswith(cand[k:]):
            cand += run[len(cand) - k :]
        if len(cand) == n:
            fixed = "".join(
                run[i] if ch not in "0123456789ABCDEF" else ch for i, ch in enumerate(cand[k:])
            )[: len(run)]
            cand = cand[:k] + fixed + cand[k + len(run) :]
    clock = frame_def.get("clock")
    if when is not None and clock and len(cand) == n:
        start = _digit_offset(frame_def, str(clock["field"]))
        if start is not None:
            repaired = _clock_candidates(cand, start, when, float(clock["tolerance_s"]))
            if repaired is not None:
                cand = repaired
    return cand


def repair_block(norad: int | None, text: str, when: datetime.datetime | None = None) -> str:
    """*text* (one frame's block, normalized) with unreadable digits filled in where the
    frame definition allows it (see ``known_digits`` / ``clock`` in the module docstring).

    *when* is when the frame was received (None if that is not reliably known: the clock
    repair is then skipped). A block that is not a frame, or needs no repair, comes back
    normalized but otherwise unchanged. Never changes a readable digit.
    """
    block = normalize_block(text)
    frames = load_cw_frames(norad) or {}
    for frame_def in frames.values():
        prefix = str(frame_def.get("id_prefix", ""))
        if not prefix:
            continue
        found = _find_prefix(block, prefix)
        if found is None:
            continue
        following = _find_prefix(block, prefix, found[1])
        stop = following[0] if following else len(block)
        cand = block[found[1] : stop]
        fixed = _repair_digits(frame_def, cand, when)
        return block[: found[1]] + fixed + block[stop:]
    return block


def _match(frames: dict[str, Any], block: str) -> tuple[str, dict[str, Any], str] | None:
    """(key, frame_def, hex_part) of the frame *block* matches exactly, else None."""
    for key, frame_def in frames.items():
        candidate = _hex_part(frame_def, block)
        if candidate is not None and "text_regex" in frame_def:
            if re.fullmatch(str(frame_def["text_regex"]), candidate):
                return str(key), frame_def, candidate
            continue
        if (
            candidate is not None
            and len(candidate) == int(frame_def["hex_digits"])
            and _HEX_RE.match(candidate)
        ):
            return str(key), frame_def, candidate
    return None


def match_frame_key(norad: int | None, text: str) -> str | None:
    """Key of the frame *text* matches exactly (id_prefix + hex digits), else None."""
    frames = load_cw_frames(norad)
    if not frames:
        return None
    matched = _match(frames, normalize_block(text))
    return matched[0] if matched else None


# A block this many digits off a frame's length (a dropped or inserted character, or two) is
# still shown as a rejected candidate. A strong OrigamiSat-2 pass (2026-10-01) lost the two
# digits "3E" of a 56-digit frame and so was never listed at all.
_NEAR_MISS_LENGTH_DIFF = 2


def near_miss_hex(norad: int | None, text: str) -> str | None:
    """The hex part of *text* if it is a near miss of a known frame (see is_near_miss()).

    The frame's ``id_prefix`` (callsign and name, e.g. ``JS1YRUORIGAMI2``) is left out, so a
    rejected candidate is shown like an accepted frame: just the digits. None if *text* is
    not a near miss.
    """
    frames = load_cw_frames(norad)
    block = normalize_block(text)
    if not frames or not block:
        return None
    for frame_def in frames.values():
        candidate = _hex_part(frame_def, block)
        if not candidate:
            continue
        if "text_regex" in frame_def:
            near = frame_def.get("near_regex")
            if near and re.fullmatch(str(near), candidate):
                return candidate
            continue
        hex_chars = sum(1 for c in candidate if c in "0123456789ABCDEF")
        if hex_chars < 0.75 * len(candidate):
            continue
        n = int(frame_def["hex_digits"])
        if len(candidate) == n:
            if not _HEX_RE.match(candidate):
                return candidate
        elif abs(len(candidate) - n) <= _NEAR_MISS_LENGTH_DIFF:
            return candidate
    return None


def is_near_miss(norad: int | None, text: str) -> bool:
    """True if *text* looks like a frame that was mis-read: a length off by one
    digit (up to two) from a known frame (a dropped or inserted character) or a
    frame-length block holding a few non-hex characters. For a frame with an
    ``id_prefix``, this only looks past an *exactly* matched prefix -- a garbled
    prefix (e.g. a dropped letter in the callsign) is not chased any further, same
    rigor as everywhere else here."""
    return near_miss_hex(norad, text) is not None


def build_satnogs_frame(norad: int | None, text: str) -> bytes | None:
    """The frame as the SatNOGS DB expects a CW telemetry submission.

    A frame definition's ``satnogs`` entry says how it is sent:

    * ``{"callsign": ..., "beacon_type": n}``: ``prefix + type byte + the frame's bytes``
      (for ARICA-2 the layout arica2.ksy names cw1_form/cw2_form/cw3_form);
    * ``{"raw": true}``: just the frame's bytes, as received. SatNOGS has no decoder
      for the satellite, so everybody submits what they received (OrigamiSat-2).

    *text* is a whole block or, as the telemetry log stores it, just the frame's hex
    digits (without any ``id_prefix``). None if *text* is not exactly one of the
    satellite's frames or the satellite has no ``satnogs`` entry.
    """
    frames = load_cw_frames(norad)
    if frames is None:
        return None
    block = normalize_block(text)
    matched = _match(frames, block)
    if matched is None:
        # the log keeps only the hex digits, without the id_prefix
        for key, frame_def in frames.items():
            if (
                "hex_digits" in frame_def
                and len(block) == int(frame_def["hex_digits"])
                and _HEX_RE.match(block)
            ):
                matched = (str(key), frame_def, block)
                break
    if matched is None:
        return None
    _key, frame_def, hex_part = matched
    upload = frame_def.get("satnogs")
    if not upload or len(hex_part) % 2:
        return None
    if upload.get("raw"):
        return bytes.fromhex(hex_part)
    prefix = str(upload["callsign"]).encode("ascii")
    return prefix + bytes([int(upload["beacon_type"])]) + bytes.fromhex(hex_part)


def repeat_required(norad: int | None) -> bool:
    """False if the satellite's frames never repeat verbatim (``satnogs.require_repeat``
    is false), so a repeat can not confirm a reading; True otherwise."""
    for frame_def in (load_cw_frames(norad) or {}).values():
        upload = frame_def.get("satnogs") or {}
        if upload.get("require_repeat") is False:
            return False
    return True


def fields_from_saved(
    norad: int | None, saved: dict[str, Any]
) -> tuple[str, str, list[TelemetryField]] | None:
    """Rebuild a logged CW frame's decoded fields from what ``telemetry_log`` saved.

    *saved* is the stored ``{field name: {"value", "unit"}}`` (the scaled value, and
    the unit -- or the display text of a flag / angular-velocity / time field). Returns
    ``(frame key, frame label, fields)`` for the frame definition whose displayed fields
    are exactly *saved*'s, or None if none is.
    """
    frames = load_cw_frames(norad)
    if not frames or not saved:
        return None
    for key, frame_def in frames.items():
        defs = display_field_defs(frame_def)
        if {str(fd["name"]) for fd in defs} != set(saved):
            continue
        fields: list[TelemetryField] = []
        for fd in defs:
            entry = saved[str(fd["name"])]
            if not isinstance(entry, dict):
                return None
            value = float(entry.get("value", 0.0))
            unit = str(entry.get("unit", ""))
            name = str(fd["name"])
            label = str(fd.get("label", name))
            if fd.get("type", "uint") in ("hms", "flag", "angvel"):
                fields.append(
                    TelemetryField(
                        name=name,
                        label=label,
                        raw_value=int(value),
                        scaled_value=value,
                        unit=unit,
                        is_string=True,
                    )
                )
            else:
                fields.append(
                    TelemetryField(
                        name=name,
                        label=label,
                        raw_value=value,
                        scaled_value=value,
                        unit=unit,
                        is_integer=float(fd.get("scale", 1.0)) == 1.0
                        and float(fd.get("add", 0.0)) == 0.0,
                    )
                )
        return str(key), str(frame_def.get("label", key)), fields
    return None


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


def decode_cw_frame(
    norad: int | None, text: str, when: datetime.datetime | None = None
) -> CwFrameDecode | None:
    """Decode *text* (hex digits) as one of *norad*'s CW frames.

    *when* is when the frame was received; if the frame definition has a ``clock``
    check, a time stamp too far from it makes the frame not valid.

    Returns None when the satellite has no ``cw_frames`` schema or the text is
    not exactly a frame's number of hex digits. Otherwise always returns a
    result; ``result.valid`` says whether the plausibility checks passed.
    """
    frames = load_cw_frames(norad)
    if frames is None:
        return None
    matched = _match(frames, normalize_block(text))
    if matched is None:
        return None
    key, frame_def, hex_part = matched
    fmt = load_format(norad) if norad is not None else None
    assert fmt is not None
    tables: dict[str, Any] = fmt.get("tables", {})
    if "text_regex" in frame_def:
        return _decode_text_frame(key, frame_def, hex_part)
    bitstring = bin(int(hex_part, 16))[2:].zfill(4 * len(hex_part))

    raw: dict[str, int] = {}
    pos = 0
    for fd in frame_def["fields"]:
        bits = fd.get("bits")
        if bits is None:
            continue
        raw[fd["name"]] = int(bitstring[pos : pos + int(bits)], 2)
        pos += int(bits)

    result = CwFrameDecode(key=key, label=str(frame_def.get("label", key)), hex_text=hex_part)
    clock = frame_def.get("clock")
    if when is not None and clock and str(clock["field"]) in raw:
        stamp = raw[str(clock["field"])]
        off = stamp - _epoch(when)
        if abs(off) > float(clock["tolerance_s"]):
            result.problems.append(f"{clock['field']} is {off:+.0f} s from the receive time")
    for fd in frame_def["fields"]:
        problem = _check(fd, raw)
        if problem:
            result.problems.append(problem)
        if fd.get("hidden"):
            continue
        result.fields.append(_build_field(fd, raw, tables))
    return result


def _decode_text_frame(key: str, frame_def: dict[str, Any], text: str) -> CwFrameDecode:
    """Decode a decimal-text frame (``text_regex`` + a ``group`` per field)."""
    m = re.fullmatch(str(frame_def["text_regex"]), text)
    assert m is not None
    result = CwFrameDecode(key=key, label=str(frame_def.get("label", key)), hex_text=text)
    for fd in frame_def["fields"]:
        value = int(m.group(int(fd["group"])))
        name = str(fd["name"])
        if "range" in fd:
            lo, hi = fd["range"]
            if not lo <= value <= hi:
                result.problems.append(f"{name}={value} (outside {lo}..{hi})")
        scale = float(fd.get("scale", 1.0))
        result.fields.append(
            TelemetryField(
                name=name,
                label=str(fd.get("label", name)),
                raw_value=value,
                scaled_value=value * scale + float(fd.get("add", 0.0)),
                unit=str(fd.get("unit", "")),
                is_integer=scale == 1.0 and float(fd.get("add", 0.0)) == 0.0,
            )
        )
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
