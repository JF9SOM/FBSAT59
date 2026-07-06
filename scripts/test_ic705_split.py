#!/usr/bin/env python3
"""Non-interactive Hamlib trace for IC-705 split/freq behaviour.

Mirrors what src/rig/controller.py's generic (non-satmode) Direct-mode
branch does when driving a satellite pass (DL on VFOA, UL split to VFOB):
    set_split_vfo(VFOA, 1, VFOB)
    set_freq(VFOA, dl_hz)
    set_split_freq(VFOA, ul_hz)   # <- what the buggy code used to call

After each write, read back VFOA/VFOB frequencies (and split state) via
Hamlib get_* calls so the actual rig state can be confirmed without
eyeballing the radio's screen.

Usage:
    python3 scripts/test_ic705_split.py [PORT [BAUD]]

Defaults: PORT=/dev/ttyACM0  BAUD=19200
"""

import sys
import time

import Hamlib

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
MODEL = 3085  # IC-705

print(f"Hamlib version: {Hamlib.hamlib_version}")
Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_ERR)

rig = Hamlib.Rig(MODEL)
rig.set_conf("rig_pathname", PORT)
rig.set_conf("serial_speed", str(BAUD))

print("Opening...")
rig.open()
time.sleep(0.3)


def dump(label: str) -> None:
    fa = rig.get_freq(Hamlib.RIG_VFO_A)
    fb = rig.get_freq(Hamlib.RIG_VFO_B)
    try:
        split, txvfo = rig.get_split_vfo(Hamlib.RIG_VFO_CURR)
    except Exception as e:
        split, txvfo = f"ERR:{e}", None
    print(
        f"[{label}] VFOA={fa / 1e6:.6f} MHz  VFOB={fb / 1e6:.6f} MHz  split={split} tx_vfo={txvfo}"
    )


dump("after open")

print()
print("=== set_split_vfo(VFOA, 1, VFOB) ===")
ret = rig.set_split_vfo(Hamlib.RIG_VFO_A, 1, Hamlib.RIG_VFO_B)
print(f"ret={ret}")
dump("after set_split_vfo")

DL = 435612000
UL = 145993000

print()
print(f"=== set_freq(VFOA, {DL}) ===")
try:
    rig.set_freq(Hamlib.RIG_VFO_A, DL)
except Exception as e:
    print(f"EXCEPTION: {e}")
dump("after set_freq(VFOA, DL)")

print()
print(f"=== set_split_freq(VFOA, {UL})  <- what the old (buggy) code did ===")
try:
    rig.set_split_freq(Hamlib.RIG_VFO_A, UL)
except Exception as e:
    print(f"EXCEPTION: {e}")
dump("after set_split_freq(VFOA, UL)")

print()
print("=== ALTERNATIVE: set_split_freq(RIG_VFO_CURR, UL) ===")
try:
    rig.set_split_freq(Hamlib.RIG_VFO_CURR, UL)
except Exception as e:
    print(f"EXCEPTION: {e}")
dump("after set_split_freq(CURR, UL)")

print()
print("=== FIX: set_freq(VFOB, UL) directly ===")
try:
    rig.set_freq(Hamlib.RIG_VFO_B, UL)
except Exception as e:
    print(f"EXCEPTION: {e}")
dump("after set_freq(VFOB, UL)")

print()
print("Closing.")
rig.close()
