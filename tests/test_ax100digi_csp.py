"""Unit tests for comms/ax100digi/csp.py's CSP v1 header pack/parse."""

from __future__ import annotations

import struct

from comms.ax100digi.csp import CspHeader, build_csp_packet, parse_csp_header, split_csp_packet


def test_pack_parse_roundtrip() -> None:
    header = CspHeader(
        priority=1,
        source=5,
        destination=10,
        dest_port=30,
        source_port=20,
        reserved=0,
        hmac=False,
        xtea=False,
        rdp=False,
        crc=True,
    )
    packed = header.pack()
    assert len(packed) == 4

    parsed = parse_csp_header(packed)
    assert parsed == header


def test_split_csp_packet_separates_header_and_payload() -> None:
    header = CspHeader(priority=0, source=1, destination=2, dest_port=3, source_port=4)
    payload = b"hello satellite"
    packet = build_csp_packet(header, payload)

    parsed_header, parsed_payload = split_csp_packet(packet)
    assert parsed_header == header
    assert parsed_payload == payload


def test_all_fields_independently_addressable() -> None:
    # Every field packed at its own bit position with no bleed into neighbors.
    header = CspHeader(
        priority=3,
        source=31,
        destination=31,
        dest_port=63,
        source_port=63,
        reserved=15,
        hmac=True,
        xtea=True,
        rdp=True,
        crc=True,
    )
    (value,) = struct.unpack("!I", header.pack())
    assert value == 0xFFFFFFFF
