"""CSP (Cubesat Space Protocol) v1 header parsing and construction.

Field layout matches gr-satellites' python/csp_header.py (Daniel Estevez,
GPL-3.0), which itself follows the libcsp CSP v1 32-bit header:

  bit 31-30  priority     (2 bits)
  bit 29-25  source       (5 bits)
  bit 24-20  destination  (5 bits)
  bit 19-14  dest_port    (6 bits)
  bit 13-8   source_port  (6 bits)
  bit 7-4    reserved     (4 bits)
  bit 3      hmac         (1 bit)
  bit 2      xtea         (1 bit)
  bit 1      rdp          (1 bit)
  bit 0      crc          (1 bit)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class CspHeader:
    priority: int
    source: int
    destination: int
    dest_port: int
    source_port: int
    reserved: int = 0
    hmac: bool = False
    xtea: bool = False
    rdp: bool = False
    crc: bool = False

    def pack(self) -> bytes:
        value = (
            (self.priority & 0x3) << 30
            | (self.source & 0x1F) << 25
            | (self.destination & 0x1F) << 20
            | (self.dest_port & 0x3F) << 14
            | (self.source_port & 0x3F) << 8
            | (self.reserved & 0xF) << 4
            | (1 if self.hmac else 0) << 3
            | (1 if self.xtea else 0) << 2
            | (1 if self.rdp else 0) << 1
            | (1 if self.crc else 0)
        )
        return struct.pack("!I", value)


def parse_csp_header(data: bytes) -> CspHeader:
    """Parse the first 4 bytes of `data` as a CSP v1 header."""
    if len(data) < 4:
        raise ValueError("CSP packet too short (need at least 4 header bytes)")
    (value,) = struct.unpack("!I", data[0:4])
    return CspHeader(
        priority=(value >> 30) & 0x3,
        source=(value >> 25) & 0x1F,
        destination=(value >> 20) & 0x1F,
        dest_port=(value >> 14) & 0x3F,
        source_port=(value >> 8) & 0x3F,
        reserved=(value >> 4) & 0xF,
        hmac=bool((value >> 3) & 1),
        xtea=bool((value >> 2) & 1),
        rdp=bool((value >> 1) & 1),
        crc=bool(value & 1),
    )


def build_csp_packet(header: CspHeader, payload: bytes) -> bytes:
    """Build a full CSP packet (4-byte header + payload)."""
    return header.pack() + payload


def split_csp_packet(data: bytes) -> tuple[CspHeader, bytes]:
    """Parse a full CSP packet, returning (header, payload)."""
    return parse_csp_header(data), data[4:]
