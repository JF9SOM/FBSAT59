#!/usr/bin/env python3
"""IC-9700: does the Sub receiver's DATA (-D) mode CI-V command work,
and does rig.send_raw() actually crash the process?

Background (GitHub Issue #16): selecting an FT4 transponder on RS-44/
JO-97/MO-122 sets USB-D/LSB-D. Diagnostic logging in the app confirmed
the base sideband (USB/LSB) lands correctly on both Main and Sub, but
the DATA flag itself only sticks on Main -- Sub stays plain USB/LSB.
Root cause traced to Hamlib's icom.c: Main/Sub+A/B rigs like the
IC-9700 route Sub-targeted mode-setting through a different, older
CI-V command (C_CTL_MEM/S_MEM_DATA_MODE, "1A 06") than Main (which
uses a newer, combined "0x26" command) -- and this older command does
not appear to actually take effect on Sub for this specific rig.

A raw-CI-V workaround using Hamlib's rig.send_raw() Python binding was
tried directly inside the app and crashed the process ("stack smashing
detected") on every satmode transponder selection. However, raw CI-V
via send_raw() has reportedly worked without crashing in other,
standalone testing against an IC-9100 in the past -- so the crash may
be specific to how/when it was called (interleaved with several other
Hamlib calls on an already-open session), not to send_raw() itself.

This script runs three independent tests, EACH IN ITS OWN SUBPROCESS,
so that a hard crash (segfault / stack smashing) in one test cannot
take down the whole script or prevent the remaining tests from running:

  Test 1: rig.send_raw() in isolation -- open a fresh session, do
          almost nothing else, then try the "Select Sub -> DATA mode
          ON -> Select Main" CI-V sequence via send_raw(). Does it
          crash here too, with minimal prior activity?

  Test 2: the same send_raw() sequence, but AFTER a realistic run of
          prior Hamlib calls (satmode entry, freq presets, set_mode,
          get_mode) matching the app's actual call pattern -- to see
          whether PRIOR ACTIVITY on the session is what triggers the
          crash, not send_raw() alone.

  Test 3: the same CI-V bytes sent via plain pyserial instead of
          Hamlib's send_raw() binding entirely (the same safe approach
          already used for FTX-1F/FT-991 raw CAT elsewhere in this
          project) -- closing the Hamlib session first, writing raw
          serial directly, then reopening Hamlib afterward to confirm
          via get_mode() whether the DATA flag actually took effect.

Each test prints clear "About to call X" markers immediately before
any risky call, and a final PASS/CRASHED verdict from the *driver*
process (which detects a crashed subprocess via its exit code/signal,
even though the crashing subprocess itself cannot report on its own
death). If a test's own log output stops right after one of these
markers with no matching "... returned OK" line following it, that is
the exact call that crashed, even if the driver also reports it as
crashed some other way.

This script does NOT modify FBSAT59 itself -- it is a standalone,
throwaway verification tool, per project convention (see e.g.
scripts/test_ic9100_raw_civ.py).

Usage:
    python scripts/test_ic9700_data_mode_civ.py [port] [baud] [civ_addr_hex]

Defaults: port=COM16 (Windows) or /dev/ttyUSB0 (else)  baud=19200  civ_addr=A2

The DL/UL frequencies used to anchor satmode Main/Sub band assignment
default to RS-44's (435.610 MHz / 145.935 MHz) -- the same satellite
already used in prior real-hardware testing of this issue. No PTT/TX is
ever triggered; only mode-setting and CI-V read/write.

Requires: pyserial (pip install pyserial) for Test 3 only. Test 1/2
require the Hamlib Python binding (same as the main app).
"""

from __future__ import annotations

import subprocess
import sys
import time

DEFAULT_PORT = "COM16" if sys.platform == "win32" else "/dev/ttyUSB0"
IC9700_MODEL = 3081
DL_HZ = 435_610_000
UL_HZ = 145_935_000


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    sys.stdout.flush()


def marker(text: str) -> None:
    """Print immediately before a risky call, and flush, so a crash right
    after this line is unambiguous in the captured output."""
    print(f"  >>> {text}")
    sys.stdout.flush()


def frame(civ_addr: int, *payload: int) -> bytes:
    return bytes([0xFE, 0xFE, civ_addr, 0xE0, *payload, 0xFD])


# ---------------------------------------------------------------------------
# Worker implementations (each runs in its OWN subprocess when invoked with
# --worker; a crash here only kills that subprocess, not the driver below).
# ---------------------------------------------------------------------------


def _require_open(rig: object, label: str) -> None:  # noqa: ANN001
    """Abort cleanly (exit code, not a crash) if open() did not actually
    succeed -- send_raw() has been confirmed (see this script's own dry-run
    against a nonexistent port) to crash the whole process instead of
    raising a catchable error when called against an unopened session, so
    every test must confirm a real open before ever reaching send_raw()."""
    status = rig.error_status  # type: ignore[attr-defined]
    if status != 0:
        print(f"      error_status={status} -- {label} FAILED, aborting this test cleanly.")
        sys.exit(1)
    print(f"      error_status={status} -- OK")


