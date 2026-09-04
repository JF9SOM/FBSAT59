#!/usr/bin/env python3
"""Test script: does rigctld's 'f' (get_freq) command really misbehave on the
FTX-1F, and if so, under what conditions?

Background: CLAUDE.md documents (2026-05-20, real FTX-1F test) that 'f'/'i'
sent right after 'F'/'I' can take 10+ seconds and trigger a disconnect. This
script re-verifies that claim precisely, and additionally measures whether a
short delay after the write makes the read safe (which would make GPredict-
style "dial feedback" — periodic get_freq to detect manual VFO retuning —
usable after all, if a suitable delay exists).

Mimics HamlibNetController._cmd_raw()'s exact wire protocol: sends
"<command>\\n", reads until "RPRT" appears in the accumulated response, same
10 s socket timeout as production (src/rig/controller.py's _TIMEOUT).

IMPORTANT: run this with nothing else talking to the same rigctld instance
(e.g. close FBSAT59, or at least make sure it isn't actively polling this
rig), otherwise F/I writes from both processes will interleave and the
timing results will be meaningless.

Usage:
    python3 scripts/test_ftx1_getfreq_net.py [HOST] [PORT] [TEST_FREQ_HZ]

Defaults: HOST=127.0.0.1  PORT=4532  TEST_FREQ_HZ=435612000
"""

from __future__ import annotations

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4532
TEST_FREQ_HZ = int(sys.argv[3]) if len(sys.argv) > 3 else 435_612_000
TIMEOUT_S = 10.0  # matches HamlibNetController._TIMEOUT

DELAYS_MS = [0, 50, 100, 200, 500, 1000, 2000, 5000]
REPEAT_CYCLES = 3  # how many times to repeat the full write+read battery


def connect() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT_S)
    sock.connect((HOST, PORT))
    return sock


def cmd(sock: socket.socket, command: str) -> tuple[str, float, str | None]:
    """Send a command, read until RPRT appears (or timeout/error).

    Returns (response_text, elapsed_seconds, error_or_None).
    """
    t0 = time.monotonic()
    try:
        sock.sendall((command + "\n").encode())
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"RPRT" in data:
                break
        elapsed = time.monotonic() - t0
        return data.decode(errors="replace").strip(), elapsed, None
    except OSError as exc:
        elapsed = time.monotonic() - t0
        return "", elapsed, f"{type(exc).__name__}: {exc}"


def fresh_connection(label: str) -> socket.socket:
    """(Re)connect and send S 1 Main, matching production's connect sequence."""
    print(f"  [{label}] connecting to {HOST}:{PORT} ...")
    sock = connect()
    resp, elapsed, err = cmd(sock, "S 1 Main")
    status = err or resp
    print(f"  [{label}] S 1 Main -> {status!r} ({elapsed * 1000:.0f} ms)")
    return sock


