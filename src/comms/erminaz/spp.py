"""ERMINAZ-2 downlink: space packets spread over several TM transfer frames.

A 223 octet TM frame has 213 octets of data field (header 6, OCF 4), so an SSDV packet (APID 500:
space packet header 6 + 256 octets) needs two frames. The first frame's first header pointer is
0; the next holds the rest, its pointer being the number of continuation octets (the remainder of
the frame is idle ``0xE0``), or ``0x7FF`` when the whole data field is continuation. With packet
stuffing the next packet may start right after the continuation. Read from the OSDLP library
(``osdlp_tm.c``) and the firmware (``telemetry.c``, ``ssdv.c``), not yet checked on real frames.
"""

from __future__ import annotations

from dataclasses import dataclass

APID_SSDV = 500
SSDV_PACKET_SIZE = 256
_FHP_NO_PACKET_START = 0x7FF
_FHP_OID = 0x7FE
_IDLE = 0xE0
_SP_HEADER = 6
_TM_HEADER = 6
_OCF_SIZE = 4


@dataclass(frozen=True)
class SpacePacket:
    """One complete space packet."""

    apid: int
    sequence: int
    data: bytes  # the packet data field (after the 6 octet header)
    vcid: int


def _packet_size(buf: bytes | bytearray) -> int | None:
    """Total octets of the space packet starting *buf* (None until its header is complete)."""
    if len(buf) < _SP_HEADER:
        return None
    return _SP_HEADER + int.from_bytes(buf[4:6], "big") + 1


def _packet(raw: bytes, vcid: int) -> SpacePacket:
    return SpacePacket(
        apid=int.from_bytes(raw[0:2], "big") & 0x7FF,
        sequence=int.from_bytes(raw[2:4], "big") & 0x3FFF,
        data=bytes(raw[_SP_HEADER:]),
        vcid=vcid,
    )


class TmReassembler:
    """Collects the space packets of a stream of TM transfer frames (any number of VCIDs)."""

    def __init__(self) -> None:
        self._open: dict[int, bytearray] = {}

    def reset(self) -> None:
        """Forget packets that are half received (the signal was lost)."""
        self._open.clear()

    def feed(self, frame: bytes) -> list[SpacePacket]:
        """Add one TM frame (as the RS decoder delivers it); return the packets it completes."""
        if len(frame) < _TM_HEADER + _OCF_SIZE + 1:
            return []
        vcid = (frame[1] >> 1) & 0x7
        end = len(frame) - (_OCF_SIZE if frame[1] & 0x1 else 0)
        data = frame[_TM_HEADER:end]
        fhp = ((frame[4] & 0x07) << 8) | frame[5]
        out: list[SpacePacket] = []
        pending = self._open.get(vcid)

        if fhp == _FHP_OID:
            self._open.pop(vcid, None)
            return out
        if pending is not None:
            if fhp == 0:
                # A new packet starts at once: the one in progress lost its tail.
                self._open.pop(vcid, None)
            else:
                take = data if fhp == _FHP_NO_PACKET_START else data[:fhp]
                pending.extend(take)
                size = _packet_size(pending)
                if size is not None and len(pending) >= size:
                    out.append(_packet(bytes(pending[:size]), vcid))
                    del self._open[vcid]
                elif fhp != _FHP_NO_PACKET_START:
                    # The continuation ended but the packet is still short: it is lost.
                    del self._open[vcid]
        if fhp == _FHP_NO_PACKET_START:
            return out

        pos = fhp
        while pos < len(data) and data[pos] != _IDLE:
            rest = data[pos:]
            size = _packet_size(rest)
            if size is None or len(rest) < size:
                self._open[vcid] = bytearray(rest)
                break
            out.append(_packet(bytes(rest[:size]), vcid))
            pos += size
        return out


def ssdv_packets(packets: list[SpacePacket]) -> list[bytes]:
    """The 256 octet SSDV packets (APID 500) among *packets*."""
    return [p.data for p in packets if p.apid == APID_SSDV and len(p.data) == SSDV_PACKET_SIZE]
