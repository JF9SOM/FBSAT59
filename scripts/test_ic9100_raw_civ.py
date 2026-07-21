#!/usr/bin/env python3
"""IC-9100: does raw pyserial CI-V communication survive REPEATED commands?

Background (see CLAUDE.md's IC-9100 Windows investigation, 2026-07):
Hamlib's rig.open() times out (Hamlib error -5, RIG_ETIMEOUT) on every
attempt on a specific Windows 11 + IC-9100 setup (remote-jack CI-V +
generic FTDI FT232R USB-serial cable) -- confirmed on two different
Windows PCs, with a verified-correct/matching CI-V address, and with a
different rig (FTX-1F, Direct mode) working fine on the same PC. The
exact same IC-9100 + cable combination works correctly on Linux.

A single raw CI-V command via plain pyserial (the existing Rig Settings
"Test" button) succeeds reliably. Reading Hamlib's own Windows serial
implementation (lib/termios.c, vendored from the ~2001 rxtx Java library)
found it uses overlapped (asynchronous) Win32 I/O for writes, with a
code comment acknowledging past quirks with "USB to Serial devices"
specifically -- a very different, more complex code path than pyserial's
simpler synchronous Windows serial handling.

This script tests the actual hypothesis behind the proposed fix (bypass
Hamlib's serial I/O entirely for this rig family on Windows, the same
way FTX-1F/FT-991 already do): does plain pyserial hold up under
REPEATED commands (not just one), including an open/close/reopen cycle
matching Hamlib's own satmode-entry sequence? If this script is NOT
reliable either, the raw-CAT-bypass plan needs rethinking before any
app code is touched.

This script does NOT modify FBSAT59 itself -- it is a standalone,
throwaway verification tool, per project convention (see e.g.
scripts/test_ic9100_ctcss_civ.py).

Usage:
    python scripts/test_ic9100_raw_civ.py [port] [baud] [civ_addr_hex]

Defaults: port=COM16 (Windows) or /dev/ttyUSB0 (else)  baud=19200  civ_addr=A2

Requires: pyserial (pip install pyserial)
"""

from __future__ import annotations

import sys
import time

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

DEFAULT_PORT = "COM16" if sys.platform == "win32" else "/dev/ttyUSB0"
PORT = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
CIV_ADDR = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0xA2

CTRL_ID = 0xE0
C_RD_FREQ = 0x03
C_SET_FREQ = 0x05
FE = 0xFE
FD = 0xFD


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def freq_to_bcd(freq_hz: int) -> bytes:
    """Icom CI-V frequency encoding: 5 BCD bytes, LSB (10s of Hz) first."""
    digits = f"{freq_hz:010d}"[::-1]  # reversed decimal string, 10 digits
    out = bytearray()
    for i in range(0, 10, 2):
        lo = int(digits[i])
        hi = int(digits[i + 1]) if i + 1 < 10 else 0
        out.append((hi << 4) | lo)
    return bytes(out)


def bcd_to_freq(data: bytes) -> int:
    digits: list[str] = []
    for b in data:
        digits.append(str(b & 0x0F))
        digits.append(str((b >> 4) & 0x0F))
    return int("".join(reversed(digits)) or "0")


def build_frame(cmd: int, data: bytes = b"") -> bytes:
    return bytes([FE, FE, CIV_ADDR, CTRL_ID, cmd]) + data + bytes([FD])


def send_and_read(ser: serial.Serial, frame: bytes, read_len: int = 32) -> bytes:
    ser.reset_input_buffer()
    ser.write(frame)
    return bytes(ser.read(read_len))


def parse_reply(reply: bytes, expect_cmd: int) -> tuple[bool, str]:
    """Very small CI-V reply parser sufficient for this test's needs."""
    if not reply:
        return False, "no response (timeout)"
    if reply[:2] != bytes([FE, FE]):
        return False, f"unexpected preamble: {reply.hex()}"
    if len(reply) < 6:
        return False, f"reply too short: {reply.hex()}"
    if reply[4] == 0xFA:
        return False, f"NAK (rig rejected command): {reply.hex()}"
    if reply[4] != expect_cmd:
        return True, f"got reply but cmd byte differs (0x{reply[4]:02x}): {reply.hex()}"
    return True, reply.hex()


