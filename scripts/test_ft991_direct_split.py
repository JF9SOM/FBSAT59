#!/usr/bin/env python3
"""Test script: FT-991A Direct mode split enable / frequency set.

Usage:
    python3 scripts/test_ft991_direct_split.py [PORT] [BAUD]

Defaults: PORT=/dev/ttyUSB0  BAUD=38400

The script tests every known method to enable split on FT-991A and sets
a test UL frequency so you can confirm VFO-B moves on the rig display.
"""

import os
import sys
import time

# ── defaults ──────────────────────────────────────────────────────────────────
PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 38400

TEST_DL_HZ = 435_610_000  # RS-44 DL — set on VFO-A
TEST_UL_HZ = 145_935_000  # RS-44 UL — set on VFO-B

MODEL_FT991A = 1036

print(f"Port: {PORT}  Baud: {BAUD}")
print(f"Test DL: {TEST_DL_HZ / 1e6:.3f} MHz  UL: {TEST_UL_HZ / 1e6:.3f} MHz")
print()


# ── helpers ───────────────────────────────────────────────────────────────────


def send_cat_pyserial(cmd: bytes) -> None:
    """Send raw CAT via pyserial (reconfigures termios)."""
    import serial

    with serial.Serial(PORT, BAUD, timeout=1) as ser:
        ser.write(cmd)
        time.sleep(0.05)


