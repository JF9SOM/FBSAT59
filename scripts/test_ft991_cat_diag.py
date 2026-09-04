#!/usr/bin/env python3
"""FT-991A CAT diagnostic — single persistent connection, raw byte display.

Usage:
    python3 scripts/test_ft991_cat_diag.py [PORT [BAUD]]

Defaults: PORT=/dev/ttyUSB0  BAUD=38400
"""

import sys
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 38400


def txrx(ser: serial.Serial, cmd: bytes, label: str = "", delay: float = 0.15) -> bytes:
    ser.reset_input_buffer()
    ser.write(cmd)
    time.sleep(delay)
    resp = ser.read(ser.in_waiting or 1)
    extra = b""
    if resp:
        time.sleep(0.05)
        extra = ser.read(ser.in_waiting)
    resp += extra
    print(f"  {label or cmd!r:30s}  →  {resp!r}")
    return resp


print(f"Opening {PORT} at {BAUD} baud (single session, no DTR toggle between commands)")
print()

with serial.Serial(
    PORT,
    BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_TWO,  # FT-991A default: 2 stop bits
    timeout=0.3,
    rtscts=False,
    dsrdtr=False,
) as ser:
    time.sleep(0.2)

    print("── Current state ─────────────────────────────────────────────")
    txrx(ser, b"IF;", "IF; (full status)")
    txrx(ser, b"ST;", "ST; (split off=ST0, on=ST1)")
    txrx(ser, b"FA;", "FA; (VFO-A freq)")
    txrx(ser, b"FB;", "FB; (VFO-B freq)")
    txrx(ser, b"MD0;", "MD0; (VFO-A mode)")
    print()

    print("── Test: set VFO-A to 435.610 MHz (9-digit format) ───────────")
    txrx(ser, b"FA435610000;", "FA435610000; set")  # 9 digits, no leading 0
    txrx(ser, b"FA;", "FA; verify")
    print()

    print("── Test: set VFO-B to 145.935 MHz (9-digit format) ───────────")
    txrx(ser, b"FB145935000;", "FB145935000; set")  # 9 digits
    txrx(ser, b"FB;", "FB; verify")
    print()

    print("── Test: ST1; (split ON) ─────────────────────────────────────")
    txrx(ser, b"ST0;", "ST0; (ensure off first)")
    time.sleep(0.1)
    txrx(ser, b"ST;", "ST; confirm off")
    time.sleep(0.1)
    txrx(ser, b"ST1;", "ST1; (split ON)")
    time.sleep(0.2)
    txrx(ser, b"ST;", "ST; confirm on?")
    txrx(ser, b"IF;", "IF; full state after ST1")
    print()
    print("  >>> CHECK: SPLIT indicator on rig display? <<<")
    input("  Press Enter to continue...")
    print()

    print("── Test: FT mode — maybe ST needs VFO mode first ─────────────")
    txrx(ser, b"ST0;", "ST0; off")
    txrx(ser, b"VS0;", "VS0; select VFO A")  # some rigs need this
    time.sleep(0.1)
    txrx(ser, b"ST1;", "ST1; ON after VS0")
    time.sleep(0.2)
    txrx(ser, b"ST;", "ST; confirm")
    txrx(ser, b"IF;", "IF; full state")
    print()
    print("  >>> CHECK: SPLIT indicator? <<<")
    input("  Press Enter to continue...")
    print()

    print("── Test: toggle with FT command (some Yaesu rigs) ───────────")
    txrx(ser, b"ST0;", "ST0; off")
    time.sleep(0.1)
    txrx(ser, b"FT1;", "FT1; (TX to VFO-B, some Yaesu models)")
    time.sleep(0.2)
    txrx(ser, b"ST;", "ST; after FT1")
    txrx(ser, b"IF;", "IF; after FT1")
    print()
    print("  >>> CHECK: SPLIT indicator? <<<")
    input("  Press Enter to continue...")
    print()

    print("── Final: leave split ON + show full IF; state ───────────────")
    txrx(ser, b"ST1;", "ST1;")
    time.sleep(0.1)
    txrx(ser, b"IF;", "IF; final")
    txrx(ser, b"ST;", "ST; final")
    # Also try TX VFO select commands seen in some Yaesu rigs
    txrx(ser, b"TX1;", "TX1; (some Yaesu: set TX to sub-VFO)")
    txrx(ser, b"TX0;", "TX0; (TX to main-VFO)")
    txrx(ser, b"SPLIT1;", "SPLIT1; (check if this variant exists)")
    txrx(ser, b"IF;", "IF; after alternates")

print()
print("Done.")
