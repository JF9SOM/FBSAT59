#!/usr/bin/env python3
"""Isolate whether FT4's CAT PTT (rigctld "T 1"/"T 0") actually keys the rig.

This bypasses the whole FT4 tab / scheduler / QSO state machine and reproduces
exactly what HamlibNetController.set_ptt() does (src/rig/controller.py):

    T 1   -> key up
    (wait)
    T 0   -> key down

Usage:
    python3 scripts/test_ft4_ptt_net.py [HOST [PORT]]

Defaults: HOST=localhost PORT=4532

Requires rigctld already running and connected to the rig (same rigctld
instance FBSAT59's NET mode uses).
"""

from __future__ import annotations

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4532
TIMEOUT = 10.0


def cmd(sock: socket.socket, command: str) -> str:
    """Send one rigctld command and read until RPRT appears (same as _cmd_raw)."""
    sock.sendall((command + "\n").encode())
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if b"RPRT" in data:
            break
    return data.decode(errors="replace").strip()


def main() -> None:
    print(f"Connecting to rigctld at {HOST}:{PORT} ...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    sock.connect((HOST, PORT))
    print("Connected.\n")

    print('Reading current PTT state ("t")...')
    resp = cmd(sock, "t")
    print(f"  response: {resp!r}\n")

    input("Press Enter to send 'T 1' (PTT ON) — watch the rig's TX LED...")
    resp = cmd(sock, "T 1")
    print(f"  'T 1' response: {resp!r}")
    if "RPRT 0" not in resp:
        print("  !! rigctld did NOT return RPRT 0 -> PTT command was rejected/failed")
    else:
        print("  rigctld accepted the command (RPRT 0).")
    print("  >>> CHECK NOW: is the rig actually transmitting (TX lamp lit)? <<<\n")

    input("Press Enter to send 'T 0' (PTT OFF)...")
    resp = cmd(sock, "T 0")
    print(f"  'T 0' response: {resp!r}")
    if "RPRT 0" not in resp:
        print("  !! rigctld did NOT return RPRT 0 for PTT OFF")
    else:
        print("  rigctld accepted the command (RPRT 0).")
    print("  >>> CHECK NOW: did the rig stop transmitting? <<<\n")

    sock.close()
    print("Done.")


if __name__ == "__main__":
    main()
