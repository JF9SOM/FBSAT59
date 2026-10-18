"""Persistent store of received SSDV packets, so one picture can be built from several passes.

A satellite sends a picture's packets over and over; one pass rarely delivers them all.
Every SSDV mode of the SSTV / SSDV tab (AX.25, CCSDS BPSK 9k6 "DSLWP", 2FSK 800bps) hands its
valid packets to this store. Packets are grouped per satellite, packet format and image id.
A packet joins the newest group with the same image id and picture size; if that group
already holds a *different* packet with the same number, the satellite has reused the image id
for a new picture, and a new group is started.

Only packets known to be good are stored: ``validate_packet()`` accepts a packet whose CRC-32
is right, or one that Reed-Solomon repair turns into such a packet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from comms.sstv import rs_ssdv
from comms.sstv.ssdv import SYNC_BYTE, TYPE_NORMAL, crc_ok

KIND_STANDARD = "std"  # 0x55 0x66/0x67 packets (AX.25 and 2FSK modes)
KIND_DSLWP = "dslwp"  # 218-byte CCSDS / DSLWP packets (ASRTU-1)
_MIN_RS_PACKET = 40


def validate_packet(packet: bytes) -> bytes | None:
    """The packet itself if its CRC-32 is right, a Reed-Solomon repaired copy if that makes
    the CRC-32 right, else None. Standard (0x55 sync) packets only."""
    if len(packet) < 2 or packet[0] != SYNC_BYTE:
        return None
    if crc_ok(packet):
        return packet
    if packet[1] != TYPE_NORMAL or not _MIN_RS_PACKET <= len(packet) <= 256:
        return None  # without FEC (0x67) a bad CRC cannot be repaired
    pad = rs_ssdv.N - (len(packet) - 1)
    fixed = rs_ssdv.decode(bytes(pad) + packet[1:])
    if fixed is None:
        return None
    candidate = bytes([SYNC_BYTE]) + fixed[pad:]
    return candidate if crc_ok(candidate) else None


def _header(kind: str, packet: bytes) -> tuple[int, int, int, int]:
    """(image id, packet id, width, height) of a packet."""
    if kind == KIND_DSLWP:
        return packet[0], (packet[1] << 8) | packet[2], packet[3], packet[4]
    return packet[6], (packet[7] << 8) | packet[8], packet[9], packet[10]


def _is_last(kind: str, packet: bytes) -> bool:
    """True if the packet carries the end-of-image flag (standard packets only)."""
    return kind == KIND_STANDARD and bool((packet[11] >> 2) & 1)


@dataclass(frozen=True)
class AddResult:
    """What ``SsdvPacketStore.add()`` did."""

    group_id: int
    is_new: bool  # the packet was not stored yet
    new_group: bool  # a new group was started for it


@dataclass(frozen=True)
class GroupInfo:
    """One stored picture (a group of packets)."""

    id: int
    norad: int
    kind: str
    image_id: int
    width: int  # in units of 16 pixels
    height: int
    packets: int
    total: int | None  # packets in the whole picture, if the last one has been received
    created_at: str
    updated_at: str


class SsdvPacketStore:
    """SQLite-backed packet store (tables ``ssdv_groups`` and ``ssdv_packets``)."""

    def __init__(self, conn: Any) -> None:
        self._conn: sqlite3.Connection = conn
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ssdv_groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                norad       INTEGER NOT NULL,
                kind        TEXT NOT NULL,
                image_id    INTEGER NOT NULL,
                width       INTEGER NOT NULL,
                height      INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ssdv_packets (
                group_id     INTEGER NOT NULL REFERENCES ssdv_groups(id) ON DELETE CASCADE,
                packet_id    INTEGER NOT NULL,
                data         BLOB NOT NULL,
                received_at  TEXT NOT NULL,
                PRIMARY KEY (group_id, packet_id)
            )
            """
        )
        self._conn.commit()

    def add(
        self,
        norad: int,
        kind: str,
        packet: bytes,
        when: datetime | None = None,
        commit: bool = True,
    ) -> AddResult:
        """Store a valid packet (see validate_packet() / find_dslwp_packet()).

        ``commit=False`` leaves the transaction open for a batch; call commit() afterwards.
        """
        stamp = (when or datetime.now(UTC)).isoformat()
        image_id, pid, width, height = _header(kind, packet)
        row = self._conn.execute(
            "SELECT id FROM ssdv_groups WHERE norad=? AND kind=? AND image_id=? "
            "AND width=? AND height=? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (norad, kind, image_id, width, height),
        ).fetchone()
        new_group = False
        if row is not None:
            gid = int(row[0])
            have = self._conn.execute(
                "SELECT data FROM ssdv_packets WHERE group_id=? AND packet_id=?", (gid, pid)
            ).fetchone()
            if have is not None:
                if bytes(have[0]) == packet:
                    return AddResult(gid, False, False)
                row = None  # same number, different packet: the image id was reused
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO ssdv_groups (norad, kind, image_id, width, height, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (norad, kind, image_id, width, height, stamp, stamp),
            )
            gid = int(cur.lastrowid or 0)
            new_group = True
        self._conn.execute(
            "INSERT INTO ssdv_packets (group_id, packet_id, data, received_at) VALUES (?,?,?,?)",
            (gid, pid, packet, stamp),
        )
        self._conn.execute("UPDATE ssdv_groups SET updated_at=? WHERE id=?", (stamp, gid))
        if commit:
            self._conn.commit()
        return AddResult(gid, True, new_group)

    def commit(self) -> None:
        """Commit a batch of add(..., commit=False)."""
        self._conn.commit()

    def packets(self, group_id: int) -> list[bytes]:
        """The group's packets, in packet-number order."""
        rows = self._conn.execute(
            "SELECT data FROM ssdv_packets WHERE group_id=? ORDER BY packet_id", (group_id,)
        ).fetchall()
        return [bytes(r[0]) for r in rows]

    def group(self, group_id: int) -> GroupInfo | None:
        """One group, or None."""
        for info in self.groups():
            if info.id == group_id:
                return info
        return None

    def groups(self) -> list[GroupInfo]:
        """Every group, newest first."""
        out: list[GroupInfo] = []
        rows = self._conn.execute(
            "SELECT id, norad, kind, image_id, width, height, created_at, updated_at "
            "FROM ssdv_groups ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        for r in rows:
            gid, kind = int(r[0]), str(r[2])
            count = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM ssdv_packets WHERE group_id=?", (gid,)
                ).fetchone()[0]
            )
            total: int | None = None
            if kind == KIND_STANDARD:
                for pid, data in self._conn.execute(
                    "SELECT packet_id, data FROM ssdv_packets WHERE group_id=?", (gid,)
                ):
                    if _is_last(kind, bytes(data)):
                        total = int(pid) + 1
                        break
            out.append(
                GroupInfo(
                    gid,
                    int(r[1]),
                    kind,
                    int(r[3]),
                    int(r[4]),
                    int(r[5]),
                    count,
                    total,
                    str(r[6]),
                    str(r[7]),
                )
            )
        return out

    def delete(self, group_id: int) -> None:
        """Remove a group and its packets."""
        self._conn.execute("DELETE FROM ssdv_packets WHERE group_id=?", (group_id,))
        self._conn.execute("DELETE FROM ssdv_groups WHERE id=?", (group_id,))
        self._conn.commit()
