#!/usr/bin/env python3
"""IC-9700: does the Sub receiver's DATA (-D) mode CI-V command actually work?

Background (GitHub Issue #16): selecting an FT4 transponder on RS-44/
JO-97/MO-122 sets USB-D/LSB-D. Diagnostic logging in the app confirmed
the base sideband (USB/LSB) lands correctly on both Main and Sub, but
the DATA flag itself only sticks on Main -- Sub stays plain USB/LSB.
Root cause traced to Hamlib's icom.c: Main/Sub+A/B rigs like the
IC-9700 route Sub-targeted mode-setting through a different, older
CI-V command (C_CTL_MEM/S_MEM_DATA_MODE, "1A 06") than Main (which
uses a newer, combined "0x26" command) -- and this older command does
not appear to actually take effect on Sub for this specific rig.

This version of the script talks to the rig with PLAIN pyserial only --
it does NOT use the Hamlib Python binding at all, so it can be run on a
machine that only has Python + pyserial installed (no separate Hamlib
Python module needed). It builds the exact CI-V byte sequences Hamlib
itself would send, straight from the Hamlib 4.7 C source
(rigs/icom/icom.c / icom_defs.h), so the results describe the real
protocol-level behaviour, not just this script's own logic.

It runs three independent tests against the Sub (UL) VFO:

  Test 1: DATA mode ON via the legacy "1A 06" command, with MINIMAL
          prior state (just: open port, enter SAT mode, select Sub,
          send DATA ON, read back). A "clean slate" baseline.

  Test 2: the same legacy "1A 06" DATA-ON command, but after a
          realistic full setup matching the app's actual sequence
          (SAT mode ON, Main/Sub frequency presets, Sub base mode set
          to LSB, mode read-back) -- reproducing the exact call
          pattern that showed the bug live.

  Test 3: the FAST/combined "0x26" command (mode + data-mode + filter
          in a single command) aimed directly at Sub as the
          "unselected VFO", bypassing the legacy path entirely. Hamlib
          itself refuses to use this fast command for Sub on Main/Sub+
          A/B rigs like the IC-9700 (it assumes only the legacy path
          works there) -- this test checks whether that assumption is
          actually correct on real hardware, or whether the rig would
          have accepted the fast command on Sub all along.

Each test reads back the Sub mode/data-mode state afterward so you can
see directly whether the DATA flag actually took hold.

This script does NOT modify FBSAT59 itself -- it is a standalone,
throwaway verification tool, per project convention (see e.g.
scripts/test_ic9100_raw_civ.py).

Usage:
    python test_ic9700_data_mode_civ.py [port] [baud] [civ_addr_hex]

Defaults: port=COM16 (Windows) or /dev/ttyUSB0 (else)  baud=19200  civ_addr=A2

The DL/UL frequencies used to anchor satmode Main/Sub band assignment
default to RS-44's (435.610 MHz / 145.935 MHz) -- the same satellite
already used in prior real-hardware testing of this issue. No PTT/TX is
ever triggered; only mode-setting and CI-V read/write.

Requires: pyserial only, and this script installs it automatically on
first run if it's missing (no manual "pip install" step needed).
Hamlib is NOT needed.

After running, this script writes its full output to a timestamped log
file on your Desktop automatically -- just send that file back, no
copy/paste needed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def _pip_install_pyserial() -> bool:
    """Try a plain install first, then fall back to --user (some
    virtualenvs reject --user; some locked-down system installs need it)."""
    for extra_args in ([], ["--user"]):
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *extra_args, "pyserial"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        print(result.stdout)
        print(result.stderr)
    return False


def _import_serial():  # noqa: ANN202
    """Import pyserial, installing it automatically on first run if needed
    (this script is meant to be handed to someone who may not have it)."""
    try:
        import serial  # noqa: PLC0415

        return serial
    except ImportError:
        print("pyserial not found -- installing it automatically (pip install pyserial)...")
        if not _pip_install_pyserial():
            print("ERROR: automatic install of pyserial failed (see output above).")
            print("Please install it manually: pip install pyserial")
            sys.exit(1)
        print("pyserial installed successfully.")
        try:
            import serial  # noqa: PLC0415

            return serial
        except ImportError as exc:
            print(f"ERROR: pyserial still not importable after install: {exc}")
            sys.exit(1)


serial = _import_serial()

DEFAULT_PORT = "COM16" if sys.platform == "win32" else "/dev/ttyUSB0"
PORT = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
CIV_ADDR = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0xA2

DL_HZ = 435_610_000  # RS-44 downlink (Main)
UL_HZ = 145_935_000  # RS-44 uplink (Sub)

CTRL_ID = 0xE0
FE = 0xFE
FD = 0xFD

# CI-V command bytes (rigs/icom/icom_defs.h, Hamlib 4.7)
C_RD_MODE = 0x04
C_SET_FREQ = 0x05
C_SET_MODE = 0x06
C_SET_VFO = 0x07
C_CTL_FUNC = 0x16
C_CTL_MEM = 0x1A
C_SEND_SEL_MODE = 0x26

# CI-V sub-command / data bytes
S_LSB = 0x00
S_MAIN = 0xD0
S_SUB = 0xD1
S_FUNC_SATM = 0x5A  # sub-command of C_CTL_FUNC: satellite mode on/off
S_MEM_DATA_MODE = 0x06  # sub-command of C_CTL_MEM: legacy data-mode flag

DEFAULT_FILTER = 0x01  # arbitrary filter slot (FIL1); does not affect the DATA flag bug

# ---------------------------------------------------------------------------
# Logging: print AND append to a timestamped file on the Desktop, so the
# reporter can just send the file back without copy/pasting the terminal.
# ---------------------------------------------------------------------------

_LOG_FILE = None


def _desktop_dir() -> Path:
    for candidate in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"):
        if candidate.is_dir():
            return candidate
    return Path.home()


def _open_log_file():  # noqa: ANN202
    global _LOG_FILE
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _desktop_dir() / f"ic9700_data_mode_test_{stamp}.log"
    try:
        _LOG_FILE = log_path.open("w", encoding="utf-8")
    except OSError as exc:
        print(f"(could not open log file {log_path}: {exc} -- continuing without it)")
        _LOG_FILE = None
        return None
    return log_path


def log(msg: str = "") -> None:
    print(msg)
    if _LOG_FILE is not None:
        _LOG_FILE.write(msg + "\n")
        _LOG_FILE.flush()


def section(title: str) -> None:
    log()
    log("=" * 78)
    log(title)
    log("=" * 78)


def marker(text: str) -> None:
    log(f"  >>> {text}")


# ---------------------------------------------------------------------------
# CI-V frame helpers
# ---------------------------------------------------------------------------


def freq_to_bcd(freq_hz: int) -> bytes:
    """Icom CI-V frequency encoding: 5 BCD bytes, LSB (10s of Hz) first."""
    digits = f"{freq_hz:010d}"[::-1]
    out = bytearray()
    for i in range(0, 10, 2):
        lo = int(digits[i])
        hi = int(digits[i + 1]) if i + 1 < 10 else 0
        out.append((hi << 4) | lo)
    return bytes(out)


def build_frame(cmd: int, *data: int) -> bytes:
    return bytes([FE, FE, CIV_ADDR, CTRL_ID, cmd, *data, FD])


def send_and_read(ser: serial.Serial, frame: bytes, read_len: int = 32) -> bytes:
    ser.reset_input_buffer()
    ser.write(frame)
    return bytes(ser.read(read_len))


def describe_reply(reply: bytes) -> str:
    if not reply:
        return "no response (timeout)"
    if reply[:2] != bytes([FE, FE]):
        return f"unexpected preamble: {reply.hex()}"
    if len(reply) >= 5 and reply[4] == 0xFA:
        return f"NAK (rig rejected command): {reply.hex()}"
    if len(reply) >= 5 and reply[4] == 0xFB:
        return f"ACK: {reply.hex()}"
    return f"reply: {reply.hex()}"


def select_vfo(ser: serial.Serial, sub_cmd: int, label: str) -> None:
    marker(f"Select VFO -- {label} (07 {sub_cmd:02X})")
    reply = send_and_read(ser, build_frame(C_SET_VFO, sub_cmd))
    log(f"      {describe_reply(reply)}")
    time.sleep(0.15)


def set_freq(ser: serial.Serial, freq_hz: int, label: str) -> None:
    marker(f"Set frequency -- {label} ({freq_hz} Hz)")
    reply = send_and_read(ser, build_frame(C_SET_FREQ, *freq_to_bcd(freq_hz)))
    log(f"      {describe_reply(reply)}")
    time.sleep(0.15)


def set_mode_legacy(ser: serial.Serial, mode_byte: int, label: str) -> None:
    marker(f"Set mode (base sideband only, no data flag) -- {label}")
    reply = send_and_read(ser, build_frame(C_SET_MODE, mode_byte, DEFAULT_FILTER))
    log(f"      {describe_reply(reply)}")
    time.sleep(0.15)


def read_mode_legacy(ser: serial.Serial, label: str) -> None:
    """C_RD_MODE (0x04): reply is FE FE E0 <addr> 04 <mode_byte> <filter> FD."""
    marker(f"Read mode (legacy 04) -- {label}")
    reply = send_and_read(ser, build_frame(C_RD_MODE))
    if len(reply) >= 7 and reply[4] == C_RD_MODE:
        log(
            f"      mode_byte=0x{reply[5]:02X} filter=0x{reply[6]:02X}  "
            f"(0x00=LSB 0x01=USB)  raw={reply.hex()}"
        )
    else:
        log(f"      {describe_reply(reply)}")


def read_data_mode_flag(ser: serial.Serial, label: str) -> None:
    """C_CTL_MEM/S_MEM_DATA_MODE with no data bytes = query current value.
    Reply: FE FE E0 <addr> 1A 06 <D0> [<D1>] FD -- D0 at index 6.
    0x00 = data mode OFF, 0x01/0x02/0x03 = data mode ON (this is exactly
    what Hamlib's icom_get_mode() checks -- see icom.c around line 3025)."""
    marker(f"Read DATA-mode flag (legacy 1A 06 query) -- {label}")
    reply = send_and_read(ser, build_frame(C_CTL_MEM, S_MEM_DATA_MODE))
    if len(reply) >= 8 and reply[4] == C_CTL_MEM and reply[5] == S_MEM_DATA_MODE:
        flag = reply[6]
        log(
            f"      data_mode_flag=0x{flag:02X}  "
            f"({'DATA ON' if flag else 'DATA OFF -- BUG: flag did not stick'})  "
            f"raw={reply.hex()}"
        )
    else:
        log(f"      {describe_reply(reply)}")


def set_data_mode_legacy(ser: serial.Serial, enable: bool, label: str) -> None:
    """The exact command Hamlib falls back to for Sub on Main/Sub+A/B rigs
    (icom.c ~line 2685): C_CTL_MEM, Sc=S_MEM_DATA_MODE, D0=1/0, D1=filter."""
    marker(f"Set DATA mode {'ON' if enable else 'OFF'} (legacy 1A 06) -- {label}")
    data_byte = 0x01 if enable else 0x00
    reply = send_and_read(ser, build_frame(C_CTL_MEM, S_MEM_DATA_MODE, data_byte, DEFAULT_FILTER))
    log(f"      {describe_reply(reply)}")
    time.sleep(0.15)


def set_satmode(ser: serial.Serial, enable: bool) -> None:
    marker(f"Satellite mode {'ON' if enable else 'OFF'} (16 5A)")
    reply = send_and_read(ser, build_frame(C_CTL_FUNC, S_FUNC_SATM, 0x01 if enable else 0x00))
    log(f"      {describe_reply(reply)}")
    time.sleep(0.3)


def set_mode_fast_x26(
    ser: serial.Serial, vfo_number: int, mode_byte: int, enable_data: bool, label: str
) -> None:
    """The fast/combined command Hamlib normally reserves for
    targetable-mode rigs -- and explicitly REFUSES to use for Sub on
    Main/Sub+A/B rigs like the IC-9700 (icom.c: force_vfo_swap -> the
    x26 attempt is skipped and returns -RIG_ENAVAIL before ever being
    sent). This sends it anyway, straight to the wire, to see whether
    the rig itself would actually have accepted it on Sub.
    vfo_number: 0x00 = currently selected VFO, 0x01 = the OTHER (unselected) VFO."""
    marker(f"Set mode+DATA via fast command (26 {vfo_number:02X}) -- {label}")
    data_byte = 0x01 if enable_data else 0x00
    reply = send_and_read(
        ser,
        build_frame(C_SEND_SEL_MODE, vfo_number, mode_byte, data_byte, DEFAULT_FILTER),
    )
    log(f"      {describe_reply(reply)}")
    time.sleep(0.15)


def read_mode_fast_x26(ser: serial.Serial, vfo_number: int, label: str) -> None:
    """Reply: FE FE E0 <addr> 26 <vfo_number> <mode_byte> <data_byte> <filter> FD."""
    marker(f"Read mode+DATA via fast command (26 {vfo_number:02X}) -- {label}")
    reply = send_and_read(ser, build_frame(C_SEND_SEL_MODE, vfo_number))
    if len(reply) >= 9 and reply[4] == C_SEND_SEL_MODE:
        mode_byte, data_byte, filt = reply[6], reply[7], reply[8]
        log(
            f"      mode_byte=0x{mode_byte:02X} data_byte=0x{data_byte:02X} "
            f"filter=0x{filt:02X}  "
            f"({'DATA ON' if data_byte else 'DATA OFF'})  raw={reply.hex()}"
        )
    else:
        log(f"      {describe_reply(reply)}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test1(ser: serial.Serial) -> None:
    section("TEST 1: legacy DATA-mode command, minimal prior state")
    set_satmode(ser, True)
    select_vfo(ser, S_SUB, "Sub")
    read_data_mode_flag(ser, "before")
    set_data_mode_legacy(ser, True, "Sub")
    read_data_mode_flag(ser, "after")
    read_mode_legacy(ser, "Sub, after")
    select_vfo(ser, S_MAIN, "Main (restore)")


def test2(ser: serial.Serial) -> None:
    section("TEST 2: legacy DATA-mode command, full realistic app sequence")
    set_satmode(ser, True)
    select_vfo(ser, S_MAIN, "Main")
    set_freq(ser, DL_HZ, "Main/DL")
    select_vfo(ser, S_SUB, "Sub")
    set_freq(ser, UL_HZ, "Sub/UL")
    set_mode_legacy(ser, S_LSB, "Sub base sideband = LSB")
    read_mode_legacy(ser, "Sub, base sideband only")
    read_data_mode_flag(ser, "Sub, before DATA-ON")
    set_data_mode_legacy(ser, True, "Sub")
    read_mode_legacy(ser, "Sub, after DATA-ON")
    read_data_mode_flag(ser, "Sub, after DATA-ON")
    select_vfo(ser, S_MAIN, "Main (restore)")


def test3(ser: serial.Serial) -> None:
    section("TEST 3: fast/combined 0x26 command aimed directly at Sub")
    log(
        "  (Hamlib normally refuses this path for Sub on this rig family --"
        " this test bypasses that refusal to see what the rig itself does.)"
    )
    set_satmode(ser, True)
    # Leave Main selected (current_vfo = Main), so vfo_number=1 means "the
    # other VFO" = Sub, per icom_get_vfo_number_x25x26() in icom.c.
    select_vfo(ser, S_MAIN, "Main (keep selected)")
    set_freq(ser, DL_HZ, "Main/DL")
    read_mode_fast_x26(ser, 0x01, "Sub, before")
    set_mode_fast_x26(ser, 0x01, S_LSB, True, "Sub = LSB + DATA ON")
    read_mode_fast_x26(ser, 0x01, "Sub, after")


def main() -> None:
    log_path = _open_log_file()
    if log_path is not None:
        log(f"(writing full log to: {log_path})")

    log(f"Port={PORT}  Baud={BAUD}  CI-V addr=0x{CIV_ADDR:02X}  Model=IC-9700")
    log("No Hamlib is used anywhere in this script -- plain pyserial only.")
    log("No PTT/TX is triggered; only mode-setting and CI-V read/write.")

    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.5)
    except serial.SerialException as exc:
        log(f"FAILED TO OPEN PORT {PORT}: {exc}")
        if _LOG_FILE is not None:
            _LOG_FILE.close()
        sys.exit(1)

    try:
        test1(ser)
        test2(ser)
        test3(ser)
    finally:
        ser.close()

    section("DONE")
    log("Please send back the log file mentioned near the top of this output.")
    if log_path is not None:
        log(f"  -> {log_path}")
    if _LOG_FILE is not None:
        _LOG_FILE.close()


if __name__ == "__main__":
    main()
