"""Receive-only APRS-IS client for watching satellite APRS traffic reported by other stations.

APRS-IS is a plain TCP text protocol: after the server banner the client sends one login
line (``user CALL pass -1 vers NAME VER filter ...``) and the server then streams the packets
that match the filter, one per line; lines starting with ``#`` are server comments. A
passcode of ``-1`` is read-only, so no passcode is needed and nothing is ever uploaded.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# rotate.aprs2.net:14580 is the public "user-defined filter" port of the APRS-IS core servers.
DEFAULT_SERVER = "rotate.aprs2.net"
DEFAULT_PORT = 14580
SOFTWARE_NAME = "FBSAT59"

# Satellite packets are rare, so silence is normal. While nothing arrives we send a keepalive
# comment line every _RECV_TIMEOUT_S seconds; the link is declared dead only after
# _IDLE_LIMIT_S seconds without a single byte from the server (servers send a comment line
# about every 20 s, so that never happens on a healthy connection).
_RECV_TIMEOUT_S = 30.0
_IDLE_LIMIT_S = 180.0

# Satellite name -> the callsigns it appears under in APRS-IS (as a digipeater in the path, or
# as the source of its own beacons). ``ARISS`` is the generic alias the ISS digipeater answers to.
SATELLITE_CALLSIGNS: dict[str, tuple[str, ...]] = {
    "ISS": ("RS0ISS", "ARISS"),
    "SONATE-2": ("DP0SNX",),
    "LASARsat": ("OK0LSR",),
    "KOSTKA": ("OK0KST",),
    "GRBBeta": ("HA2GRB",),
    "MARINA": ("OM9MAR",),
    "CroCube": ("9A0CC",),
    "BOTAN": ("JS1YPT",),
    "SAKURA": ("JS1YNI",),
    "PCSAT": ("W3ADO-1",),
    "EOS-7": ("A55BTN",),
}


@dataclass(frozen=True)
class IsPacket:
    """One APRS-IS packet split into its parts."""

    source: str
    dest: str
    path: tuple[str, ...]  # digipeaters and the q-construct, in order
    info: str
    raw: str


def parse_is_line(line: str) -> IsPacket | None:
    """Split ``SRC>DEST,PATH...:info`` into an :class:`IsPacket`; None for comments/garbage."""
    line = line.strip()
    if not line or line.startswith("#") or ":" not in line:
        return None
    header, info = line.split(":", 1)
    if ">" not in header:
        return None
    source, rest = header.split(">", 1)
    parts = rest.split(",")
    if not source or not parts[0]:
        return None
    return IsPacket(source, parts[0], tuple(p for p in parts[1:] if p), info, line)


def _base(call: str) -> str:
    """Callsign without the has-been-repeated ``*``, upper-cased."""
    return call.rstrip("*").upper()


def match_satellite(packet: IsPacket, names: list[str] | None = None) -> str | None:
    """Name of the satellite *packet* came through (or from), or None.

    A satellite matches when one of its callsigns is the packet source or is a
    digipeater in the path. *names* limits the search (default: every known satellite).
    """
    seen = {_base(packet.source), *(_base(p) for p in packet.path)}
    for name in names if names is not None else list(SATELLITE_CALLSIGNS):
        if any(call.upper() in seen for call in SATELLITE_CALLSIGNS.get(name, ())):
            return name
    return None


def build_filter(names: list[str], extra: str = "") -> str:
    """APRS-IS filter for the satellites in *names* plus an optional raw *extra* filter.

    ``d/`` matches packets digipeated by a satellite, ``b/`` those the satellite sent itself.
    """
    calls = [c for n in names for c in SATELLITE_CALLSIGNS.get(n, ())]
    parts: list[str] = []
    if calls:
        parts.append("d/" + "/".join(calls))
        parts.append("b/" + "/".join(calls))
    if extra.strip():
        parts.append(extra.strip())
    return " ".join(parts)


class AprsIsClient(QObject):
    """Connects to an APRS-IS server in a background thread and emits received lines.

    Signals (emitted from the worker thread; Qt queues them to the GUI thread):
        line_received(str): one packet line (server comments are not forwarded)
        status_changed(str): "connecting" / "connected" / "disconnected" / "error: ..."
    """

    line_received: Signal = Signal(str)
    status_changed: Signal = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        """True while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, host: str, port: int, login_call: str, filter_text: str) -> None:
        """Connect and start streaming. A running connection is stopped first."""
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(host, port, login_call, filter_text, self._stop),
            daemon=True,
            name="aprs-is",
        )
        self._thread.start()

    def update_filter(self, filter_text: str) -> None:
        """Change the filter of a running session (APRS-IS ``#filter`` command)."""
        with self._lock:
            sock = self._sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.sendall(f"#filter {filter_text}\r\n".encode("ascii", errors="replace"))

    def stop(self) -> None:
        """Disconnect and wait briefly for the worker thread."""
        self._stop.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(
        self, host: str, port: int, login_call: str, filter_text: str, stop: threading.Event
    ) -> None:
        self.status_changed.emit("connecting")
        try:
            sock = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            logger.warning("APRS-IS connect to %s:%d failed: %s", host, port, exc)
            self.status_changed.emit(f"error: {exc}")
            return
        with self._lock:
            self._sock = sock
        try:
            sock.settimeout(_RECV_TIMEOUT_S)
            login = f"user {login_call} pass -1 vers {SOFTWARE_NAME} 1.0"
            if filter_text.strip():
                login += f" filter {filter_text.strip()}"
            sock.sendall((login + "\r\n").encode("ascii", errors="replace"))
            self.status_changed.emit("connected")
            buf = b""
            idle = 0.0
            while not stop.is_set():
                try:
                    data = sock.recv(4096)
                except TimeoutError:
                    idle += _RECV_TIMEOUT_S
                    if idle >= _IDLE_LIMIT_S:
                        raise
                    sock.sendall(b"#\r\n")  # keepalive; also fails fast on a dead link
                    continue
                if not data:
                    break
                idle = 0.0
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("ascii", errors="replace").strip()
                    if text and not text.startswith("#"):
                        self.line_received.emit(text)
        except OSError as exc:
            if not stop.is_set():
                logger.warning("APRS-IS %s:%d: %s", host, port, exc)
                self.status_changed.emit(f"error: {exc}")
                return
        finally:
            with self._lock:
                self._sock = None
            with contextlib.suppress(OSError):
                sock.close()
        self.status_changed.emit("disconnected")
