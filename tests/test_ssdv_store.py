"""Tests for the SSDV packet store and Reed-Solomon repair (comms.sstv.packet_store, rs_ssdv)."""

from __future__ import annotations

import random
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta

import pytest

from comms.sstv import rs_ssdv
from comms.sstv.packet_store import (
    KIND_DSLWP,
    KIND_STANDARD,
    SsdvPacketStore,
    validate_packet,
)
from tests.test_ssdv import make_dslwp_packet, make_packet


def real_packet(*, image: int = 5, pid: int = 0, eoi: bool = False) -> bytes:
    """A standard 256-byte packet with a valid CRC-32 and real Reed-Solomon parity."""
    pkt = bytearray(make_packet(image=image, pid=pid))
    pkt[11] = 0x18 | (0x04 if eoi else 0)  # quality 7, end-of-image flag
    pkt[220:224] = zlib.crc32(bytes(pkt[1:220])).to_bytes(4, "big")
    pkt[224:256] = rs_ssdv.encode(bytes(pkt[1:224]))
    return bytes(pkt)


def test_rs_decodes_errors_and_erasures() -> None:
    rng = random.Random(1)
    cw = bytes(rng.randrange(256) for _ in range(223))
    word = cw + rs_ssdv.encode(cw)
    assert not any(rs_ssdv.syndromes(word))
    for erasures, errors in ((0, 16), (32, 0), (10, 11), (0, 1)):
        pos = rng.sample(range(255), erasures + errors)
        bad = bytearray(word)
        for p in pos:
            bad[p] ^= rng.randrange(1, 256)
        assert rs_ssdv.decode(bytes(bad), pos[:erasures]) == word
    bad = bytearray(word)
    for p in rng.sample(range(255), 17):
        bad[p] ^= 0x5A
    assert rs_ssdv.decode(bytes(bad)) is None


def test_validate_accepts_good_and_repairs_damaged_packets() -> None:
    pkt = real_packet(pid=3)
    assert validate_packet(pkt) == pkt
    damaged = bytearray(pkt)
    for i in (20, 60, 99, 130, 180, 230):  # payload and parity bytes
        damaged[i] ^= 0xA5
    assert validate_packet(bytes(damaged)) == pkt
    wrecked = bytes(pkt[:20]) + bytes(b ^ 0xFF for b in pkt[20:120]) + bytes(pkt[120:])
    assert validate_packet(wrecked) is None
    assert validate_packet(b"\x00" * 256) is None


def test_validate_does_not_repair_packets_without_fec() -> None:
    nofec = make_packet(nofec=True)
    assert validate_packet(nofec) == nofec
    damaged = bytearray(nofec)
    damaged[40] ^= 1
    assert validate_packet(bytes(damaged)) is None


@pytest.fixture
def store() -> SsdvPacketStore:
    return SsdvPacketStore(sqlite3.connect(":memory:"))


def test_packets_of_several_passes_join_one_picture(store: SsdvPacketStore) -> None:
    first = [real_packet(image=48, pid=n) for n in (0, 1, 2)]
    second = [real_packet(image=48, pid=n) for n in (2, 3, 4)]  # pass 2 overlaps and adds
    groups = set()
    for pkt in first + second:
        result = store.add(68446, KIND_STANDARD, pkt)
        groups.add(result.group_id)
    assert len(groups) == 1
    infos = store.groups()
    assert len(infos) == 1 and infos[0].packets == 5
    assert [p[8] for p in store.packets(infos[0].id)] == [0, 1, 2, 3, 4]


def test_a_repeated_packet_is_not_new(store: SsdvPacketStore) -> None:
    pkt = real_packet(pid=1)
    assert store.add(1, KIND_STANDARD, pkt).is_new
    again = store.add(1, KIND_STANDARD, pkt)
    assert not again.is_new and not again.new_group


def test_a_reused_image_id_starts_a_new_picture(store: SsdvPacketStore) -> None:
    old = real_packet(image=7, pid=0)
    other = bytearray(old)
    other[30] ^= 0x01  # a different packet with the same image id and number
    other[220:224] = zlib.crc32(bytes(other[1:220])).to_bytes(4, "big")
    a = store.add(1, KIND_STANDARD, old)
    b = store.add(1, KIND_STANDARD, bytes(other))
    assert b.new_group and b.group_id != a.group_id
    # later packets go to the newest picture
    c = store.add(1, KIND_STANDARD, real_packet(image=7, pid=1))
    assert c.group_id == b.group_id


def test_different_satellites_and_sizes_are_kept_apart(store: SsdvPacketStore) -> None:
    pkt = real_packet(image=1, pid=0)
    assert store.add(1, KIND_STANDARD, pkt).group_id != store.add(2, KIND_STANDARD, pkt).group_id
    wide = bytearray(real_packet(image=1, pid=1))
    wide[9] = 40  # width differs
    wide[220:224] = zlib.crc32(bytes(wide[1:220])).to_bytes(4, "big")
    assert store.add(1, KIND_STANDARD, bytes(wide)).new_group


def test_total_is_known_once_the_last_packet_arrives(store: SsdvPacketStore) -> None:
    for n in range(3):
        store.add(9, KIND_STANDARD, real_packet(image=2, pid=n))
    assert store.groups()[0].total is None
    store.add(9, KIND_STANDARD, real_packet(image=2, pid=3, eoi=True))
    assert store.groups()[0].total == 4


def test_dslwp_packets_are_stored_in_their_own_kind(store: SsdvPacketStore) -> None:
    store.add(61781, KIND_DSLWP, make_dslwp_packet(pid=0))
    store.add(61781, KIND_DSLWP, make_dslwp_packet(pid=1))
    info = store.groups()[0]
    assert info.kind == KIND_DSLWP and info.packets == 2 and info.total is None


def test_groups_are_newest_first_and_can_be_deleted(store: SsdvPacketStore) -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    store.add(1, KIND_STANDARD, real_packet(image=1), t0)
    store.add(1, KIND_STANDARD, real_packet(image=2), t0 + timedelta(days=1))
    infos = store.groups()
    assert [i.image_id for i in infos] == [2, 1]
    store.delete(infos[0].id)
    assert [i.image_id for i in store.groups()] == [1]
    assert store.packets(infos[0].id) == []


def test_store_survives_reopening_the_database() -> None:
    conn = sqlite3.connect(":memory:")
    SsdvPacketStore(conn).add(1, KIND_STANDARD, real_packet(image=3, pid=0))
    assert SsdvPacketStore(conn).groups()[0].packets == 1
