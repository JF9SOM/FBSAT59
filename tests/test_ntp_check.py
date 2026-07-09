"""Tests for core.ntp_check — pure logic, no real network access."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

from core.ntp_check import _NTP_EPOCH_OFFSET, check_system_clock


def _build_ntp_response(recv_time: float, xmit_time: float) -> bytes:
    """Build a 48-byte SNTP response packet with the given server timestamps."""
    packet = bytearray(48)
    recv_int = int(recv_time) + _NTP_EPOCH_OFFSET
    recv_frac = int((recv_time % 1) * 2**32)
    xmit_int = int(xmit_time) + _NTP_EPOCH_OFFSET
    xmit_frac = int((xmit_time % 1) * 2**32)
    packet[32:40] = struct.pack("!II", recv_int, recv_frac)
    packet[40:48] = struct.pack("!II", xmit_int, xmit_frac)
    return bytes(packet)


def test_check_system_clock_reports_zero_offset_when_synced() -> None:
    """A server response with server time == local time yields ~0 offset."""
    now = 1_800_000_000.0
    response = _build_ntp_response(recv_time=now, xmit_time=now)

    fake_sock = MagicMock()
    fake_sock.recvfrom.return_value = (response, ("1.2.3.4", 123))
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.__exit__.return_value = False

    with (
        patch("core.ntp_check.socket.socket", return_value=fake_sock),
        patch("core.ntp_check.time.time", side_effect=[now, now]),
    ):
        result = check_system_clock(servers=("fake.example",))

    assert result.reachable is True
    assert result.server == "fake.example"
    assert result.offset_s is not None
    assert abs(result.offset_s) < 1e-6


def test_check_system_clock_detects_large_drift() -> None:
    """A server clearly ahead of the local clock produces a large positive offset."""
    local_now = 1_800_000_000.0
    server_now = local_now + 5.0  # server is 5s ahead => local clock is 5s behind

    response = _build_ntp_response(recv_time=server_now, xmit_time=server_now)

    fake_sock = MagicMock()
    fake_sock.recvfrom.return_value = (response, ("1.2.3.4", 123))
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.__exit__.return_value = False

    with (
        patch("core.ntp_check.socket.socket", return_value=fake_sock),
        patch("core.ntp_check.time.time", side_effect=[local_now, local_now]),
    ):
        result = check_system_clock(servers=("fake.example",))

    assert result.reachable is True
    assert result.offset_s is not None
    assert result.offset_s == 5.0


def test_check_system_clock_all_servers_unreachable() -> None:
    """If every server raises, the result reports reachable=False with an error."""
    with patch("core.ntp_check.socket.socket", side_effect=OSError("network unreachable")):
        result = check_system_clock(servers=("a.example", "b.example"))

    assert result.reachable is False
    assert result.offset_s is None
    assert result.server is None
    assert result.error is not None
    assert "network unreachable" in result.error


def test_check_system_clock_falls_back_to_second_server() -> None:
    """If the first server times out, the second server is tried and used."""
    now = 1_800_000_000.0
    response = _build_ntp_response(recv_time=now, xmit_time=now)

    good_sock = MagicMock()
    good_sock.recvfrom.return_value = (response, ("1.2.3.4", 123))
    good_sock.__enter__.return_value = good_sock
    good_sock.__exit__.return_value = False

    bad_sock = MagicMock()
    bad_sock.__enter__.return_value = bad_sock
    bad_sock.__exit__.return_value = False
    bad_sock.sendto.side_effect = TimeoutError("timed out")

    with (
        patch("core.ntp_check.socket.socket", side_effect=[bad_sock, good_sock]),
        patch("core.ntp_check.time.time", side_effect=[now, now, now]),
    ):
        result = check_system_clock(servers=("bad.example", "good.example"))

    assert result.reachable is True
    assert result.server == "good.example"