def _open_satmode_session(Hamlib: object, port: str, baud: int, civ_addr: int):  # noqa: ANN001
    """Mirror the app's connect() satmode-entry sequence: open, set_func
    SATMODE, close, reopen, extra set_func (IC-9700-specific cache fix)."""
    rig = Hamlib.Rig(IC9700_MODEL)
    rig.set_conf("rig_pathname", port)
    rig.set_conf("serial_speed", str(baud))
    rig.set_conf("civaddr", f"0x{civ_addr:02X}")
    marker("open() [step 1]")
    rig.open()
    _require_open(rig, "open() [step 1]")
    time.sleep(0.3)
    marker("set_func(SATMODE, 1) [step 1]")
    rig.set_func(Hamlib.RIG_FUNC_SATMODE, 1)
    print(f"      error_status={rig.error_status}")
    time.sleep(0.5)
    rig.close()
    time.sleep(0.3)

    rig2 = Hamlib.Rig(IC9700_MODEL)
    rig2.set_conf("rig_pathname", port)
    rig2.set_conf("serial_speed", str(baud))
    rig2.set_conf("civaddr", f"0x{civ_addr:02X}")
    marker("open() [step 2]")
    rig2.open()
    _require_open(rig2, "open() [step 2]")
    time.sleep(0.5)
    marker("extra set_func(SATMODE, 1) [IC-9700 cache fix]")
    rig2.set_func(Hamlib.RIG_FUNC_SATMODE, 1)
    print(f"      error_status={rig2.error_status}")
    time.sleep(0.2)
    return rig2


def _do_freq_and_mode_setup(Hamlib: object, rig2: object, civ_addr: int) -> None:  # noqa: ANN001
    vfo_main = Hamlib.RIG_VFO_MAIN
    vfo_sub = Hamlib.RIG_VFO_SUB

    marker(f"set_freq(MAIN, {DL_HZ})")
    rig2.set_freq(vfo_main, DL_HZ)
    print(f"      error_status={rig2.error_status}")
    time.sleep(0.2)

    marker(f"set_freq(SUB, {UL_HZ})")
    rig2.set_freq(vfo_sub, UL_HZ)
    print(f"      error_status={rig2.error_status}")
    time.sleep(0.2)

    marker("set_mode(LSB, 0, SUB)  -- base sideband only, no data flag")
    rig2.set_mode(Hamlib.RIG_MODE_LSB, 0, vfo_sub)
    print(f"      error_status={rig2.error_status}")
    time.sleep(0.2)

    marker("get_mode(SUB) readback")
    mode, pb = rig2.get_mode(vfo_sub)
    print(f"      Sub mode={mode} (RIG_MODE_LSB={Hamlib.RIG_MODE_LSB}) pb={pb}")
    time.sleep(0.2)


def worker_test1(port: str, baud: int, civ_addr: int) -> None:
    section("TEST 1: send_raw() in isolation (minimal prior activity)")
    import Hamlib  # noqa: PLC0415

    Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_TRACE)

    rig = Hamlib.Rig(IC9700_MODEL)
    rig.set_conf("rig_pathname", port)
    rig.set_conf("serial_speed", str(baud))
    rig.set_conf("civaddr", f"0x{civ_addr:02X}")
    marker("open()")
    rig.open()
    _require_open(rig, "open()")
    time.sleep(0.3)

    marker("send_raw() -- Select Sub (07 D1)")
    rig.send_raw(frame(civ_addr, 0x07, 0xD1))
    print("      send_raw() (Select Sub) returned -- no crash")
    time.sleep(0.15)

    marker("send_raw() -- DATA mode ON (1A 06 01)")
    rig.send_raw(frame(civ_addr, 0x1A, 0x06, 0x01))
    print("      send_raw() (DATA ON) returned -- no crash")
    time.sleep(0.15)

    marker("send_raw() -- Select Main (07 D0)")
    rig.send_raw(frame(civ_addr, 0x07, 0xD0))
    print("      send_raw() (Select Main) returned -- no crash")
    time.sleep(0.15)

    rig.close()
    print("\nTEST 1 RESULT: PASSED (no crash)")


