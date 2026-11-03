"""Tests for comms.telemetry.file_assembler (OrigamiSat-2 ID68 file reassembly)."""

from __future__ import annotations

import random

from comms.telemetry.decoder import load_format
from comms.telemetry.file_assembler import FileAssembler, FileTransferDef, detect_kind, parse_block

SPEC = FileTransferDef(
    telemetry_id=68, number_offset=12, number_length=2, data_offset=14, data_length=190
)


def _payload(number: int, data: bytes) -> bytes:
    """A 206-byte ID68 payload: 12-byte header, block number, 190 data bytes, 2-byte CRC."""
    header = bytes([0xCB, 0xFE, 68, number & 0xFF]) + bytes(8)
    return header + number.to_bytes(2, "big") + data.ljust(190, b"\x00") + b"\x00\x00"


def _jpeg(size: int) -> bytes:
    rng = random.Random(1)
    body = bytes(b if b != 0xFF else 0xFE for b in (rng.randrange(256) for _ in range(size)))
    return b"\xff\xd8" + body + b"\xff\xd9"


def _split(data: bytes) -> list[bytes]:
    return [data[i : i + 190] for i in range(0, len(data), 190)]


def test_format_file_has_file_transfer_definition() -> None:
    fmt = load_format(68795)
    assert fmt is not None
    assert FileTransferDef.from_json(fmt["file_transfer"]) == SPEC


def test_real_frame_block_number_is_16_bit() -> None:
    # Captured 2026-11-03: bytes 12-13 = 07 EF, telemetry count 0xF0.
    head = bytes.fromhex("cbfe44f06a8c88551302000207ef")
    parsed = parse_block(head + bytes(190) + bytes(2), SPEC)
    assert parsed is not None
    assert parsed[0] == 0x07EF


def test_other_telemetry_id_is_ignored() -> None:
    payload = bytearray(_payload(5, b"x"))
    payload[2] = 1
    assert parse_block(bytes(payload), SPEC) is None
    assert parse_block(b"\x00" * 10, SPEC) is None


def test_reassemble_out_of_order_with_duplicates() -> None:
    original = _jpeg(1000)
    blocks = _split(original)
    asm = FileAssembler(SPEC)
    order = list(range(len(blocks)))
    random.Random(2).shuffle(order)
    for n in order + order[:3]:
        asm.add_payload(_payload(100 + n, blocks[n]))
    assert len(asm.blocks) == len(blocks)
    assert asm.start_blocks() == [100]
    built = asm.build(100)
    assert built is not None
    assert built.startswith(original)
    assert detect_kind(built) == "jpg"
    assert asm.is_complete_jpeg(100)


def test_duplicate_block_is_not_new() -> None:
    asm = FileAssembler(SPEC)
    assert asm.add_payload(_payload(7, b"a")) is True
    assert asm.add_payload(_payload(7, b"a")) is False


def test_gap_blocks_build_unless_filled() -> None:
    blocks = _split(_jpeg(800))
    asm = FileAssembler(SPEC)
    for n, b in enumerate(blocks):
        if n != 2:
            asm.add_payload(_payload(n, b))
    assert [(r.first, r.last) for r in asm.runs()] == [(0, 1), (3, len(blocks) - 1)]
    assert asm.build(0, len(blocks) - 1) is None
    filled = asm.build(0, len(blocks) - 1, fill_gaps=True)
    assert filled is not None
    assert filled[2 * 190 : 3 * 190] == bytes(190)
    assert not asm.is_complete_jpeg(0)


def test_detect_kind_avi_and_unknown() -> None:
    assert detect_kind(b"RIFF\x00\x00\x00\x00AVI LIST") == "avi"
    assert detect_kind(b"\x01\x02\x03") == "bin"


def test_real_frames_form_two_runs() -> None:
    asm = FileAssembler(SPEC)
    for n in (0x0604, 0x0607, 0x0608, 0x0609, 0x07EF, 0x07F0):
        asm.add_payload(_payload(n, bytes([n & 0xFF])))
    assert [(r.first, r.last) for r in asm.runs()] == [
        (0x0604, 0x0604),
        (0x0607, 0x0609),
        (0x07EF, 0x07F0),
    ]
    assert asm.start_blocks() == []
    assert "6 blocks, 3 run(s)" in asm.summary()


def test_files_page_lists_runs_from_log(qtbot: object) -> None:
    import sqlite3

    from ui.telemetry_files_page import TelemetryFilesPage

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE telemetry_log (norad_cat_id INTEGER, raw_hex TEXT)")
    blocks = _split(_jpeg(500))
    for n, b in enumerate(blocks):
        conn.execute("INSERT INTO telemetry_log VALUES (68795, ?)", (_payload(10 + n, b).hex(),))
    conn.execute("INSERT INTO telemetry_log VALUES (68795, ?)", (_payload(900, b"zz").hex(),))
    page = TelemetryFilesPage()
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.refresh(conn, 68795)
    assert page._list.count() == 2
    assert "JPEG image" in page._list.item(0).text()
    assert "complete" in page._list.item(0).text()
    assert "Fragment" in page._list.item(1).text()
