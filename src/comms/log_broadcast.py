"""UDP ADIF log broadcaster for external logging bridges.

Broadcasts a single ADIF record (as plain UTF-8 text) via UDP whenever a QSO
is logged in FT4, Q65, or APRS. This targets lightweight log-relay tools
such as wavelog-gate and JT-Linker that listen for plain ADIF text on a UDP
port (default 2333) — it is not the WSJT-X binary UDP protocol used by
JTAlert/GridTracker.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 2333

LogBroadcastSettings = dict[str, int | bool | str]


def load_log_broadcast_settings(conn: sqlite3.Connection) -> LogBroadcastSettings:
    """Load UDP log broadcast preferences from app_settings.

    Returns a dict with keys: enabled (bool), host (str), port (int).
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'log_broadcast_settings'"
    ).fetchone()
    defaults: LogBroadcastSettings = {
        "enabled": False,
        "host": _DEFAULT_HOST,
        "port": _DEFAULT_PORT,
    }
    if row and row["value"]:
        try:
            stored: dict[str, int | bool | str] = json.loads(str(row["value"]))
            defaults.update(stored)
        except Exception:  # noqa: BLE001
            pass
    return defaults


def save_log_broadcast_settings(conn: sqlite3.Connection, settings: LogBroadcastSettings) -> None:
    """Persist UDP log broadcast preferences to app_settings."""
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at)"
        " VALUES ('log_broadcast_settings', ?, CURRENT_TIMESTAMP)",
        (json.dumps(settings),),
    )
    conn.commit()


class LogBroadcaster:
    """Sends logged QSOs as ADIF-over-UDP datagrams to a configurable host/port.

    A single instance is shared process-wide via get_log_broadcaster() so that
    the FT4, Q65, and APRS tabs all broadcast through the same socket and the
    same enabled/host/port state.
    """

    def __init__(self) -> None:
        self._enabled = False
        self._host = _DEFAULT_HOST
        self._port = _DEFAULT_PORT
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def reload_settings(self, conn: sqlite3.Connection) -> None:
        """Reload enabled/host/port from app_settings. Call after Settings are saved."""
        settings = load_log_broadcast_settings(conn)
        self._enabled = bool(settings["enabled"])
        self._host = str(settings["host"])
        self._port = int(settings["port"])

    def send_adif_record(self, adif_text: str) -> None:
        """Send *adif_text* (one ADIF record) as a UDP datagram.

        No-op when broadcasting is disabled. Send failures (e.g. nothing
        listening on the LAN) are swallowed since UDP is fire-and-forget and
        must never interrupt QSO logging.
        """
        if not self._enabled:
            return
        with contextlib.suppress(OSError):
            self._sock.sendto(adif_text.encode("utf-8"), (self._host, self._port))


_broadcaster: LogBroadcaster | None = None


def get_log_broadcaster() -> LogBroadcaster:
    """Return the process-wide LogBroadcaster singleton."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = LogBroadcaster()
    return _broadcaster