def worker_test2(port: str, baud: int, civ_addr: int) -> None:
    section("TEST 2: send_raw() after realistic prior Hamlib activity")
    import Hamlib  # noqa: PLC0415

    Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_TRACE)

    rig2 = _open_satmode_session(Hamlib, port, baud, civ_addr)
    _do_freq_and_mode_setup(Hamlib, rig2, civ_addr)

    marker("send_raw() -- Select Sub (07 D1)")
    rig2.send_raw(frame(civ_addr, 0x07, 0xD1))
    print("      send_raw() (Select Sub) returned -- no crash")
    time.sleep(0.15)

    marker("send_raw() -- DATA mode ON (1A 06 01)")
    rig2.send_raw(frame(civ_addr, 0x1A, 0x06, 0x01))
    print("      send_raw() (DATA ON) returned -- no crash")
    time.sleep(0.15)

    marker("send_raw() -- Select Main (07 D0)")
    rig2.send_raw(frame(civ_addr, 0x07, 0xD0))
    print("      send_raw() (Select Main) returned -- no crash")
    time.sleep(0.15)

    marker("get_mode(SUB) readback after DATA toggle")
    mode2, pb2 = rig2.get_mode(Hamlib.RIG_VFO_SUB)
    print(
        f"      Sub mode={mode2} (RIG_MODE_PKTLSB={Hamlib.RIG_MODE_PKTLSB} "
        f"would mean DATA is now ON) pb={pb2}"
    )

    rig2.close()
    print("\nTEST 2 RESULT: PASSED (no crash)")


def worker_test3(port: str, baud: int, civ_addr: int) -> None:
    section("TEST 3: plain pyserial instead of Hamlib send_raw() (FTX-1F-style bypass)")
    import Hamlib  # noqa: PLC0415

    try:
        import serial  # noqa: PLC0415
    except ImportError:
        print("ERROR: pyserial not installed. Run: pip install pyserial")
        sys.exit(1)

    Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_TRACE)

    rig2 = _open_satmode_session(Hamlib, port, baud, civ_addr)
    _do_freq_and_mode_setup(Hamlib, rig2, civ_addr)

    marker("close() Hamlib session before raw pyserial write")
    rig2.close()
    time.sleep(0.3)

    marker("open pyserial directly")
    with serial.Serial(port, baud, timeout=0.4) as ser:
        marker("pyserial write -- Select Sub (07 D1)")
        ser.reset_input_buffer()
        ser.write(frame(civ_addr, 0x07, 0xD1))
        reply = ser.read(32)
        print(f"      reply: {reply.hex() if reply else '(no response)'}")
        time.sleep(0.15)

        marker("pyserial write -- DATA mode ON (1A 06 01)")
        ser.reset_input_buffer()
        ser.write(frame(civ_addr, 0x1A, 0x06, 0x01))
        reply = ser.read(32)
        print(f"      reply: {reply.hex() if reply else '(no response)'}")
        time.sleep(0.15)

        marker("pyserial write -- Select Main (07 D0)")
        ser.reset_input_buffer()
        ser.write(frame(civ_addr, 0x07, 0xD0))
        reply = ser.read(32)
        print(f"      reply: {reply.hex() if reply else '(no response)'}")

    time.sleep(0.3)
    marker("reopen Hamlib to verify via get_mode()")
    rig3 = Hamlib.Rig(IC9700_MODEL)
    rig3.set_conf("rig_pathname", port)
    rig3.set_conf("serial_speed", str(baud))
    rig3.set_conf("civaddr", f"0x{civ_addr:02X}")
    rig3.open()
    print(f"      error_status={rig3.error_status}")
    time.sleep(0.3)
    mode3, pb3 = rig3.get_mode(Hamlib.RIG_VFO_SUB)
    print(
        f"      Sub mode={mode3} (RIG_MODE_PKTLSB={Hamlib.RIG_MODE_PKTLSB} "
        f"would mean DATA is now ON) pb={pb3}"
    )
    rig3.close()
    print("\nTEST 3 RESULT: PASSED (no crash)")


# ---------------------------------------------------------------------------
# Driver: runs each test as an isolated subprocess
# ---------------------------------------------------------------------------

_WORKERS = {
    "1": worker_test1,
    "2": worker_test2,
    "3": worker_test3,
}


def run_as_subprocess(test_id: str, port: str, baud: int, civ_addr: int) -> str:
    result = subprocess.run(
        [sys.executable, __file__, "--worker", test_id, port, str(baud), hex(civ_addr)],
    )
    if result.returncode < 0:
        return f"CRASHED (killed by signal {-result.returncode})"
    if result.returncode != 0:
        return f"exited abnormally (code {result.returncode})"
    return "completed normally"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        test_id, port, baud_s, civ_hex = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
        _WORKERS[test_id](port, int(baud_s), int(civ_hex, 16))
        return

    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
    civ_addr = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0xA2

    print(f"Port={port}  Baud={baud}  CI-V addr=0x{civ_addr:02X}  Model=IC-9700 ({IC9700_MODEL})")
    print("Each test below runs in its own subprocess; a crash in one will not stop the others.")

    verdicts: dict[str, str] = {}
    for test_id in ("1", "2", "3"):
        verdicts[test_id] = run_as_subprocess(test_id, port, baud, civ_addr)

    section("SUMMARY")
    for test_id, verdict in verdicts.items():
        print(f"  Test {test_id}: {verdict}")
    print()
    print("Please copy this entire terminal output (all three tests) and send it back.")


if __name__ == "__main__":
    main()
