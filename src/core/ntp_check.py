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
# A single SNTP exchange is only as accurate as the network path is symmetric:
# observed single-shot results on one machine ranged 0.10-0.22 s from run to
# run while the real clock error (agreed by three servers) was 0.035 s. So
# several samples are taken per server and only the lowest round-trip one is
# kept (the standard NTP clock-filter idea), then the median across servers.
_SAMPLES_PER_SERVER = 4
_MAX_SERVERS = 3
# Offsets below this are indistinguishable from measurement noise on a
# consumer network path, and FT4 tolerates this much timing error anyway, so
# they are reported as exactly 0 instead of being "corrected" into the signal.
_NOISE_FLOOR_S = 0.1


@dataclass
class NtpCheckResult:
    """Result of a single-shot SNTP time check."""

    reachable: bool
    # Seconds to add to the local clock to get the true time (positive = local
    # clock is behind, negative = local clock is ahead). None if unreachable.
    offset_s: float | None
    server: str | None
    error: str | None


def _query_sntp(server: str, timeout: float = _QUERY_TIMEOUT_S) -> tuple[float, float]:
    """Query one SNTP server and return (clock offset, round-trip delay) in seconds.

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
    # struct.unpack()'s stub returns tuple[Any, ...], so without these casts
    # t2/t3 (and thus this function's return value) infer as Any instead of
    # float under mypy strict.
    t2 = (int(recv_int) - _NTP_EPOCH_OFFSET) + int(recv_frac) / 2**32
    t3 = (int(xmit_int) - _NTP_EPOCH_OFFSET) + int(xmit_frac) / 2**32

    # Standard SNTP clock offset formula (RFC 4330): positive means the local
    # clock is behind and should be advanced by this many seconds.
    offset = ((t2 - t1) + (t3 - t4)) / 2.0
    delay = max(0.0, (t4 - t1) - (t3 - t2))
    return offset, delay


def check_system_clock(servers: tuple[str, ...] = _NTP_SERVERS) -> NtpCheckResult:
    """Estimate the local clock offset from several NTP servers.

    Per server, the lowest-delay of several samples is kept; the result is the
    median over up to _MAX_SERVERS responding servers, snapped to 0.0 when
    smaller than _NOISE_FLOOR_S. A server that fails its first sample is
    skipped immediately (no repeated timeouts). If none respond, returns
    reachable=False with the last error encountered.
    """
    last_error: str | None = None
    # (offset, delay, server) -- best sample of each responding server
    best: list[tuple[float, float, str]] = []
    for server in servers:
        if len(best) >= _MAX_SERVERS:
            break
        samples: list[tuple[float, float]] = []
        for _ in range(_SAMPLES_PER_SERVER):
            try:
                samples.append(_query_sntp(server))
            except Exception as exc:  # noqa: BLE001 - socket/struct errors all mean "unreachable" here
                last_error = f"{type(exc).__name__}: {exc}"
                break
        if samples:
            offset, delay = min(samples, key=lambda x: x[1])
            best.append((offset, delay, server))
    if not best:
        return NtpCheckResult(reachable=False, offset_s=None, server=None, error=last_error)
    offsets = sorted(b[0] for b in best)
    mid = len(offsets) // 2
    median = offsets[mid] if len(offsets) % 2 else (offsets[mid - 1] + offsets[mid]) / 2.0
    if abs(median) < _NOISE_FLOOR_S:
        median = 0.0
    server_name = min(best, key=lambda b: b[1])[2]
    return NtpCheckResult(reachable=True, offset_s=median, server=server_name, error=None)
