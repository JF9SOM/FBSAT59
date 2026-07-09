"""Lightweight SNTP client for verifying system clock accuracy at startup.

Performs its own SNTP round trip rather than reading the OS's NTP daemon
status, so it works identically on Linux/Windows/macOS regardless of which
(if any) time-sync service is configured on the host.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

_NTP_SERVERS: tuple[str, ...] = (
    "ntp.ubuntu.com",
    "pool.ntp.org",
    "time.google.com",
)
_NTP_PORT = 123
_NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900-01-01 (NTP epoch) and 1970-01-01
_QUERY_TIMEOUT_S = 4.0


@dataclass
class NtpCheckResult:
    """Result of a single-shot SNTP time check."""

    reachable: bool
    # Seconds to add to the local clock to get the true time (positive = local
    # clock is behind, negative = local clock is ahead). None if unreachable.
    offset_s: float | None
    server: str | None
    error: str | None


def _query_sntp(server: str, timeout: float = _QUERY_TIMEOUT_S) -> float:
    """Query one SNTP server and return the clock offset in seconds.

    Raises OSError / socket.timeout / struct.error on failure.
    """
    packet = bytearray(48)
    packet[0] = 0b00_011_011  # LI=0 (no warning), VN=3, Mode=3 (client)
    t1 = time.time()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, _NTP_PORT))
        data, _addr = sock.recvfrom(48)
    t4 = time.time()

    # Receive Timestamp (T2) at bytes 32-39, Transmit Timestamp (T3) at bytes 40-47;
    # each is a 4-byte seconds field followed by a 4-byte fraction field.
    recv_int, recv_frac = struct.unpack("!II", data[32:40])
    xmit_int, xmit_frac = struct.unpack("!II", data[40:48])
    t2 = (recv_int - _NTP_EPOCH_OFFSET) + recv_frac / 2**32
    t3 = (xmit_int - _NTP_EPOCH_OFFSET) + xmit_frac / 2**32

    # Standard SNTP clock offset formula (RFC 4330): positive means the local
    # clock is behind and should be advanced by this many seconds.
    return ((t2 - t1) + (t3 - t4)) / 2.0


def check_system_clock(servers: tuple[str, ...] = _NTP_SERVERS) -> NtpCheckResult:
    """Query NTP servers in turn and return the first successful result.

    Tries each server in `servers` until one responds; if none respond,
    returns a result with reachable=False and the last error encountered.
    """
    last_error: str | None = None
    for server in servers:
        try:
            offset = _query_sntp(server)
            return NtpCheckResult(reachable=True, offset_s=offset, server=server, error=None)
        except Exception as exc:  # noqa: BLE001 - socket/struct errors all mean "unreachable" here
            last_error = f"{type(exc).__name__}: {exc}"
            continue
    return NtpCheckResult(reachable=False, offset_s=None, server=None, error=last_error)
