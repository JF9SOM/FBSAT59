#!/usr/bin/env python3
"""IC-9100 CTCSS diagnostic: is "L CTCSS_TONE" really broken via rigctld?

Background (see CLAUDE.md "ICOM SATMODE機（IC-9100/9700等）NETモードCTCSS —
`L CTCSS_TONE` が壊れている疑い"): the IC-705 investigation found that
rigctld rejects "L CTCSS_TONE <value>" with RPRT -11 (ENAVAIL) because
CTCSS_TONE is not a rigctld LEVEL — the correct command is "C <deci_hz>".
`_apply_ctcss_civ_direct()` (src/rig/controller.py, satmode NET-mode CTCSS
path used for IC-9100/IC-9700/IC-910H/IC-821H) still uses the old
"L CTCSS_TONE" form and has never been verified live on a satmode rig.

This script talks to rigctld directly (bypassing the app) and prints the
raw RPRT response for both the old command and the proposed replacement,
plus a readback, so the hypothesis can be confirmed/refuted on the spot
without needing to patch and redeploy the app first.

Usage:
    python3 scripts/test_ic9100_ctcss_civ.py [host] [port] [tone_hz]

Defaults: host=127.0.0.1  port=4532  tone_hz=88.5

Prerequisite: rigctld already running against the IC-9100
(e.g. `rigctld -m <model> -r /dev/ttyUSBx ...`), with SAT mode already
established (this script does not touch split/SAT mode setup).
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4532
TONE_HZ = float(sys.argv[3]) if len(sys.argv) > 3 else 88.5
TONE_DECI = int(round(TONE_HZ * 10))


def connect() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    sock.connect((HOST, PORT))
    return sock


def send_recv(sock: socket.socket, cmd: str) -> str:
    sock.sendall((cmd + "\n").encode())
    buf = b""
    try:
        while b"RPRT" not in buf:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
    except OSError:
        pass
    return buf.decode(errors="replace").strip()


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


print(f"rigctld: {HOST}:{PORT}   test tone: {TONE_HZ} Hz (deci={TONE_DECI})")
sock = connect()

section("V Sub  — select Sub (TX/UL) VFO")
print(" ->", repr(send_recv(sock, "V Sub")))

section('OLD form: L CTCSS_TONE {deci}  — expected to FAIL with RPRT -11 per IC-705 findings')
resp_old = send_recv(sock, f"L CTCSS_TONE {TONE_DECI}")
print(" ->", repr(resp_old))
if "RPRT -11" in resp_old:
    print("    CONFIRMED: same ENAVAIL failure as IC-705. _apply_ctcss_civ_direct() is broken.")
elif "RPRT 0" in resp_old:
    print("    UNEXPECTED: succeeded here. IC-9100 backend may differ from IC-705's.")
else:
    print("    UNEXPECTED response — inspect manually.")

section("l CTCSS_TONE  — read back tone frequency after the OLD form attempt")
print(" ->", repr(send_recv(sock, "l CTCSS_TONE")))

section(f"NEW form: C {TONE_DECI}  — proposed replacement command")
resp_new = send_recv(sock, f"C {TONE_DECI}")
print(" ->", repr(resp_new))
if "RPRT 0" in resp_new:
    print("    OK: accepted. This is the fix candidate.")
else:
    print("    Did NOT succeed either — needs further investigation before patching.")

section("c  — read back tone frequency after the NEW form")
print(" ->", repr(send_recv(sock, "c")))

section("U TONE 1 — enable CTCSS encoder on Sub")
print(" ->", repr(send_recv(sock, "U TONE 1")))

time.sleep(0.2)

section("u TONE — read back encoder enable state")
print(" ->", repr(send_recv(sock, "u TONE")))

section("Restore: V Main, U TONE 0 (clear bleed-through on Main)")
print(" V Main ->", repr(send_recv(sock, "V Main")))
print(" U TONE 0 ->", repr(send_recv(sock, "U TONE 0")))

sock.close()

print()
print("=" * 70)
print("Manual check: does the IC-9100's actual CTCSS tone menu/display show")
print(f"{TONE_HZ} Hz now? (readback above only proves rigctld's own state,")
print("not necessarily that the rig's CTCSS circuit changed.)")
print("=" * 70)
