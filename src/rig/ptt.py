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

import logging
import subprocess
import sys
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


def _modem_lines(ser: Any) -> str:
    """Names of the modem control lines currently high on *ser* (diagnostics only)."""
    if sys.platform == "win32":
        return "n/a"
    try:
        # fcntl/termios are Unix-only; import lazily so Windows can import this module.
        import fcntl
        import struct
        import termios

        bits = (
            (termios.TIOCM_DTR, "DTR"),
            (termios.TIOCM_RTS, "RTS"),
            (termios.TIOCM_CTS, "CTS"),
            (termios.TIOCM_DSR, "DSR"),
            (termios.TIOCM_CAR, "CD"),
            (termios.TIOCM_RNG, "RI"),
        )
        raw = fcntl.ioctl(ser.fileno(), _TIOCMGET, struct.pack("I", 0))
        value = struct.unpack("I", raw)[0]
        high = [name for bit, name in bits if value & bit]
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


PORT_FREE = "free"
PORT_BUSY = "busy"
PORT_MISSING = "missing"
PORT_UNKNOWN = "unknown"


def classify_port_open_error(exc: BaseException) -> str:
    """Map a pyserial open() failure to PORT_BUSY / PORT_MISSING / PORT_UNKNOWN.

    pyserial wraps the OS error in a SerialException whose text carries the
    original class (Windows: PermissionError(13, 'Access is denied'); POSIX:
    'Resource busy' / 'Permission denied'), so the text is matched too.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "filenotfound" in text or "no such file" in text or "cannot find the file" in text:
        return PORT_MISSING
    if any(k in text for k in ("permissionerror", "access is denied", "resource busy", "busy")):
        return PORT_BUSY
    return PORT_UNKNOWN


def probe_serial_port(port: str) -> str:
    """Try to open and immediately close *port*; PORT_FREE when it opened.

    Never raises. Used only after a rig connect failed and the rig's own
    handle is closed, to tell "another program holds this port" apart from
    other causes (Hamlib reports every open failure as the same error -5).
    """
    try:
        import serial

        ser = serial.Serial()
        ser.port = port
        ser.rts = False
        ser.dtr = False
        ser.open()
        ser.close()
        return PORT_FREE
    except Exception as exc:
        return classify_port_open_error(exc)


def busy_port_hint(ports: list[str]) -> str:
    """Human-readable warning naming the ports another program holds, or "".

    Probes each distinct non-empty port in *ports* and logs every result so
    the log shows which port was free, busy or missing at the failure.
    """
    busy: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for port in ports:
        if not port or port in seen:
            continue
        seen.add(port)
        status = probe_serial_port(port)
        logger.info("Serial port probe after connect failure: %s -> %s", port, status)
        if status == PORT_BUSY:
            busy.append(port)
        elif status == PORT_MISSING:
            missing.append(port)
    parts: list[str] = []
    if busy:
        parts.append(
            f"{', '.join(busy)} is in use by another program. "
            "Close any other software using it (loggers, CI-V tools, WSJT-X, "
            "the radio maker's software), then connect again."
        )
    if missing:
        parts.append(f"{', '.join(missing)} was not found. Check the port in Rig Settings.")
    return " ".join(parts)
