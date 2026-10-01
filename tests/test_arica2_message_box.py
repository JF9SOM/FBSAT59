"""ARICA-2 message-box frame builder/parser (layout from N6RFM's reverse engineering)."""

from __future__ import annotations

import pytest

from comms.arica2.message_box import Command, build_command, parse_downlink


def test_upload_frame_layout() -> None:
    frame = build_command(Command.UPLOAD, "jf9som", "hello")
    assert len(frame) == 19
    assert frame[:3] == bytes([0x42, 0xF8, 0xBD])
    assert frame[3:5] == bytes([0x50, 0x00])
    assert frame[5:11] == b"JF9SOM"
    assert frame[11:] == b"HELLO\x00\x00\x00"


@pytest.mark.parametrize(
    ("command", "expected"),
    [(Command.CONFIRM, bytes([0x60, 0x00])), (Command.PARROT, bytes([0x70, 0x00]))],
)
def test_fixed_command_bytes(command: Command, expected: bytes) -> None:
    assert build_command(command, "JF9SOM", "x")[3:5] == expected


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        (1, bytes([0x40, 0x80])),
        (2, bytes([0x41, 0x00])),
        (3, bytes([0x41, 0x80])),
        (20, bytes([0x4A, 0x00])),
    ],
)
def test_download_slot_encoding(slot: int, expected: bytes) -> None:
    assert build_command(Command.DOWNLOAD, "JF9SOM", slot=slot)[3:5] == expected


@pytest.mark.parametrize("slot", [None, 0, 21])
def test_download_rejects_bad_slot(slot: int | None) -> None:
    with pytest.raises(ValueError):
        build_command(Command.DOWNLOAD, "JF9SOM", slot=slot)


def test_rejects_overlong_or_non_ascii_fields() -> None:
    with pytest.raises(ValueError):
        build_command(Command.UPLOAD, "JF9SOMX", "hi")
    with pytest.raises(ValueError):
        build_command(Command.UPLOAD, "JF9SOM", "123456789")
    with pytest.raises(ValueError):
        build_command(Command.UPLOAD, "JF9SOM", "こんにちは")
    with pytest.raises(ValueError):
        build_command(Command.UPLOAD, "  ", "hi")


def _shifted(text: str, length: int) -> bytes:
    return bytes((ord(c) << 1) for c in text.ljust(length))


def test_parse_downlink() -> None:
    payload = (
        _shifted("N6RFM", 5)
        + _shifted("JS1YSD", 6)
        + bytes([0x61, 0x03, 0xF0])
        + b"saved '' at box: 3"
    )
    parsed = parse_downlink(payload)
    assert parsed is not None
    assert parsed.dest == "N6RFM"
    assert parsed.source == "JS1YSD"
    assert parsed.text == "saved '' at box: 3"


def test_parse_downlink_rejects_other_frames() -> None:
    assert parse_downlink(b"short") is None
    not_ui = _shifted("N6RFM", 5) + _shifted("JS1YSD", 6) + bytes([0x61, 0x13, 0xF0]) + b"x"
    assert parse_downlink(not_ui) is None
