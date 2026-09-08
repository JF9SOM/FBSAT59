"""Turn an APRS packet into one plain-language line for the APRS tab.

The APRS tab's receive log has a Raw / Plain toggle. Raw shows the on-air
information field verbatim; Plain calls :func:`humanize_frame` here, which
leans on ``aprslib`` for the hard parsing (MIC-E, compressed positions,
messages, objects, weather, third-party encapsulation) and then renders a
short human sentence from the parsed fields.

Design notes
------------
* Base strings are English and wrapped in ``_()`` so the UI language
  applies. Coordinates / numbers are formatted, not translated.
* ``humanize_frame`` returns ``None`` when it cannot make a useful
  sentence — the caller then shows the raw info field instead, so an
  unrecognised packet is never blanked.
* MIC-E decode values are only as trustworthy as aprslib's decoder; we do
  not second-guess them, we just present what it returns (see the
  regression set in ``tests/test_aprs_humanize.py``, whose expected values
  are cross-checked against the APRS spec / aprs.fi rather than against
  this module).
"""

from __future__ import annotations

import re
from typing import Any

import aprslib

from i18n import _


def _N(text: str) -> str:
    """gettext no-op marker.

    The tables below are module-level constants, so wrapping their values in
    ``_()`` directly would freeze them to the language active at *import*
    time (docs/i18n.md pitfall #2). Instead the values are plain English
    marked with ``_N`` for string extraction, and translated with ``_()``
    at lookup time inside the render functions.
    """
    return text


# --------------------------------------------------------------------------- #
# Symbol table -> short label
# --------------------------------------------------------------------------- #
# Keyed by the two-char "table + code" string aprslib reports as
# symbol_table + symbol. Only the common ones; anything else falls back to
# a generic label so the line still says something.
_SYMBOLS: dict[str, str] = {
    "/>": _N("car"),
    "/k": _N("truck"),
    "/j": _N("jeep"),
    "/v": _N("van"),
    "/u": _N("truck"),
    "/<": _N("motorcycle"),
    "/b": _N("bicycle"),
    "/-": _N("house"),
    "/y": _N("house (HF)"),
    "/R": _N("RV"),
    "/[": _N("person"),
    "/s": _N("boat"),
    "/Y": _N("yacht"),
    "/_": _N("weather station"),
    "/O": _N("balloon"),
    "/'": _N("aircraft"),
    "/^": _N("aircraft"),
    "/g": _N("glider"),
    "/*": _N("snowmobile"),
    "/I": _N("TCP/IP node"),
    "/&": _N("gateway"),
    "/#": _N("digipeater"),
    "\\#": _N("digipeater"),
    "/r": _N("repeater"),
    "/S": _N("satellite"),
    "\\S": _N("satellite"),
    "\\N": _N("weather service"),
    "/$": _N("phone"),
}

# MIC-E message type (aprslib "mtype" like "M0: Off Duty") -> label.
_MICE_MTYPE: dict[str, str] = {
    "M0": _N("off duty"),
    "M1": _N("en route"),
    "M2": _N("in service"),
    "M3": _N("returning"),
    "M4": _N("committed"),
    "M5": _N("special"),
    "M6": _N("priority"),
    "C0": _N("custom-0"),
    "C1": _N("custom-1"),
    "C2": _N("custom-2"),
    "C3": _N("custom-3"),
    "C4": _N("custom-4"),
    "C5": _N("custom-5"),
    "C6": _N("custom-6"),
    "Emergency": _N("EMERGENCY"),
}

# Digipeater path tokens that mean "went through a satellite / the ISS".
# Values are either an _N-marked phrase (translated at use) or a bare proper
# noun (left as-is).
_SAT_PATH_TOKENS = {
    "ARISS": _N("the ISS"),
    "RS0ISS": _N("the ISS"),
    "ISS": _N("the ISS"),
    "APRSAT": _N("a satellite"),
    "SGATE": _N("a satellite gateway"),
    "PSAT": "PSAT",
    "PSAT2": "PSAT2",
    "PCSAT": "PCSAT",
}
_SAT_PATH_XLATE = {_N("the ISS"), _N("a satellite"), _N("a satellite gateway")}

_COMPASS = [
    _N("N"),
    _N("NE"),
    _N("E"),
    _N("SE"),
    _N("S"),
    _N("SW"),
    _N("W"),
    _N("NW"),
]

