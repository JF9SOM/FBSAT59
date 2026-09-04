#!/usr/bin/env python3
"""Test script: can DL (Main) and UL (Sub) be read back WITHOUT any explicit
VFO switching ("V" command), mirroring how the write side already works?

Background: prior investigation confirmed the FTX-1F NET-mode write path
never sends "V" in the per-cycle loop:
    F {hz}  -- implicitly writes Main (RX/DL), no VFO switch needed
    I {hz}  -- implicitly writes Sub  (TX/UL), no VFO switch needed
("S 1 Main" is sent once at connect time; the FTX-1F rigctld backend forces
Main=RX / Sub=TX regardless of the VFO argument.)

CLAUDE.md documents that explicit "V" (active VFO switch) causes the TX LED
to light and is forbidden in the per-second Doppler cycle. A design using
"V Main" + "f" to read DL was therefore ruled out for a frequent poller.

This script tests the natural read-side counterparts instead:
    f   -- get_freq on "current VFO" (mirrors bare "F")
    i   -- get_split_freq (mirrors bare "I" -- reads the split/TX register
            directly, which by rigctld design should NOT require switching
            "current VFO" at all, since it's a distinct split-freq concept)

No "V" command is sent anywhere in this script.

Sequence:
  1. Connect, "S 1 Main" (matches production connect sequence)
  2. Write F <dl0>, write I <ul0>  (matches production's initial preset)
  3. Read back "f" and "i" immediately -- do they match dl0/ul0?
  4. Pause and ask the operator to MANUALLY retune the DL dial on the rig
     by some known amount (e.g. +5 kHz), then press Enter.
  5. Read back "f" and "i" again:
       - does "f" reflect the manual DL change?
       - is "i" (UL) unaffected, as expected (nothing should have moved it)?
  6. Write a new F (simulating the next Doppler cycle write) and confirm
     the manual offset is overwritten (this is expected -- our own write
     always wins; detecting the *delta* is a job for the app layer, not
     this script).
  7. Repeat steps 3-6 for REPEAT_CYCLES to check for consistency/drift.

Usage:
    python3 scripts/test_ftx1_split_freq_read.py [HOST] [PORT] [DL_HZ] [UL_HZ]

Defaults: HOST=127.0.0.1  PORT=4532  DL_HZ=435612000  UL_HZ=145993000
"""

from __future__ import annotations

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4532
DL_HZ = int(sys.argv[3]) if len(sys.argv) > 3 else 435_612_000
UL_HZ = int(sys.argv[4]) if len(sys.argv) > 4 else 145_993_000
TIMEOUT_S = 10.0  # matches HamlibNetController._TIMEOUT
REPEAT_CYCLES = 3


def connect() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT_S)
    sock.connect((HOST, PORT))
    return sock


def cmd(sock: socket.socket, command: str) -> tuple[str, float, str | None]:
    """Send a command, read until RPRT appears in the buffer (or timeout/error).

    For lowercase query commands (f, i, m, ...) a *successful* response has
    no RPRT line at all -- only the value, terminated by a bare newline.
    This mirrors the real _cmd_raw() fix: for query commands we stop as
    soon as we have a complete line, without waiting for RPRT.
    """
    is_query = command[:1].islower()
    t0 = time.monotonic()
    try:
        sock.sendall((command + "\n").encode())
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if is_query:
                if data.endswith(b"\n"):
                    break
            elif b"RPRT" in data:
                break
        elapsed = time.monotonic() - t0
        return data.decode(errors="replace").strip(), elapsed, None
    except OSError as exc:
        elapsed = time.monotonic() - t0
        return "", elapsed, f"{type(exc).__name__}: {exc}"


def show(label: str, sock: socket.socket, command: str) -> str:
    resp, elapsed, err = cmd(sock, command)
    tag = "FAIL" if err else "ok"
    print(
        f"    [{tag}] {label:<28s} {command!r:<14s} -> {err or resp!r}  ({elapsed * 1000:.0f} ms)"
    )
    return resp


def main() -> None:
    print("=" * 70)
    print("FTX-1F DL/UL readback WITHOUT explicit VFO switching (no 'V' sent)")
    print(f"Target: {HOST}:{PORT}   DL={DL_HZ / 1e6:.6f} MHz  UL={UL_HZ / 1e6:.6f} MHz")
    print("=" * 70)
    print()
    print("Make sure FBSAT59 (or any other rigctld client) is NOT also")
    print("actively sending commands to this rig right now.")
    input("Press ENTER to begin...")
    print()

    sock = connect()
    show("connect: S 1 Main", sock, "S 1 Main")
    print()

    for cycle in range(REPEAT_CYCLES):
        print("-" * 70)
        print(f"Cycle {cycle + 1}/{REPEAT_CYCLES}")
        dl = DL_HZ + cycle * 1000
        ul = UL_HZ + cycle * 1000

        print("  Step 1: write F (DL) and I (UL)")
        show("write F (DL)", sock, f"F {dl}")
        show("write I (UL)", sock, f"I {ul}")

        print("  Step 2: read back immediately (no V sent)")
        f1 = show("read f (expect DL)", sock, "f")
        i1 = show("read i (expect UL)", sock, "i")
        f_ok = f1.strip() == str(dl)
        i_ok = i1.strip() == str(ul)
        print(f"    -> f matches DL written? {f_ok}   i matches UL written? {i_ok}")
        print()

        print("  Step 3: manual retune check")
        print(f"    Please turn the DL (Main) dial by some KNOWN amount now,")
        print(f"    e.g. +5000 Hz from {dl} Hz, then press ENTER.")
        input("    Press ENTER once you've retuned the dial...")
        f2 = show("read f (after manual retune)", sock, "f")
        i2 = show("read i (UL, should be unchanged)", sock, "i")
        try:
            f2_val = float(f2.strip())
            delta = f2_val - dl
            print(
                f"    -> f changed by {delta:+.0f} Hz since last F write (expected ~+5000 Hz if you moved it)"
            )
        except ValueError:
            print(f"    -> could not parse f response as a frequency: {f2!r}")
        try:
            i2_val = float(i2.strip())
            i_delta = i2_val - ul
            print(f"    -> i changed by {i_delta:+.0f} Hz since last I write (expected ~0 Hz)")
        except ValueError:
            print(f"    -> could not parse i response as a frequency: {i2!r}")
        print()

        print("  Step 4: next-cycle F write should overwrite the manual offset")
        next_dl = dl + 2000
        show("write F (simulated next Doppler cycle)", sock, f"F {next_dl}")
        f3 = show("read f (expect next_dl, manual offset gone)", sock, "f")
        print()

    sock.close()
    print("=" * 70)
    print("Done. Review the [FAIL] tags and the delta lines above.")
    print("Key questions to answer from this run:")
    print("  1. Did 'f' ever return an unexpected/stale value (e.g. matching")
    print("     UL instead of DL) at any point, without any 'V' being sent?")
    print("  2. Did 'i' reliably read back the UL value written by 'I',")
    print("     and stay stable across subsequent F writes?")
    print("  3. Did manually retuning the DL dial show up correctly in 'f'?")
    print("=" * 70)


if __name__ == "__main__":
    main()
