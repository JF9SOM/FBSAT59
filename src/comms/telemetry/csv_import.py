"""Frames from a CSV downloaded from SatNOGS DB, for the Telemetry tab.

SatNOGS DB (the satellite page's "Download frames") gives one line per frame decoded by the
stations of the network::

    2026-10-18 11:17:08|B316F17A...89||KM7DOS-CN87xi

(time in UTC, the frame as hex, an empty field, the observer). Several stations usually report
the same transmission, so the same frame appears more than once. The lines are returned
oldest first (the file is newest first).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class CsvFrame:
    """One frame of the CSV."""

    when: datetime
    frame: bytes
    observer: str


def parse_line(line: str) -> CsvFrame | None:
    """A CSV line as a frame, or None if it is not a frame line."""
    parts = line.strip().split("|")
    if len(parts) < 2:
        return None
    try:
        when = datetime.fromisoformat(parts[0].strip().replace("Z", "+00:00"))
        frame = bytes.fromhex(parts[1].strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return CsvFrame(when, frame, parts[-1].strip() if len(parts) > 2 else "")


def read_frames(path: str | Path) -> tuple[int, list[CsvFrame]]:
    """``(non-empty lines, frames oldest first)`` of a SatNOGS frames CSV."""
    lines = 0
    frames: list[CsvFrame] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            lines += 1
            parsed = parse_line(line)
            if parsed is not None:
                frames.append(parsed)
    frames.sort(key=lambda f: f.when)
    return lines, frames


def unique_frames(frames: list[CsvFrame]) -> Iterator[CsvFrame]:
    """The frames without the copies of other stations (same time to the second, same bytes)."""
    seen: set[tuple[datetime, bytes]] = set()
    for f in frames:
        key = (f.when, f.frame)
        if key not in seen:
            seen.add(key)
            yield f
