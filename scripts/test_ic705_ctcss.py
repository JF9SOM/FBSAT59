#!/usr/bin/env python3
"""Standalone-session CTCSS test for IC-705.

Opens a fresh Hamlib session (rig disconnected, as it would be right after
transponder selection), sets FM mode (TONE indicator only shows in FM),
sets the CTCSS tone frequency, then enables the encoder via set_func()
with explicit delays between every CI-V transaction (the existing
_apply_ctcss_civ_via_send_raw() helper in controller.py sleeps 0.15s
between raw frames for the same reason — Icom CI-V over USB-serial needs
breathing room between commands). Avoids send_raw() entirely: an earlier
run of this script crashed Python with "stack smashing detected" inside
Hamlib's SWIG send_raw() binding.

Usage:
    python3 scripts/test_ic705_ctcss.py [PORT [BAUD]]

Defaults: PORT=/dev/ttyACM0  BAUD=19200
"""

import sys
import time

import Hamlib

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
MODEL = 3085  # IC-705
TONE_HZ = 100.0
DELAY = 0.2

print(f"Hamlib version: {Hamlib.hamlib_version}")
Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_ERR)

rig = Hamlib.Rig(MODEL)
rig.set_conf("rig_pathname", PORT)
rig.set_conf("serial_speed", str(BAUD))

print("Opening...")
rig.open()
time.sleep(0.3)

vfo_a = Hamlib.RIG_VFO_A
vfo_b = Hamlib.RIG_VFO_B
tone_int = int(round(TONE_HZ * 10))

print()
print("=== set_mode(FM) on VFOB then VFOA (TONE indicator only shows in FM) ===")
rig.set_vfo(vfo_b)
time.sleep(DELAY)
rig.set_mode(Hamlib.RIG_MODE_FM, 0)
time.sleep(DELAY)
rig.set_vfo(vfo_a)
time.sleep(DELAY)
rig.set_mode(Hamlib.RIG_MODE_FM, 0)
time.sleep(DELAY)

print()
print(f"=== VFOB: set_ctcss_tone({tone_int}) then set_func(TONE, 1), each with delay ===")
rig.set_vfo(vfo_b)
time.sleep(DELAY)
ret_tone = rig.set_ctcss_tone(vfo_b, tone_int)
print(f"set_ctcss_tone ret={ret_tone}")
time.sleep(DELAY)
ret_func = rig.set_func(vfo_b, Hamlib.RIG_FUNC_TONE, 1)
print(f"set_func ret={ret_func}")
time.sleep(DELAY)

readback_tone_b = rig.get_ctcss_tone(vfo_b)
print(f"readback VFOB tone={readback_tone_b}")

print()
print(">>> CHECK ON RADIO NOW (while still on VFOB / before restoring VFOA): TONE indicator? <<<")
time.sleep(2.0)

print()
print("=== set_vfo(VFOA) — restore Main display (tone stays ON on VFOB) ===")
rig.set_vfo(vfo_a)
time.sleep(DELAY)

print()
print("(Tone is left enabled intentionally so it can be checked after this script exits.)")

print()
print("Closing.")
rig.close()
