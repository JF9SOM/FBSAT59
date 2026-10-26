"""Space packets spread over ERMINAZ-2 TM frames (the SSDV downlink)."""

from __future__ import annotations

from comms.erminaz.spp import APID_SSDV, TmReassembler, ssdv_packets

_DATA = 213  # 223 - 6 (header) - 4 (OCF)


def _space_packet(apid: int, payload: bytes, seq: int = 0) -> bytes:
    return (
        apid.to_bytes(2, "big")
        + (0xC000 | seq).to_bytes(2, "big")
        + (len(payload) - 1).to_bytes(2, "big")
        + payload
    )


def _frame(data: bytes, fhp: int, vcid: int = 1, count: int = 0) -> bytes:
    """A TM frame whose data field is *data* padded with idle octets, with an OCF."""
    head = bytes([102 >> 4, ((102 & 0xF) << 4) | (vcid << 1) | 1, count, count])
    head += (0x1800 | fhp).to_bytes(2, "big")
    return head + data.ljust(_DATA, b"\xe0") + bytes(4)


def _split(packet: bytes) -> list[bytes]:
    """The frames the satellite sends for one packet (no stuffing)."""
    frames = [_frame(packet[:_DATA], 0)]
    rest = packet[_DATA:]
    while rest:
        fhp = 0x7FF if len(rest) >= _DATA else len(rest)
        frames.append(_frame(rest[:_DATA], fhp))
        rest = rest[_DATA:]
    return frames


def _ssdv(index: int) -> bytes:
    return bytes([0x55, 0x67]) + bytes([index]) * 254


def test_ssdv_packet_spans_two_frames() -> None:
    packet = _ssdv(7)
    frames = _split(_space_packet(APID_SSDV, packet))
    assert len(frames) == 2
    assembler = TmReassembler()
    assert assembler.feed(frames[0]) == []
    done = assembler.feed(frames[1])
    assert [p.apid for p in done] == [APID_SSDV]
    assert ssdv_packets(done) == [packet]


def test_packets_that_fit_one_frame_and_back_to_back_ones() -> None:
    a = _space_packet(600, b"x" * 40, seq=1)
    b = _space_packet(300, b"y" * 60, seq=2)
    assembler = TmReassembler()
    out = assembler.feed(_frame(a + b, 0))
    assert [(p.apid, p.sequence, len(p.data)) for p in out] == [(600, 1, 40), (300, 2, 60)]


def test_stuffed_frame_continues_one_packet_and_starts_the_next() -> None:
    first = _space_packet(APID_SSDV, _ssdv(1))
    second = _space_packet(APID_SSDV, _ssdv(2))
    tail = first[_DATA:]
    assembler = TmReassembler()
    assembler.feed(_frame(first[:_DATA], 0))
    done = assembler.feed(_frame(tail + second[: _DATA - len(tail)], len(tail)))
    assert ssdv_packets(done) == [_ssdv(1)]
    rest = second[_DATA - len(tail) :]
    assert ssdv_packets(assembler.feed(_frame(rest, len(rest)))) == [_ssdv(2)]


def test_a_lost_frame_drops_the_packet_without_corrupting_the_next() -> None:
    frames = _split(_space_packet(APID_SSDV, _ssdv(1)))
    nxt = _split(_space_packet(APID_SSDV, _ssdv(2)))
    assembler = TmReassembler()
    assembler.feed(frames[0])  # the continuation frame is lost
    assembler.feed(nxt[0])  # a new packet starts: the half packet is dropped
    assert ssdv_packets(assembler.feed(nxt[1])) == [_ssdv(2)]


def test_continuation_without_a_start_is_ignored() -> None:
    frames = _split(_space_packet(APID_SSDV, _ssdv(3)))
    assert TmReassembler().feed(frames[1]) == []


def test_vcids_are_kept_apart() -> None:
    p1 = _split(_space_packet(APID_SSDV, _ssdv(1)))
    other = _frame(_space_packet(300, b"z" * 20), 0, vcid=0)
    assembler = TmReassembler()
    assembler.feed(p1[0])
    assert [p.apid for p in assembler.feed(other)] == [300]
    assert ssdv_packets(assembler.feed(p1[1])) == [_ssdv(1)]