# Leading TNC / Kenwood / Byonics type bytes and trailing Yaesu device
# codes that ride along in a MIC-E "comment" but are not user text.
_MICE_JUNK_LEAD = re.compile(r"^[\s`'>\]\[]+")
_MICE_JUNK_TRAIL = re.compile(r"[\s]*_[%\)\(\"#0-9A-Za-z]$")


def _compass(course: float) -> str:
    """8-point compass label for a course in degrees."""
    return _(_COMPASS[int((course % 360) / 45 + 0.5) % 8])


def _fmt_coords(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f}°{ns} {abs(lon):.4f}°{ew}"


def _symbol_label(parsed: dict[str, Any]) -> str:
    tbl = str(parsed.get("symbol_table") or "/")
    code = str(parsed.get("symbol") or "")
    key = f"{tbl}{code}"
    if key in _SYMBOLS:
        return _(_SYMBOLS[key])
    return _("station")


def _clean_mice_comment(text: str) -> str:
    text = _MICE_JUNK_LEAD.sub("", text or "")
    text = _MICE_JUNK_TRAIL.sub("", text)
    return text.strip()


def _sat_note(parsed: dict[str, Any]) -> str:
    """`` · via the ISS`` when the digipeater path went through a satellite."""
    path = parsed.get("path") or []
    for hop in path:
        token = str(hop).rstrip("*").upper()
        if token in _SAT_PATH_TOKENS:
            via = _SAT_PATH_TOKENS[token]
            if via in _SAT_PATH_XLATE:
                via = _(via)
            return _(" · via {via}").format(via=via)
    return ""


def _altitude_note(parsed: dict[str, Any]) -> str:
    alt = parsed.get("altitude")
    if isinstance(alt, int | float) and abs(alt) >= 30:
        return _(" · alt {m:.0f} m").format(m=float(alt))
    return ""


def _comment_note(text: str) -> str:
    text = (text or "").strip()
    return _(" · “{text}”").format(text=text) if text else ""


def _render_position(parsed: dict[str, Any]) -> str | None:
    lat = parsed.get("latitude")
    lon = parsed.get("longitude")
    if lat is None or lon is None:
        return None
    coords = _fmt_coords(float(lat), float(lon))
    icon = _symbol_label(parsed)
    speed = parsed.get("speed")
    course = parsed.get("course")
    cmt = parsed.get("comment") or ""
    if parsed.get("format") == "mic-e":
        cmt = _clean_mice_comment(cmt)

    weather = parsed.get("weather")
    if weather:
        return _("Weather · {coords}{wx}").format(coords=coords, wx=_weather_note(weather))

    tail = _altitude_note(parsed) + _comment_note(cmt) + _sat_note(parsed)

    if isinstance(speed, int | float) and speed >= 1.0:
        dir_txt = _compass(float(course)) if isinstance(course, int | float) else "?"
        line = _("{icon} moving · {coords} · {dir} {spd:.0f} km/h").format(
            icon=icon, coords=coords, dir=dir_txt, spd=float(speed)
        )
    else:
        line = _("{icon} · {coords}").format(icon=icon, coords=coords)

    mtype = str(parsed.get("mtype") or "")
    mkey = mtype.split(":")[0].strip()
    if mkey and mkey != "M0" and mkey in _MICE_MTYPE:
        line += _(" · {state}").format(state=_(_MICE_MTYPE[mkey]))

    return line + tail


def _weather_note(wx: dict[str, Any]) -> str:
    bits: list[str] = []
    t = wx.get("temperature")
    if isinstance(t, int | float):
        bits.append(_("{t:.1f}°C").format(t=float(t)))
    ws = wx.get("wind_speed")
    wd = wx.get("wind_direction")
    if isinstance(ws, int | float):
        if isinstance(wd, int | float):
            bits.append(_("wind {d}° {s:.0f} m/s").format(d=int(wd), s=float(ws)))
        else:
            bits.append(_("wind {s:.0f} m/s").format(s=float(ws)))
    h = wx.get("humidity")
    if isinstance(h, int | float):
        bits.append(_("humidity {h:.0f}%").format(h=float(h)))
    r = wx.get("rain_1h")
    if isinstance(r, int | float) and r > 0:
        bits.append(_("rain {r:.1f} mm/h").format(r=float(r)))
    return (" · " + " · ".join(bits)) if bits else ""


