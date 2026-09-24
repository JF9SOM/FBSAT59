"""Tests for comms/sstv/ssdv.py: hex-frame parsing, SSDV packet lookup, the decoder."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot

import comms.sstv.ssdv as ssdv_mod
from comms.sstv.ssdv import (
    SsdvDecoder,
    extract_hex_frames,
    find_ssdv,
    find_ssdv_packet,
    format_hex_line,
    packet_id,
    packet_image_id,
)

AX25_HEADER = bytes.fromhex("94a662b29caa6094a662b2a4aae103f0")  # JS1YNU>JS1YRU, UI, PID F0


def make_packet(
    *, size: int = 256, nofec: bool = False, image: int = 5, pid: int = 0, good_crc: bool = True
) -> bytes:
    """A syntactically valid SSDV packet (random-ish payload, correct CRC-32)."""
    fec = 0 if nofec else 32
    header = bytes([0x55, 0x67 if nofec else 0x66]) + b"\x8a\x6e\x21\x08" + bytes([image])
    header += pid.to_bytes(2, "big") + bytes([20, 14, 0x05, 0, 0, 0])[:6]
    assert len(header) == 15
    payload = bytes((pid * 7 + i * 13) & 0xFF for i in range(size - 15 - 4 - fec))
    body = header + payload
    crc = zlib.crc32(body[1:])
    if not good_crc:
        crc ^= 0xFFFF
    return body + crc.to_bytes(4, "big") + bytes(fec)


# --------------------------------------------------------------------------
# extract_hex_frames
# --------------------------------------------------------------------------


def test_extract_hex_frames_accepts_the_usual_notations() -> None:
    text = "94 A6 62\n94a662\n0x94, 0xA6, 0x62\n94:A6:62\n94-a6-62\n"
    assert extract_hex_frames(text) == [bytes.fromhex("94a662")] * 5


def test_extract_hex_frames_ignores_satnogs_titles_and_other_text() -> None:
    text = (
        "Observation #15046484\n"
        "data_obs/2026/9/23/6/15046484/data_15046484_2026-09-23T06-47-28\n"
        "94 A6 62 B2 9C AA 60 94 A6 62 B2 A4 AA E1 03 F0 72 FF\n"
        "\n"
        "data_obs/2026/9/23/6/15046484/data_15046484_2026-09-23T06-47-29\n"
        "94 A6 62 B2 9C AA 60 94 A6 62 B2 A4 AA E1 03 F0 22 FE\n"
        "Load More Data(10)\n"
    )
    frames = extract_hex_frames(text)
    assert [f[-2:] for f in frames] == [b"\x72\xff", b"\x22\xfe"]


def test_extract_hex_frames_takes_the_last_cell_of_a_copied_table_row() -> None:
    row = "2026-09-22 06:52:45\tJS1YRU\tOrigamiSat-2\t94 A6 62 B2"
    assert extract_hex_frames(row) == [bytes.fromhex("94a662b2")]


def test_extract_hex_frames_rejects_odd_or_mixed_lines() -> None:
    assert extract_hex_frames("94 A6 6\nABC\nhello 94 A6\n94 A6 zz\n") == []
    assert extract_hex_frames("") == []


def test_format_hex_line_round_trips_through_extract() -> None:
    frame = bytes(range(0, 256, 5))
    assert extract_hex_frames(format_hex_line(frame)) == [frame]


# --------------------------------------------------------------------------
# find_ssdv_packet
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "nofec"),
    [(256, False), (256, True), (100, False), (64, True), (53, False), (21, True)],
)
def test_finds_a_packet_with_or_without_an_ax25_header(size: int, nofec: bool) -> None:
    pkt = make_packet(size=size, nofec=nofec)
    assert find_ssdv_packet(pkt) == pkt
    assert find_ssdv_packet(AX25_HEADER + pkt) == pkt
    assert find_ssdv_packet(b"\x00" + pkt) == pkt


def test_packet_fields() -> None:
    pkt = make_packet(image=9, pid=0x0123)
    assert packet_image_id(pkt) == 9
    assert packet_id(pkt) == 0x0123


def test_a_valid_later_candidate_beats_an_earlier_false_sync() -> None:
    pkt = make_packet(size=100)
    # a false sync (55 67 ...) ahead of the real packet: its candidate spans the real
    # packet, fails the CRC, and the scan goes on to the real one
    assert find_ssdv_packet(b"\x55\x67" + b"\x11" * 3 + b"\x00" + pkt) == pkt


def test_a_corrupted_full_length_packet_is_returned_for_reed_solomon_repair() -> None:
    pkt = make_packet(good_crc=False)
    assert find_ssdv_packet(pkt) == pkt


def test_a_corrupted_short_or_nofec_packet_is_not_returned() -> None:
    assert find_ssdv_packet(make_packet(size=100, good_crc=False)) is None
    assert find_ssdv_packet(make_packet(nofec=True, good_crc=False)) is None


def test_frames_without_a_packet_give_none() -> None:
    origami = bytes.fromhex(
        "94a662b29caa6094a662b2a4aae103f022fe646a6a3a8fc60000080100550000073e" + "ad5032bcb7eec0"
    )
    assert find_ssdv_packet(origami) is None
    assert find_ssdv_packet(b"") is None
    assert find_ssdv_packet(b"\x55") is None
    assert find_ssdv_packet(b"\x55\x66") is None  # sync only, too short to be a packet


# --------------------------------------------------------------------------
# SsdvDecoder
# --------------------------------------------------------------------------


def _png(path: str, width: int = 64, height: int = 48) -> None:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#3070c0"))
    image.save(path, "PNG")


class _FakeSsdv:
    """Stands in for subprocess.run(): records the call, writes a picture."""

    def __init__(self, returncode: int = 0, write: bool = True) -> None:
        self.calls: list[tuple[list[str], bytes]] = []
        self.returncode = returncode
        self.write = write

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # ssdv is given files (binary-safe on Windows): -d -l N <packets file> <image file>
        self.calls.append((list(argv), Path(argv[-2]).read_bytes()))
        if self.write:
            _png(argv[-1])
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"")


@pytest.fixture
def fake_ssdv(monkeypatch: pytest.MonkeyPatch) -> _FakeSsdv:
    fake = _FakeSsdv()
    monkeypatch.setattr(ssdv_mod, "find_ssdv", lambda: "/fake/ssdv")
    monkeypatch.setattr(ssdv_mod.subprocess, "run", fake)
    return fake


def test_push_packet_accepts_only_ssdv_packets(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    assert dec.push_packet(make_packet())
    assert dec.push_packet(make_packet(size=64, nofec=True, pid=1))
    assert not dec.push_packet(b"")
    assert not dec.push_packet(bytes(300))
    assert not dec.push_packet(b"\x54\x66" + bytes(254))  # no sync
    assert not dec.push_packet(b"\x55\x70" + bytes(254))  # unknown type
    assert not dec.push_packet(b"\x55\x66" + bytes(10))  # too short
    assert dec.packet_count == 2


def test_duplicates_are_dropped_and_the_latest_copy_wins(
    qtbot: QtBot, fake_ssdv: _FakeSsdv
) -> None:
    dec = SsdvDecoder()
    first = make_packet(pid=3)
    repaired = bytearray(first)
    repaired[20] ^= 0xFF
    dec.push_packet(first)
    dec.push_packet(bytes(repaired))
    assert dec.packet_count == 1
    assert dec.decode_now()
    assert fake_ssdv.calls[0][1] == bytes(repaired)


def test_decode_orders_by_packet_id_and_passes_the_length(
    qtbot: QtBot, fake_ssdv: _FakeSsdv
) -> None:
    dec = SsdvDecoder()
    pkts = [make_packet(size=100, pid=p) for p in (2, 0, 1)]
    for p in pkts:
        dec.push_packet(p)
    assert dec.decode_now()
    argv, data = fake_ssdv.calls[0]
    assert argv[:4] == ["/fake/ssdv", "-d", "-l", "100"]
    assert data == pkts[1] + pkts[2] + pkts[0]


def test_decode_picks_the_image_with_the_most_packets(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    for p in range(2):
        dec.push_packet(make_packet(image=1, pid=p))
    for p in range(5):
        dec.push_packet(make_packet(image=2, pid=p))
    statuses: list[str] = []
    dec.status_changed.connect(statuses.append)
    assert dec.decode_now()
    assert len(fake_ssdv.calls[0][1]) == 5 * 256
    assert "image 2" in statuses[-1]


def test_decode_uses_the_most_common_packet_length(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    for p in range(3):
        dec.push_packet(make_packet(size=100, pid=p))
    dec.push_packet(make_packet(size=120, pid=3))
    assert dec.decode_now()
    argv, data = fake_ssdv.calls[0]
    assert argv[3] == "100"
    assert len(data) == 300


def test_decode_emits_the_image(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    with qtbot.waitSignal(dec.image_updated, timeout=3000) as blocker:
        dec.decode_now()
    image = blocker.args[0]
    assert (image.width(), image.height()) == (64, 48)


def test_push_schedules_a_live_decode(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    with qtbot.waitSignal(dec.image_updated, timeout=3000):
        dec.push_packet(make_packet())
    assert len(fake_ssdv.calls) == 1


def test_reset_forgets_packets_and_cancels_the_decode(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    dec.reset()
    assert dec.packet_count == 0
    assert not dec.decode_now()
    qtbot.wait(900)
    assert fake_ssdv.calls == []


def test_flush_decodes_once_and_clears(qtbot: QtBot, fake_ssdv: _FakeSsdv) -> None:
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    dec.flush()
    assert len(fake_ssdv.calls) == 1
    assert dec.packet_count == 0


def test_missing_binary_reports_an_error(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssdv_mod, "find_ssdv", lambda: None)
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    errors: list[str] = []
    dec.error_occurred.connect(errors.append)
    assert not dec.decode_now()
    assert errors and "ssdv binary not found" in errors[0]


@pytest.mark.parametrize("returncode", [1, -1])
def test_a_failing_binary_reports_an_error(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    monkeypatch.setattr(ssdv_mod, "find_ssdv", lambda: "/fake/ssdv")
    monkeypatch.setattr(ssdv_mod.subprocess, "run", _FakeSsdv(returncode=returncode))
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    errors: list[str] = []
    dec.error_occurred.connect(errors.append)
    assert not dec.decode_now()
    assert errors


def test_no_image_written_reports_an_error(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssdv_mod, "find_ssdv", lambda: "/fake/ssdv")
    monkeypatch.setattr(ssdv_mod.subprocess, "run", _FakeSsdv(write=False))
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    errors: list[str] = []
    dec.error_occurred.connect(errors.append)
    assert not dec.decode_now()
    assert errors


# --------------------------------------------------------------------------
# With the real ssdv binary (skipped where it is not installed)
# --------------------------------------------------------------------------


@pytest.mark.skipif(find_ssdv() is None, reason="ssdv binary not installed")
@pytest.mark.parametrize("extra", [[], ["-n"], ["-l", "100"], ["-n", "-l", "64"]])
def test_real_ssdv_round_trip_through_hex_text(
    qtbot: QtBot, tmp_path: Path, extra: list[str]
) -> None:
    pil = pytest.importorskip("PIL.Image")
    ssdv = shutil.which("ssdv") or find_ssdv()
    assert ssdv is not None
    jpg = tmp_path / "in.jpg"
    image = pil.new("RGB", (160, 112), (40, 80, 160))
    image.paste((230, 200, 40), (30, 20, 120, 90))
    image.save(jpg, quality=85, subsampling=2)
    encoded = subprocess.run(
        [ssdv, "-e", "-c", "JF9SOM", "-i", "5", *extra, str(jpg), "-"], capture_output=True
    ).stdout
    length = int(extra[extra.index("-l") + 1]) if "-l" in extra else 256
    packets = [encoded[i : i + length] for i in range(0, len(encoded), length)]
    text = "\n".join(format_hex_line(AX25_HEADER + p) for p in packets)

    dec = SsdvDecoder()
    for frame in extract_hex_frames(text):
        packet = find_ssdv_packet(frame)
        assert packet is not None
        dec.push_packet(packet)
    with qtbot.waitSignal(dec.image_updated, timeout=5000) as blocker:
        dec.decode_now()
    assert (blocker.args[0].width(), blocker.args[0].height()) == (160, 112)


def test_decode_leaves_no_temporary_files_behind(
    qtbot: QtBot, fake_ssdv: _FakeSsdv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    dec = SsdvDecoder()
    dec.push_packet(make_packet())
    assert dec.decode_now()
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# find_ssdv: user copy, PATH, then the copy bundled with the app
# --------------------------------------------------------------------------


def _exe(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("ssdv.exe" if sys.platform == "win32" else "ssdv")
    path.write_bytes(b"#!/bin/sh\n")
    return path


def test_find_ssdv_prefers_the_user_copy_then_path_then_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir, on_path, bundle = tmp_path / "user", tmp_path / "path", tmp_path / "bundle"
    monkeypatch.setattr(ssdv_mod, "_user_ssdv_dirs", lambda: [user_dir])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(ssdv_mod.shutil, "which", lambda _name: None)

    assert find_ssdv() is None
    bundled = _exe(bundle)
    assert find_ssdv() == str(bundled)
    monkeypatch.setattr(ssdv_mod.shutil, "which", lambda _name: str(_exe(on_path)))
    assert find_ssdv() == str(on_path / bundled.name)
    user = _exe(user_dir)
    assert find_ssdv() == str(user)


def test_bundled_ssdv_is_ignored_when_not_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exe(tmp_path)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert ssdv_mod._bundled_ssdv() is None
