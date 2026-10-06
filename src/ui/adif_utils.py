"""
Shared ADIF log export utilities.

All communication tabs (APRS, FT4, Q65) use a common filename ``log_YYYYMMDD.adi``
and append to it when the file already exists so that a single day's QSOs from
different modes end up in one file.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

_ADIF_HEADER = "<ADIF_VER:5>3.1.4\n<PROGRAMID:7>FBSAT59\n<EOH>\n\n"


def adif_field(tag: str, value: str) -> str:
    """Return one ADIF field token, e.g. ``<CALL:6>JF9SOM``, or "" if *value* is blank."""
    v = str(value).strip()
    return f"<{tag}:{len(v)}>{v}" if v else ""


def build_adif_record(fields: dict[str, str]) -> str:
    """Build one ADIF record (ending in ``<EOR>``) from a tag->value mapping.

    Keys are emitted in insertion order; blank values are omitted. Shared by
    the manual log export dialog and the real-time UDP log broadcaster so
    both produce identical ADIF output for the same QSO.
    """
    tokens = [t for t in (adif_field(tag, value) for tag, value in fields.items()) if t]
    tokens.append("<EOR>")
    return " ".join(tokens) + "\n"


# ADIF band enumeration (MHz ranges), the band names LoTW's BAND field takes.
_ADIF_BANDS: tuple[tuple[str, float, float], ...] = (
    ("2190M", 0.1357, 0.1378),
    ("630M", 0.472, 0.479),
    ("560M", 0.501, 0.504),
    ("160M", 1.8, 2.0),
    ("80M", 3.5, 4.0),
    ("60M", 5.06, 5.45),
    ("40M", 7.0, 7.3),
    ("30M", 10.1, 10.15),
    ("20M", 14.0, 14.35),
    ("17M", 18.068, 18.168),
    ("15M", 21.0, 21.45),
    ("12M", 24.89, 24.99),
    ("10M", 28.0, 29.7),
    ("8M", 40.0, 45.0),
    ("6M", 50.0, 54.0),
    ("5M", 54.000001, 69.9),
    ("4M", 70.0, 71.0),
    ("2M", 144.0, 148.0),
    ("1.25M", 222.0, 225.0),
    ("70CM", 420.0, 450.0),
    ("33CM", 902.0, 928.0),
    ("23CM", 1240.0, 1300.0),
    ("13CM", 2300.0, 2450.0),
    ("9CM", 3300.0, 3500.0),
    ("6CM", 5650.0, 5925.0),
    ("3CM", 10000.0, 10500.0),
    ("1.25CM", 24000.0, 24250.0),
    ("6MM", 47000.0, 47200.0),
)


def adif_band(freq_hz: float | None) -> str:
    """ADIF band name for a frequency in Hz ("2M", "70CM", ...), or "" if none matches."""
    if not freq_hz or freq_hz <= 0:
        return ""
    mhz = freq_hz / 1e6
    for name, low, high in _ADIF_BANDS:
        if low <= mhz <= high:
            return name
    return ""


def _mhz(freq_hz: float | None) -> str:
    return f"{freq_hz / 1e6:.6f}" if freq_hz else ""


def build_satellite_record(
    *,
    call: str,
    qso_date: str,
    time_on: str,
    time_off: str = "",
    mode: str,
    sat_name: str,
    freq_hz: float | None = None,
    freq_rx_hz: float | None = None,
    rst_sent: str = "",
    rst_rcvd: str = "",
    gridsquare: str = "",
    comment: str = "",
) -> str:
    """One satellite-QSO ADIF record in the form LoTW accepts.

    LoTW's essentials are CALL, QSO_DATE, TIME_ON, BAND and MODE, plus, for a
    satellite, PROP_MODE=SAT and a SAT_NAME that is exactly an ID on its list
    (https://lotw.arrl.org/lotw-help/satellite-qsos/). BAND is the uplink band and
    BAND_RX the downlink band; FREQ/FREQ_RX are optional and only carry the band's
    MHz here. MODE must be one LoTW lists (FT4, PACKET, DATA, ...).
    """
    return build_adif_record(
        {
            "CALL": call,
            "QSO_DATE": qso_date,
            "TIME_ON": time_on,
            "TIME_OFF": time_off or time_on,
            "BAND": adif_band(freq_hz),
            "BAND_RX": adif_band(freq_rx_hz),
            "FREQ": _mhz(freq_hz),
            "FREQ_RX": _mhz(freq_rx_hz),
            "MODE": mode,
            "PROP_MODE": "SAT",
            "SAT_NAME": sat_name,
            "RST_SENT": rst_sent,
            "RST_RCVD": rst_rcvd,
            "GRIDSQUARE": gridsquare,
            "COMMENT": comment,
        }
    )


def adif_default_filename() -> str:
    """Return today's shared ADIF log filename, e.g. ``log_20260627.adi``."""
    return f"log_{datetime.now(tz=UTC).strftime('%Y%m%d')}.adi"


def adif_write_or_append(path: str, records: str) -> None:
    """Write *records* to *path*, creating the file with an ADIF header if needed.

    When the file already exists the records are appended without a second
    header so that multi-mode QSOs from the same day accumulate in one file.

    Args:
        path:    Destination file path.
        records: One or more ADIF record strings (ending with ``<EOR>``).
    """
    if os.path.exists(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(records)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_ADIF_HEADER)
            fh.write(records)
