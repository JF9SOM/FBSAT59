"""Skylink frames and the AX.25 frames of Foresail-1p's repeater channel."""

from __future__ import annotations

import pytest

from comms.foresail.skylink import (
    VC_REPEATER,
    build_repeater_frame,
    build_skylink_frame,
    encode_ax25_address,
    max_info_length,
    parse_repeater_frame,
    parse_skylink_frame,
    skylink_crc32,
    tdd_extension,
)

# Two real satellite frames (RS decoded, 2026-10-18 01:32 UTC): UHF and ADCS housekeeping.
_UHF = bytes.fromhex(
    "664f48324631530985a7054400fa00020b340b34003f10030 46ad42186fa68000090000501".replace(" ", "")
    + "00000000"
    "00ff07000000000000000000000000000068526b52010101fa000000fa0000000b00"
    "0600c9ff0800000000"
    "0000fe6fd9a4"
)


def test_parse_real_satellite_frame() -> None:
    frame = parse_skylink_frame(_UHF)
    assert frame is not None
    assert frame.identity == "OH2F1S" and frame.version == 12
    assert frame.vc == 1 and frame.authenticated and not frame.crced and not frame.arq_on
    assert frame.frame_sequence == 0x85A7
    assert frame.tdd == (250, 2)
    assert frame.auth == bytes.fromhex("fe6fd9a4")
    assert frame.payload[7:9] == bytes([3, 4])  # PUS service 3, subtype 4 (UHF housekeeping)


def test_parse_rejects_non_skylink() -> None:
    assert parse_skylink_frame(b"") is None
    assert parse_skylink_frame(bytes(10)) is None
    assert parse_skylink_frame(bytes([0x66]) + b"OH2F1S") is None  # truncated header


def test_build_and_parse_uplink_frame() -> None:
    payload = b"\x82\xa0\xa4"
    raw = build_skylink_frame(payload, vc=VC_REPEATER, identity="PyGS", sequence=7)
    assert raw[0] == 0x64  # version 12, 4-character identity
    frame = parse_skylink_frame(raw)
    assert frame is not None
    assert (frame.identity, frame.vc, frame.frame_sequence) == ("PyGS", 3, 7)
    assert not frame.authenticated and not frame.crced and frame.tdd is None
    assert frame.payload == payload


def test_uplink_frame_with_tdd_extension() -> None:
    raw = build_skylink_frame(b"x", extensions=(tdd_extension(250, 100),))
    frame = parse_skylink_frame(raw)
    assert frame is not None and frame.tdd == (250, 100) and frame.payload == b"x"


def test_uplink_identity_must_differ_from_the_satellites() -> None:
    with pytest.raises(ValueError):
        build_skylink_frame(b"x", identity="OH2F1S")
    with pytest.raises(ValueError):
        build_skylink_frame(b"x", identity="")
    with pytest.raises(ValueError):
        build_skylink_frame(b"x", identity="TOOLONG12")


def test_frame_crc_is_checked_when_present() -> None:
    body = bytes([0x66]) + b"OH2F1S" + bytes([0x13, 0, 1, 0]) + b"payload"
    raw = body + skylink_crc32(body).to_bytes(4, "big")
    frame = parse_skylink_frame(raw)
    assert frame is not None and frame.crced and frame.crc_ok is True
    assert frame.payload == b"payload" and frame.vc == 3
    bad = body + (skylink_crc32(body) ^ 1).to_bytes(4, "big")
    broken = parse_skylink_frame(bad)
    assert broken is not None and broken.crc_ok is False


def test_ax25_address_encoding() -> None:
    assert encode_ax25_address("CQ", 0) == bytes([0x86, 0xA2, 0x40, 0x40, 0x40, 0x40, 0x60])
    assert encode_ax25_address("N0CALL", 15, last=True)[-1] == 0x60 | (15 << 1) | 1
    with pytest.raises(ValueError):
        encode_ax25_address("TOOLONG1")
    with pytest.raises(ValueError):
        encode_ax25_address("A B")


def test_repeater_frame_roundtrip_with_digipeaters() -> None:
    raw = build_repeater_frame("APRS", "JF9SOM-7", b">hello", ("WIDE1-1", "WIDE2-1"))
    # No HDLC flags; the frame starts with the shifted destination callsign.
    assert raw[:4] == bytes([0x82, 0xA0, 0xA4, 0xA6])
    frame = parse_repeater_frame(raw)
    assert frame is not None
    assert str(frame.destination) == "APRS" and str(frame.source) == "JF9SOM-7"
    assert frame.via == "WIDE1-1,WIDE2-1"
    assert (frame.control, frame.pid, frame.text) == (0x03, 0xF0, ">hello")
    assert frame.fcs_status == "ok"


def test_repeater_fcs_is_most_significant_byte_first() -> None:
    from comms.aprs.g3ruh_tx import fcs16

    raw = build_repeater_frame("CQ", "N0CALL", b"hi")
    assert raw[-2:] == fcs16(raw[:-2]).to_bytes(2, "big")


def test_satellites_repeat_has_only_the_second_fcs_byte_right() -> None:
    raw = bytearray(build_repeater_frame("APRS", "N0CALL", b"x"))
    raw[-2] ^= 0xFF  # the satellite keeps the first FCS byte of the original frame
    frame = parse_repeater_frame(bytes(raw))
    assert frame is not None and frame.fcs_status == "second-byte-ok"
    raw[-1] ^= 0xFF
    broken = parse_repeater_frame(bytes(raw))
    assert broken is not None and broken.fcs_status == "bad"


def test_repeated_digipeater_is_marked() -> None:
    raw = bytearray(build_repeater_frame("APRS", "N0CALL", b"x", ("OH2F1S-11",)))
    raw[14 + 6] |= 0x80  # has-been-repeated bit of the digipeater SSID byte
    frame = parse_repeater_frame(bytes(raw[:-2]) + b"\x00\x00")
    assert frame is not None and frame.via == "OH2F1S-11*"


def test_information_field_limit() -> None:
    assert max_info_length(0) == 110
    assert max_info_length(8) == 110 - 56
    build_repeater_frame("CQ", "N0CALL", b"a" * 110)
    with pytest.raises(ValueError):
        build_repeater_frame("CQ", "N0CALL", b"a" * 111)
    with pytest.raises(ValueError):
        build_repeater_frame("CQ", "N0CALL", b"a", tuple(f"D{i}" for i in range(9)))


def test_uplink_bits_start_with_the_preamble_and_the_skylink_sync_word() -> None:
    pytest.importorskip("reed_solomon_ccsds")
    import numpy as np

    from comms.ax100digi.frame import SKYLINK_SYNCWORD, find_frames
    from comms.foresail.tx import build_uplink, uplink_bits

    skylink, _audio = build_uplink("APRS", "N0CALL", b"hi")
    bits = uplink_bits(skylink)
    assert bits[:8].tolist() == [1, 0, 1, 0, 1, 0, 1, 0]  # 0xAA
    sync = np.array([int(c) for c in SKYLINK_SYNCWORD], dtype=np.uint8)
    assert bits[16 * 8 : 16 * 8 + 32].tolist() == sync.tolist()
    frames = list(
        find_frames(bits, syncword=SKYLINK_SYNCWORD, forced_flags=(True, True), rs_dual_basis=False)
    )
    assert len(frames) == 1 and frames[0].payload == skylink
    assert frames[0].frame_len == len(skylink) + 32  # Golay length = payload + RS parity
