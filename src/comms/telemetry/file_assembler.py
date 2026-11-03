"""Reassemble a file downlinked as numbered telemetry packets (OrigamiSat-2 ID68).

OrigamiSat-2's ID68 packets carry a JPEG image or an AVI video cut into fixed-size
blocks (ORI-2-0027-OPR 3.2.10). Real frames show a 16-bit big-endian block number at
bytes 12-13 (the specification's "packet number + total packets" bytes do not fit
them), followed by 190 data bytes. A pass delivers only a few blocks, so blocks are
collected across passes by number and written out once a contiguous run is complete.

Pure logic: no Qt, no database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


@dataclass(frozen=True)
class FileTransferDef:
    """Where the block number and data sit in a payload (``file_transfer`` in the format JSON)."""

    telemetry_id: int
    number_offset: int
    number_length: int
    data_offset: int
    data_length: int
    id_offset: int = 2

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> FileTransferDef | None:
        """Build from the format file's ``file_transfer`` object, or None when absent."""
        if not raw:
            return None
        return cls(
            telemetry_id=int(raw["telemetry_id"]),
            number_offset=int(raw["number_offset"]),
            number_length=int(raw["number_length"]),
            data_offset=int(raw["data_offset"]),
            data_length=int(raw["data_length"]),
            id_offset=int(raw.get("id_offset", 2)),
        )


def parse_block(payload: bytes, spec: FileTransferDef) -> tuple[int, bytes] | None:
    """Return ``(block number, data)`` of a payload, or None if it is not a data block."""
    end = spec.data_offset + spec.data_length
    if len(payload) < end or payload[spec.id_offset] != spec.telemetry_id:
        return None
    number = int.from_bytes(
        payload[spec.number_offset : spec.number_offset + spec.number_length], "big"
    )
    return number, bytes(payload[spec.data_offset : end])


def detect_kind(data: bytes) -> str:
    """``"jpg"``, ``"avi"`` or ``"bin"`` from the first bytes of a file."""
    if data.startswith(JPEG_SOI):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "avi"
    return "bin"


@dataclass
class FileRun:
    """A contiguous run of block numbers."""

    first: int
    last: int

    @property
    def count(self) -> int:
        """Number of blocks in the run."""
        return self.last - self.first + 1


@dataclass
class FileAssembler:
    """Blocks collected by number; the first block of a file starts with its magic bytes."""

    spec: FileTransferDef
    blocks: dict[int, bytes] = field(default_factory=dict)

    def add_payload(self, payload: bytes) -> bool:
        """Store one payload's block. True if it was a new block of this transfer."""
        parsed = parse_block(payload, self.spec)
        if parsed is None:
            return False
        number, data = parsed
        if number in self.blocks:
            return False
        self.blocks[number] = data
        return True

    def runs(self) -> list[FileRun]:
        """Contiguous runs of received block numbers, in order."""
        out: list[FileRun] = []
        for n in sorted(self.blocks):
            if out and n == out[-1].last + 1:
                out[-1].last = n
            else:
                out.append(FileRun(n, n))
        return out

    def start_blocks(self) -> list[int]:
        """Numbers of blocks that look like the first block of a JPEG or AVI."""
        return sorted(n for n, d in self.blocks.items() if detect_kind(d) != "bin")

    def build(self, first: int, last: int | None = None, fill_gaps: bool = False) -> bytes | None:
        """Concatenate blocks *first*..*last* (default: through the end of its run).

        With a missing block inside the range this returns None, or - when *fill_gaps* -
        zero bytes in place of it.
        """
        if first not in self.blocks:
            return None
        if last is None:
            last = next((r.last for r in self.runs() if r.first <= first <= r.last), first)
        size = self.spec.data_length
        parts: list[bytes] = []
        for n in range(first, last + 1):
            block = self.blocks.get(n)
            if block is None:
                if not fill_gaps:
                    return None
                block = b"\x00" * size
            parts.append(block)
        return b"".join(parts)

    def is_complete_jpeg(self, first: int) -> bool:
        """True if the run starting at *first* is a JPEG that ends with EOI."""
        data = self.build(first)
        if data is None or not data.startswith(JPEG_SOI):
            return False
        return JPEG_EOI in data.rstrip(b"\x00")[-(self.spec.data_length + 2) :]

    def summary(self) -> str:
        """One line: blocks held, runs, and where files appear to start."""
        runs = self.runs()
        starts = self.start_blocks()
        text = f"{len(self.blocks)} blocks, {len(runs)} run(s)"
        if runs:
            text += ": " + ", ".join(f"{r.first}-{r.last}" for r in runs[:8])
            if len(runs) > 8:
                text += ", ..."
        if starts:
            text += "; file start at " + ", ".join(str(s) for s in starts[:8])
        return text
