#!/usr/bin/env python3
"""Hamlib trace: what CAT bytes does Hamlib actually send for split on FT-991A?

Usage:
    python3 scripts/test_hamlib_split_trace.py [PORT [BAUD]]

Defaults: PORT=/dev/ttyUSB0  BAUD=38400

Enables Hamlib TRACE debug so every byte sent/received is printed.
"""

import sys
import time

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 38400
MODEL = 1036  # FT-991A

try:
    import Hamlib
except ImportError:
    print("ERROR: Hamlib not available")
    sys.exit(1)

print(f"Hamlib version: {Hamlib.hamlib_version}")
print(f"Port: {PORT}  Baud: {BAUD}  Model: {MODEL} (FT-991A)")
print()

# Enable maximum Hamlib debug output so we can see every byte.
Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_TRACE)

rig = Hamlib.Rig(MODEL)
rig.set_conf("rig_pathname", PORT)
rig.set_conf("serial_speed", str(BAUD))

print("=" * 60)
print("Opening Hamlib connection (watch trace for init bytes)...")
print("=" * 60)
rig.open()
time.sleep(0.5)

print()
print("=" * 60)
print("get_freq(VFOA) — confirm connection works")
print("=" * 60)
freq = rig.get_freq()
print(f"  VFOA freq = {freq} Hz = {freq / 1e6:.3f} MHz")

print()
print("=" * 60)
print("set_split_vfo(RIG_VFO_CURR, 1, RIG_VFO_B) — enable split")
print("=" * 60)
ret = rig.set_split_vfo(Hamlib.RIG_VFO_CURR, 1, Hamlib.RIG_VFO_B)
print(f"  return value: {ret}")

print()
print("=" * 60)
print("get_split_vfo() — query current split state")
print("=" * 60)
try:
    split, tx_vfo = rig.get_split_vfo(Hamlib.RIG_VFO_CURR)
    print(f"  split={split} tx_vfo={tx_vfo}")
except Exception as e:
    print(f"  get_split_vfo failed: {e}")

print()
print("  >>> CHECK: SPLIT indicator on rig display? <<<")
input("  Press Enter to test UL freq set (VFO-B = 145.935 MHz)...")

print()
print("=" * 60)
print("set_freq(RIG_VFO_B, 145935000) — set UL frequency")
print("=" * 60)
ret2 = rig.set_freq(Hamlib.RIG_VFO_B, 145935000)
print(f"  return value: {ret2}")

print()
print("  >>> CHECK: Did VFO-B change on rig display? <<<")
input("  Press Enter to try set_split_freq...")

print()
print("=" * 60)
print("set_split_freq(RIG_VFO_CURR, 145935000) — alternative UL set")
print("=" * 60)
ret3 = rig.set_split_freq(Hamlib.RIG_VFO_CURR, 145935000)
print(f"  return value: {ret3}")
print()
print("  >>> CHECK: Did VFO-B change? <<<")
input("  Press Enter to close...")

rig.close()
print("Done.")
