"""PTT method selection and the serial control line used for RTS/DTR keying.

Each rig (Rig 1 / Rig 2) has its own PTT method, stored in that rig's settings
dict under ``ptt_method`` / ``ptt_port``:

* ``cat`` -- key through the rig's CAT/CI-V link (the historical behaviour and
  the default for every existing configuration).
* ``rts`` / ``dtr`` -- raise the RTS / DTR line of a serial port. Direct mode
  hands this to Hamlib (``ptt_type`` / ``ptt_pathname``), which also copes with
  the very common Icom setup where the PTT line is on the same USB serial port
  as CI-V. NET mode cannot configure rigctld, so the app drives the line itself
  with :class:`SerialPttLine` -- which needs a port rigctld is not using.
* ``vox`` -- the app sends nothing at all; the rig's own VOX keys off the audio.
"""

from __future__ import annotations

import fcntl
import logging
import struct
import subprocess
import termios
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

PTT_CAT = "cat"
PTT_RTS = "rts"
PTT_DTR = "dtr"
PTT_VOX = "vox"

PTT_METHODS: tuple[str, ...] = (PTT_CAT, PTT_RTS, PTT_DTR, PTT_VOX)
PTT_LINE_METHODS: frozenset[str] = frozenset({PTT_RTS, PTT_DTR})


def normalize_ptt_method(value: Any) -> str:
    """Return a valid PTT method; anything unknown (or missing) means CAT."""
    method = str(value or "").strip().lower()
    return method if method in PTT_METHODS else PTT_CAT


# TIOCMGET (read the modem control lines); not exported by the termios module on macOS.
_TIOCMGET = 0x4004746A
_MODEM_BITS: tuple[tuple[int, str], ...] = (
    (termios.TIOCM_DTR, "DTR"),
    (termios.TIOCM_RTS, "RTS"),
    (termios.TIOCM_CTS, "CTS"),
    (termios.TIOCM_DSR, "DSR"),
    (termios.TIOCM_CAR, "CD"),
    (termios.TIOCM_RNG, "RI"),
)


def _modem_lines(ser: Any) -> str:
    """Names of the modem control lines currently high on *ser* (diagnostics only)."""
    try:
        raw = fcntl.ioctl(ser.fileno(), _TIOCMGET, struct.pack("I", 0))
        value = struct.unpack("I", raw)[0]
        high = [name for bit, name in _MODEM_BITS if value & bit]
        return "+".join(high) if high else "none"
    except Exception as exc:
        return f"? ({exc})"


def _port_holders(port: str) -> str:
    """PIDs that have *port* open right now (diagnostics only; never raises)."""
    try:
        out = subprocess.run(
            ["lsof", "-t", port], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout.split()
        return ",".join(out) if out else "none"
    except Exception as exc:
        return f"? ({exc})"


class SerialPttLine:
    """Keys a transmitter through the RTS or DTR line of a serial port.

    The port is opened once (at rig connect) and held until :meth:`close`, with
    both control lines forced low at open, so keying never depends on an OS
    re-asserting the lines on every open. All methods are thread-safe and never
    raise: failures are logged and reported through the return value.
    """

    def __init__(self, port: str, line: str) -> None:
        if line not in PTT_LINE_METHODS:
            raise ValueError(f"PTT line must be 'rts' or 'dtr', got {line!r}")
        self._port = port
        self._line = line
        self._lock = threading.Lock()
        self._serial: Any = None

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._serial is not None

    def open(self, count_holders: bool = True) -> bool:
        """Open the port with RTS and DTR low. True when it is ready to key.

        Every open is logged with the modem lines seen right after it and, when
        *count_holders* is set, the PIDs holding the port (a PTT test or another
        program opening the same port shows up here) -- 2026-10-10 diagnostics for
        the FT-991A USB drops at PTT-on.
        """
        with self._lock:
            if self._serial is not None:
                return True
            try:
                import serial

                ser = serial.Serial()
                ser.port = self._port
                ser.rtscts = False
                ser.dsrdtr = False
                ser.xonxoff = False
                # Set before open() so the lines are never raised by the open.
                ser.rts = False
                ser.dtr = False
                t0 = time.monotonic()
                ser.open()
                self._serial = ser
                logger.info(
                    "PTT %s open %s: %.0f ms, lines=%s, holders=%s",
                    self._line.upper(),
                    self._port,
                    (time.monotonic() - t0) * 1000.0,
                    _modem_lines(ser),
                    _port_holders(self._port) if count_holders else "-",
                )
                return True
            except Exception as exc:
                logger.error("PTT %s: cannot open %s: %s", self._line.upper(), self._port, exc)
                return False

    def key(self, on: bool) -> bool:
        """Raise (``on``) or drop the PTT line. True when the line was set."""
        with self._lock:
            ser = self._serial
            if ser is None:
                return False
            try:
                if self._line == PTT_RTS:
                    ser.rts = on
                else:
                    ser.dtr = on
                logger.info(
                    "PTT %s %s: lines=%s",
                    self._line.upper(),
                    "on" if on else "off",
                    _modem_lines(ser),
                )
                return True
            except Exception as exc:
                logger.error("PTT %s %s failed: %s", self._line.upper(), "on" if on else "off", exc)
                return False

    def close(self) -> None:
        """Drop the line and release the port."""
        with self._lock:
            ser, self._serial = self._serial, None
        if ser is None:
            return
        try:
            if self._line == PTT_RTS:
                ser.rts = False
            else:
                ser.dtr = False
        except Exception as exc:
            logger.error("PTT %s: could not drop line on close: %s", self._line.upper(), exc)
        try:
            ser.close()
        except Exception as exc:
            logger.warning("PTT %s: close failed: %s", self._line.upper(), exc)
