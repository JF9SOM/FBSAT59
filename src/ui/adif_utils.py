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