def main() -> None:
    print("=" * 70)
    print("FTX-1F get_freq ('f') behaviour test — rigctld NET mode")
    print(f"Target: {HOST}:{PORT}   Test freq: {TEST_FREQ_HZ / 1e6:.6f} MHz")
    print("=" * 70)
    print()
    print("Make sure FBSAT59 (or any other rigctld client) is NOT also")
    print("actively sending commands to this rig right now.")
    input("Press ENTER to begin...")
    print()

    results: list[dict[str, object]] = []

    # ── Test A: baseline 'f' with no prior write in this session ──────────
    print("-" * 70)
    print("Test A: baseline 'f' immediately after connect (no F sent yet)")
    sock = fresh_connection("A")
    resp, elapsed, err = cmd(sock, "f")
    ok = err is None and elapsed < 2.0
    print(f"  f -> {err or resp!r}  ({elapsed * 1000:.0f} ms)  {'OK' if ok else 'SLOW/FAIL'}")
    results.append({"test": "A: baseline f (no prior F)", "elapsed_s": elapsed, "err": err})
    sock.close()
    print()

    # ── Test B: F then f with varying delay ────────────────────────────────
    print("-" * 70)
    print("Test B: F <freq> ; then f, with varying delay after the write")
    for delay_ms in DELAYS_MS:
        sock = fresh_connection(f"B delay={delay_ms}ms")
        resp, w_elapsed, w_err = cmd(sock, f"F {TEST_FREQ_HZ}")
        print(f"    F {TEST_FREQ_HZ} -> {w_err or resp!r}  ({w_elapsed * 1000:.0f} ms)")
        if w_err:
            print("    (write itself failed — skipping read)")
            results.append(
                {"test": f"B: F then f, delay={delay_ms}ms", "elapsed_s": None, "err": w_err}
            )
            sock.close()
            continue
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        resp, r_elapsed, r_err = cmd(sock, "f")
        ok = r_err is None and r_elapsed < 2.0
        tag = "OK" if ok else ("TIMEOUT/ERROR" if r_err else "SLOW")
        print(
            f"    f (after {delay_ms}ms delay) -> {r_err or resp!r}"
            f"  ({r_elapsed * 1000:.0f} ms)  {tag}"
        )
        results.append(
            {"test": f"B: F then f, delay={delay_ms}ms", "elapsed_s": r_elapsed, "err": r_err}
        )
        sock.close()
        print()

    # ── Test C: repeated write+read cycles at a fixed "candidate safe" delay ──
    CANDIDATE_DELAY_MS = 500
    print("-" * 70)
    print(
        f"Test C: {REPEAT_CYCLES}x repeated F-then-f cycles at "
        f"{CANDIDATE_DELAY_MS}ms delay (consistency check)"
    )
    sock = fresh_connection("C")
    for i in range(REPEAT_CYCLES):
        freq = TEST_FREQ_HZ + i * 1000  # nudge frequency each cycle
        resp, w_elapsed, w_err = cmd(sock, f"F {freq}")
        if w_err:
            print(f"    cycle {i}: F failed -> {w_err}")
            results.append({"test": f"C: cycle {i} F", "elapsed_s": None, "err": w_err})
            break
        time.sleep(CANDIDATE_DELAY_MS / 1000.0)
        resp, r_elapsed, r_err = cmd(sock, "f")
        ok = r_err is None and r_elapsed < 2.0
        tag = "OK" if ok else ("TIMEOUT/ERROR" if r_err else "SLOW")
        print(
            f"    cycle {i}: F {freq} then f -> {r_err or resp!r}"
            f"  ({r_elapsed * 1000:.0f} ms)  {tag}"
        )
        results.append({"test": f"C: cycle {i}", "elapsed_s": r_elapsed, "err": r_err})
        if r_err:
            print("    (socket died — reconnecting for next cycle)")
            sock = fresh_connection(f"C cycle {i + 1}")
    sock.close()
    print()

    # ── Summary ─────────────────────────────────────────────────────────────
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    for r in results:
        elapsed_s = r["elapsed_s"]
        elapsed_str = f"{elapsed_s * 1000:.0f} ms" if isinstance(elapsed_s, float) else "n/a"
        err = r["err"]
        status = (
            "FAIL"
            if err
            else ("SLOW" if isinstance(elapsed_s, float) and elapsed_s >= 2.0 else "OK")
        )
        print(f"  [{status:4s}] {r['test']:<40s} {elapsed_str:>10s}  {err or ''}")
    print()
    n_fail = sum(1 for r in results if r["err"])
    n_slow = sum(
        1
        for r in results
        if not r["err"] and isinstance(r["elapsed_s"], float) and r["elapsed_s"] >= 2.0
    )
    n_ok = len(results) - n_fail - n_slow
    print(f"Total: {len(results)}  FAIL={n_fail}  SLOW(>=2s)={n_slow}  OK={n_ok}")


if __name__ == "__main__":
    main()