def read_freq_once(ser: serial.Serial, label: str) -> bool:
    reply = send_and_read(ser, build_frame(C_RD_FREQ))
    ok, detail = parse_reply(reply, C_RD_FREQ)
    if ok and len(reply) >= 11:
        freq = bcd_to_freq(reply[5:10])
        print(f"  {label}: OK  freq={freq} Hz  raw={detail}")
    else:
        print(f"  {label}: FAIL  {detail}")
    return ok


def set_freq_once(ser: serial.Serial, freq_hz: int, label: str) -> bool:
    reply = send_and_read(ser, build_frame(C_SET_FREQ, freq_to_bcd(freq_hz)))
    ok, detail = parse_reply(reply, C_SET_FREQ)
    if not ok:
        # C_SET_FREQ acks with cmd byte 0xFB (ACK), not an echoed C_SET_FREQ
        ok = len(reply) >= 6 and reply[4] == 0xFB
        detail = f"ACK check: {reply.hex()}" if reply else detail
    print(f"  {label}: {'OK' if ok else 'FAIL'}  {detail}")
    return ok


print(f"Port={PORT}  Baud={BAUD}  CI-V addr=0x{CIV_ADDR:02X}")

# ---------------------------------------------------------------------------
section("Test 1: single open, single read-frequency (baseline, = Test button)")
# ---------------------------------------------------------------------------
try:
    with serial.Serial(PORT, BAUD, timeout=0.4) as ser:
        ok1 = read_freq_once(ser, "attempt 1/1")
except serial.SerialException as exc:
    print(f"  FAILED TO OPEN PORT: {exc}")
    sys.exit(1)

if not ok1:
    print("\nBaseline failed -- port/address/baud is wrong. Stop here.")
    sys.exit(1)

# ---------------------------------------------------------------------------
section("Test 2: single open, 20x repeated read-frequency (~1/sec, like Doppler loop)")
# ---------------------------------------------------------------------------
successes = 0
attempts = 20
with serial.Serial(PORT, BAUD, timeout=0.4) as ser:
    for i in range(1, attempts + 1):
        if read_freq_once(ser, f"attempt {i}/{attempts}"):
            successes += 1
        time.sleep(0.5)
print(f"\n  Result: {successes}/{attempts} succeeded")

# ---------------------------------------------------------------------------
section("Test 3: open -> close -> reopen -> 5x read-frequency (mimics Hamlib satmode entry)")
# ---------------------------------------------------------------------------
try:
    with serial.Serial(PORT, BAUD, timeout=0.4) as ser:
        read_freq_once(ser, "pre-close probe")
    time.sleep(0.3)
    successes3 = 0
    with serial.Serial(PORT, BAUD, timeout=0.4) as ser:
        for i in range(1, 6):
            if read_freq_once(ser, f"post-reopen attempt {i}/5"):
                successes3 += 1
            time.sleep(0.3)
    print(f"\n  Result: {successes3}/5 succeeded after reopen")
except serial.SerialException as exc:
    print(f"  FAILED during open/close/reopen: {exc}")

# ---------------------------------------------------------------------------
section("Test 4: 10x set-frequency (write path, not just read) -- CAUTION: retunes the rig")
# ---------------------------------------------------------------------------
print("  Skipped by default to avoid surprising anyone with the rig connected.")
print("  Uncomment the block in this script to run it manually if tests 1-3 pass.")
# with serial.Serial(PORT, BAUD, timeout=0.4) as ser:
#     base_freq = 435_600_000
#     successes4 = 0
#     for i in range(10):
#         if set_freq_once(ser, base_freq + i, f"set attempt {i + 1}/10"):
#             successes4 += 1
#         time.sleep(0.5)
#     print(f"\n  Result: {successes4}/10 succeeded")

section("Summary")
print(f"  Test 1 (baseline):        {'PASS' if ok1 else 'FAIL'}")
print(f"  Test 2 (repeated reads):  {successes}/{attempts}")
print(f"  Test 3 (reopen + reads):  {successes3}/5")
print()
if successes == attempts and successes3 == 5:
    print("  All raw-pyserial tests passed reliably.")
    print("  This supports bypassing Hamlib's serial I/O for this rig on Windows.")
else:
    print("  Raw pyserial ALSO shows failures under repetition.")
    print("  The raw-CAT-bypass plan needs rethinking -- the problem may not be")
    print("  specific to Hamlib's serial code after all.")
