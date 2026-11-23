"""Export the frames saved in ``telemetry_log`` as CSV or as text other tools read.

The Telemetry tab logs every received frame. This module turns those rows into one of
three text formats, for sharing frames (for example a satellite team's reception report):

``csv``
    One row per frame: UTC time, NORAD, callsign, kind, signal, whole-frame flag, hex.
``kiss``
    One KISS data frame per line, ``C0 00 <frame, escaped> C0`` (hex bytes with spaces).
    ``C0`` becomes ``DB DC`` and ``DB`` becomes ``DB DD`` inside the frame, as KISS
    requires. This is the text that ARICA-2's reconstruction software reads.
``satnogs``
    ``YYYY-MM-DD HH:MM:SS|<frame hex>||``, the line format of a SatNOGS DB frame download
    (the Telemetry tab's "Import CSV…" reads it back).

Rows logged before the whole frame was saved have only the payload (``whole`` is False).
They are written to the CSV (marked as payload only) and left out of the other two
formats, because a payload alone is not a frame.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

FORMAT_CSV = "csv"
FORMAT_KISS = "kiss"
FORMAT_SATNOGS = "satnogs"
FORMATS = (FORMAT_CSV, FORMAT_KISS, FORMAT_SATNOGS)

_FEND = 0xC0
_FESC = 0xDB
_TFEND = 0xDC
_TFESC = 0xDD

CSV_HEADER = ("received_at_utc", "norad", "callsign", "kind", "signal_db", "whole_frame", "hex")


@dataclass(frozen=True)
class LoggedFrame:
    """One saved frame of the ``telemetry_log`` table."""

    when: datetime
    norad: int | None
    callsign: str
    kind: str
    signal_db: float | None
    hex_text: str
    whole: bool


def effective_kind(kind: str | None, callsign: str) -> str:
    """Kind of a logged row; a row from before the ``kind`` column is inferred.

    A row without a callsign is an HDLC frame that is not AX.25; one with a callsign is AX.25.
    """
    if kind:
        return kind
    return "ax25" if callsign else "hdlc"


def kiss_escape(frame: bytes) -> bytes:
    """*frame* with ``C0`` -> ``DB DC`` and ``DB`` -> ``DB DD`` (KISS transposition)."""
    out = bytearray()
    for b in frame:
        if b == _FEND:
            out += bytes((_FESC, _TFEND))
        elif b == _FESC:
            out += bytes((_FESC, _TFESC))
        else:
            out.append(b)
    return bytes(out)


def kiss_line(frame: bytes) -> str:
    """One KISS data frame (port 0) as hex bytes with spaces: ``C0 00 .. C0``."""
    data = bytes((_FEND, 0x00)) + kiss_escape(frame) + bytes((_FEND,))
    return " ".join(f"{b:02X}" for b in data)


def _parse_time(text: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def select_frames(
    conn: sqlite3.Connection,
    norad: int,
    start: datetime,
    end: datetime,
    kinds: Iterable[str],
) -> list[LoggedFrame]:
    """The logged frames of *norad* with ``start <= time < end`` and a kind in *kinds*.

    Oldest first. Rows with no NORAD are not included.
    """
    wanted = set(kinds)
    rows = conn.execute(
        "SELECT received_at, norad_cat_id, callsign, raw_hex, frame_hex, kind, signal_db"
        " FROM telemetry_log WHERE norad_cat_id = ? ORDER BY received_at, id",
        (norad,),
    ).fetchall()
    frames: list[LoggedFrame] = []
    for received, row_norad, callsign, raw_hex, frame_hex, kind, signal in rows:
        when = _parse_time(str(received))
        if when is None or not (start <= when < end):
            continue
        call = str(callsign or "")
        row_kind = effective_kind(kind, call)
        if row_kind not in wanted:
            continue
        whole = bool(frame_hex)
        text = str(frame_hex if whole else (raw_hex or "")).strip().upper()
        if not text:
            continue
        frames.append(
            LoggedFrame(
                when=when,
                norad=row_norad,
                callsign=call,
                kind=row_kind,
                signal_db=None if signal is None else float(signal),
                hex_text=text,
                whole=whole,
            )
        )
    return frames


def _utc(when: datetime) -> datetime:
    return when.astimezone(UTC)


def format_csv(frames: Sequence[LoggedFrame]) -> str:
    """The frames as CSV text (all of them, payload-only rows included)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for f in frames:
        writer.writerow(
            [
                _utc(f.when).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "" if f.norad is None else f.norad,
                f.callsign,
                f.kind,
                "" if f.signal_db is None else f"{f.signal_db:g}",
                1 if f.whole else 0,
                f.hex_text,
            ]
        )
    return buf.getvalue()


def export_text(frames: Sequence[LoggedFrame], fmt: str) -> tuple[str, int, int]:
    """``(text, frames written, frames left out)`` for format *fmt*.

    Only ``csv`` keeps payload-only rows; ``kiss`` and ``satnogs`` leave them out (they are
    counted in the last number).
    """
    if fmt == FORMAT_CSV:
        return format_csv(frames), len(frames), 0
    if fmt not in (FORMAT_KISS, FORMAT_SATNOGS):
        raise ValueError(f"unknown export format: {fmt}")
    lines: list[str] = []
    skipped = 0
    for f in frames:
        if not f.whole:
            skipped += 1
            continue
        try:
            data = bytes.fromhex(f.hex_text)
        except ValueError:
            skipped += 1
            continue
        if fmt == FORMAT_KISS:
            lines.append(kiss_line(data))
        else:
            lines.append(f"{_utc(f.when).strftime('%Y-%m-%d %H:%M:%S')}|{data.hex().upper()}||")
    text = "\n".join(lines) + ("\n" if lines else "")
    return text, len(lines), skipped


def default_filename(norad: int, name: str, fmt: str, when: datetime) -> str:
    """A file name such as ``68796_ARICA-2_20261123.csv`` (``.txt`` for the text formats)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip()).strip("_")
    stem = f"{norad}_{safe}" if safe else str(norad)
    ext = "csv" if fmt == FORMAT_CSV else "txt"
    return f"{stem}_{_utc(when).strftime('%Y%m%d')}.{ext}"