def send_cat_os(cmd: bytes) -> None:
    """Send raw CAT via os.open(O_NOCTTY|O_NONBLOCK) — no termios change."""
    fd = os.open(PORT, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        os.write(fd, cmd)
    finally:
        os.close(fd)


def query_cat_pyserial(cmd: bytes, read_bytes: int = 64) -> bytes:
    """Send a query command and read the response."""
    import serial

    with serial.Serial(PORT, BAUD, timeout=1) as ser:
        ser.write(cmd)
        time.sleep(0.1)
        return ser.read(read_bytes)


# ── Step 0: query current state ───────────────────────────────────────────────
print("=" * 60)
print("STEP 0: Query current split state (ST;)")
resp = query_cat_pyserial(b"ST;")
print(f"  ST; response: {resp!r}  (ST0=off, ST1=on)")

print("  Query current VFO-A freq (FA;)")
resp = query_cat_pyserial(b"FA;")
print(f"  FA; response: {resp!r}")

print("  Query current VFO-B freq (FB;)")
resp = query_cat_pyserial(b"FB;")
print(f"  FB; response: {resp!r}")
print()

# ── Step 1: Set frequencies first ────────────────────────────────────────────
print("=" * 60)
print(f"STEP 1: Set VFO-A to DL {TEST_DL_HZ / 1e6:.3f} MHz via pyserial FA command")
send_cat_pyserial(f"FA{TEST_DL_HZ:09d};".encode())  # 9 digits for FT-991A
time.sleep(0.1)
resp = query_cat_pyserial(b"FA;")
print(f"  FA; response after set: {resp!r}")

print(f"  Set VFO-B to UL {TEST_UL_HZ / 1e6:.3f} MHz via pyserial FB command")
send_cat_pyserial(f"FB{TEST_UL_HZ:09d};".encode())  # 9 digits for FT-991A
time.sleep(0.1)
resp = query_cat_pyserial(b"FB;")
print(f"  FB; response after set: {resp!r}")
print()

# ── Step 2: Enable split via pyserial ST1; ────────────────────────────────────
print("=" * 60)
print("STEP 2: Enable split via pyserial ST1;")
send_cat_pyserial(b"ST1;")
time.sleep(0.1)
resp = query_cat_pyserial(b"ST;")
print(f"  ST; after pyserial ST1: {resp!r}  (expect ST1)")
print("  >>> CHECK: Does the SPLIT indicator light on the rig display? <<<")
input("  Press Enter to continue...")
print()

# ── Step 3: Disable split and try os.open approach ───────────────────────────
print("=" * 60)
print("STEP 3: Disable split ST0; then re-enable via os.open(O_NOCTTY)")
send_cat_pyserial(b"ST0;")
time.sleep(0.1)
resp = query_cat_pyserial(b"ST;")
print(f"  ST; after ST0: {resp!r}  (expect ST0)")

send_cat_os(b"ST1;")
time.sleep(0.1)
resp = query_cat_pyserial(b"ST;")
print(f"  ST; after os.open ST1: {resp!r}  (expect ST1)")
print("  >>> CHECK: Does the SPLIT indicator light on the rig display? <<<")
input("  Press Enter to continue...")
print()

# ── Step 4: Try Hamlib set_split_vfo ─────────────────────────────────────────
print("=" * 60)
print("STEP 4: Try Hamlib set_split_vfo (several VFO combinations)")
try:
    import Hamlib  # noqa: PLC0415

    send_cat_pyserial(b"ST0;")  # reset split first
    time.sleep(0.1)

    rig = Hamlib.Rig(MODEL_FT991A)
    rig.set_conf("rig_pathname", PORT)
    rig.set_conf("serial_speed", str(BAUD))
    rig.open()
    time.sleep(0.3)

    combos = [
        ("RIG_VFO_CURR, 1, RIG_VFO_B", Hamlib.RIG_VFO_CURR, 1, Hamlib.RIG_VFO_B),
        ("RIG_VFO_A,    1, RIG_VFO_B", Hamlib.RIG_VFO_A, 1, Hamlib.RIG_VFO_B),
        ("RIG_VFO_MAIN, 1, RIG_VFO_B", Hamlib.RIG_VFO_MAIN, 1, Hamlib.RIG_VFO_B),
        ("RIG_VFO_CURR, 1, RIG_VFO_SUB", Hamlib.RIG_VFO_CURR, 1, Hamlib.RIG_VFO_SUB),
    ]
    for label, rx, split, tx in combos:
        ret = rig.set_split_vfo(rx, split, tx)
        resp = query_cat_pyserial(b"ST;")
        print(f"  set_split_vfo({label}) ret={ret}  ST;={resp!r}")
        send_cat_pyserial(b"ST0;")
        time.sleep(0.1)

    rig.close()
except ImportError:
    print("  Hamlib not available — skipping Hamlib test")
print()

# ── Step 5: Try Hamlib set_split_freq ────────────────────────────────────────
print("=" * 60)
print("STEP 5: Enable split with pyserial ST1; then test Hamlib set_split_freq")
send_cat_pyserial(b"ST1;")
time.sleep(0.1)
try:
    import Hamlib  # noqa: PLC0415

    rig = Hamlib.Rig(MODEL_FT991A)
    rig.set_conf("rig_pathname", PORT)
    rig.set_conf("serial_speed", str(BAUD))
    rig.open()
    time.sleep(0.3)

    ret = rig.set_split_freq(Hamlib.RIG_VFO_A, TEST_UL_HZ)
    resp = query_cat_pyserial(b"FB;")
    print(f"  set_split_freq(VFOA, {TEST_UL_HZ}) ret={ret}")
    print(f"  FB; response: {resp!r}  (expect {TEST_UL_HZ:010d})")

    # Also test set_freq(VFOB)
    ret2 = rig.set_freq(Hamlib.RIG_VFO_B, TEST_UL_HZ)
    resp2 = query_cat_pyserial(b"FB;")
    print(f"  set_freq(VFOB, {TEST_UL_HZ}) ret={ret2}")
    print(f"  FB; response: {resp2!r}  (expect {TEST_UL_HZ:010d})")

    rig.close()
except ImportError:
    print("  Hamlib not available — skipping")
print()

# ── Final state ───────────────────────────────────────────────────────────────
print("=" * 60)
print("FINAL: Leaving rig in split=ON state for visual check.")
send_cat_pyserial(b"ST1;")
send_cat_pyserial(f"FA{TEST_DL_HZ:09d};".encode())
send_cat_pyserial(f"FB{TEST_UL_HZ:09d};".encode())
time.sleep(0.1)
resp_st = query_cat_pyserial(b"ST;")
resp_fa = query_cat_pyserial(b"FA;")
resp_fb = query_cat_pyserial(b"FB;")
print(f"  ST;={resp_st!r}  FA;={resp_fa!r}  FB;={resp_fb!r}")
print()
print("Done. Run 'python3 scripts/test_ft991_direct_split.py --off' to clear split.")
if len(sys.argv) > 1 and sys.argv[-1] == "--off":
    send_cat_pyserial(b"ST0;")
    print("Split cleared.")