def _render_message(parsed: dict[str, Any]) -> str:
    frm = parsed.get("from", "?")
    to = parsed.get("addresse", "?")
    no = parsed.get("msgNo")
    response = parsed.get("response")
    if response == "ack":
        return _("{frm} → {to}: acknowledged message #{no}").format(frm=frm, to=to, no=no)
    if response == "rej":
        return _("{frm} → {to}: rejected message #{no}").format(frm=frm, to=to, no=no)
    text = (parsed.get("message_text") or "").strip()
    line = _("{frm} → {to}: “{text}”").format(frm=frm, to=to, text=text)
    if no:
        line += _(" (ack requested #{no})").format(no=no)
    return line


def _render_bulletin(parsed: dict[str, Any]) -> str:
    frm = parsed.get("from", "?")
    bid = parsed.get("bid", "")
    text = (parsed.get("message_text") or "").strip()
    return _("Bulletin {bid} from {frm}: “{text}”").format(bid=bid, frm=frm, text=text)


def _render_object(parsed: dict[str, Any]) -> str | None:
    name = str(parsed.get("object_name") or parsed.get("item_name") or "").strip()
    lat = parsed.get("latitude")
    lon = parsed.get("longitude")
    coords = _fmt_coords(float(lat), float(lon)) if lat is not None and lon is not None else ""
    killed = parsed.get("alive") is False
    cmt = _comment_note(parsed.get("comment") or "")
    if killed:
        return _("Object {name} (killed) · {coords}").format(name=name, coords=coords)
    return _("Object {name} · {coords}{cmt}").format(name=name, coords=coords, cmt=cmt)


def _render_telemetry(info: str) -> str | None:
    # T#SEQ,a1,a2,a3,a4,a5,bbbbbbbb   (SEQ may be "MIC" or digits)
    m = re.match(
        r"T#(\w+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),([01]{0,8})",
        info.strip(),
    )
    if not m:
        return None
    seq = m.group(1)
    vals = ", ".join(m.group(i).lstrip("0") or "0" for i in range(2, 7))
    bits = m.group(7)
    line = _("Telemetry #{seq}: {vals}").format(seq=seq, vals=vals)
    if bits:
        line += _(" (bits {bits})").format(bits=bits)
    return line


def _render(parsed: dict[str, Any], info: str) -> str | None:
    fmt = parsed.get("format")
    if fmt == "thirdparty":
        sub = parsed.get("subpacket") or {}
        sub_info = str(sub.get("raw", ""))
        sub_info = sub_info.split(":", 1)[1] if ":" in sub_info else sub_info
        inner = _render(sub, sub_info)
        if inner is None:
            return None
        return inner + _sat_note(parsed)
    if fmt in ("uncompressed", "compressed", "mic-e"):
        return _render_position(parsed)
    if fmt == "message":
        return _render_message(parsed)
    if fmt == "bulletin":
        return _render_bulletin(parsed)
    if fmt in ("object", "item"):
        return _render_object(parsed)
    if fmt == "status":
        return _("Status: “{text}”").format(text=str(parsed.get("status", "")).strip())
    if info.startswith("T#"):
        return _render_telemetry(info)
    return None


def humanize_tnc2(tnc2: str) -> str | None:
    """Render a TNC2-format APRS string (``SRC>DEST,path:info``) as one line.

    Returns None when the packet can't be turned into a useful sentence.
    """
    info = tnc2.split(":", 1)[1] if ":" in tnc2 else ""
    try:
        parsed = aprslib.parse(tnc2)
    except Exception:
        # aprslib rejects a few types outright (e.g. "T#" telemetry reports).
        # Handle the ones worth a sentence ourselves before giving up.
        return _render_telemetry(info) if info.startswith("T#") else None
    try:
        return _render(parsed, info)
    except Exception:
        return None


def humanize_frame(frame: Any) -> str | None:
    """Render an :class:`~comms.aprs.parser.Ax25Frame` as one plain line.

    Uses latin-1 to reconstruct the info field so every original byte
    reaches aprslib intact (MIC-E position bytes must not be mangled by a
    lossy UTF-8 decode).
    """
    try:
        info = frame.payload.decode("latin-1")
        path = ",".join(frame.via)
        hdr = f"{frame.src}>{frame.dest}"
        if path:
            hdr += f",{path}"
        return humanize_tnc2(f"{hdr}:{info}")
    except Exception:
        return None
