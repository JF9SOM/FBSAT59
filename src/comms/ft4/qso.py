"""FT4 QSO state machine for satellite operations.

The QSO flow for a calling station is:
  IDLE → CALLING → EXCHANGE → CONFIRM → LOGGED

For a responding station (clicking a decoded CQ):
  IDLE → EXCHANGE → CONFIRM → LOGGED

The manager generates TX messages at each step and tracks RST values.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto

UTC = UTC


class QsoState(Enum):
    """FT4 QSO state."""

    IDLE = auto()
    CALLING = auto()  # sent CQ, waiting for response
    EXCHANGE = auto()  # exchanging callsigns and signal reports
    CONFIRM = auto()  # sent RR73, waiting for 73
    LOGGED = auto()  # QSO complete, awaiting user confirmation to log


@dataclass
class Ft4QsoSession:
    """Data accumulated during an active QSO."""

    their_call: str = ""
    their_grid: str = ""
    rst_sent: str = ""
    rst_rcvd: str = ""
    qso_start: datetime = field(default_factory=lambda: datetime.now(UTC))
    freq_hz: int = 0
    norad_cat_id: int | None = None
    sat_name: str = ""


class Ft4QsoManager:
    """State machine for a single FT4 QSO.

    Call start_cq() or respond_to() to enter an active QSO.
    Feed each decoded message to advance() — it returns the next TX string
    when a state transition occurs.
    Call log_qso() after the QSO is confirmed to write to the database.
    """

    def __init__(self, my_call: str, my_grid: str) -> None:
        self._my_call: str = my_call.upper().strip()
        self._my_grid: str = my_grid.upper().strip()[:4]
        self._state: QsoState = QsoState.IDLE
        self._session: Ft4QsoSession = Ft4QsoSession()
        self._pending_tx: str = ""

    # ------------------------------------------------------------------ #
    # Properties                                                           #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> QsoState:
        return self._state

    @property
    def session(self) -> Ft4QsoSession:
        return self._session

    @property
    def pending_tx(self) -> str:
        """Current TX message to be sent on the next TX slot."""
        return self._pending_tx

    @pending_tx.setter
    def pending_tx(self, value: str) -> None:
        self._pending_tx = value.upper()[:22]

    # ------------------------------------------------------------------ #
    # State entry points                                                   #
    # ------------------------------------------------------------------ #

    def start_cq(self) -> str:
        """Send CQ — transitions to CALLING. Returns the TX message."""
        self._state = QsoState.CALLING
        self._session = Ft4QsoSession()
        msg = f"CQ {self._my_call} {self._my_grid}"
        self._pending_tx = msg
        return msg

    def respond_to(self, their_call: str, their_grid: str = "") -> str:
        """Respond to a specific station — transitions to EXCHANGE.

        Returns the TX message "<THEIR_CALL> <MY_CALL> -05" as a starting report.
        """
        self._state = QsoState.EXCHANGE
        self._session = Ft4QsoSession()
        self._session.their_call = their_call.upper().strip()
        self._session.their_grid = their_grid.upper().strip()
        self._session.qso_start = datetime.now(UTC)
        msg = f"{self._session.their_call} {self._my_call} -05"
        self._pending_tx = msg
        self._session.rst_sent = "-05"
        return msg

    # ------------------------------------------------------------------ #
    # State machine                                                        #
    # ------------------------------------------------------------------ #

    def advance(self, decoded_text: str, their_snr: float | None = None) -> str | None:
        """Process a decoded message and advance state if it matches the QSO.

        Returns the next TX message string if a transition occurred, else None.
        """
        words = decoded_text.upper().split()
        if len(words) < 2:
            return None

        if self._state == QsoState.CALLING:
            # Looking for: <THEIR_CALL> <MY_CALL> <RST>
            if len(words) >= 3 and words[1] == self._my_call:
                their_call = words[0]
                self._session.their_call = their_call
                self._session.rst_rcvd = words[2]
                self._session.qso_start = datetime.now(UTC)
                report = f"{their_snr:+.0f}" if their_snr is not None else "-05"
                msg = f"{their_call} {self._my_call} R{report}"
                self._pending_tx = msg
                self._session.rst_sent = report
                self._state = QsoState.EXCHANGE
                return msg

        elif self._state == QsoState.EXCHANGE:
            # Looking for: <THEIR_CALL> <MY_CALL> R<RST>  (confirmation)
            target = self._session.their_call
            if (
                target
                and len(words) >= 3
                and words[0] == target
                and words[1] == self._my_call
                and words[2].startswith("R")
            ):
                self._session.rst_rcvd = words[2][1:]  # strip leading R
                msg = f"{target} {self._my_call} RR73"
                self._pending_tx = msg
                self._state = QsoState.CONFIRM
                return msg

        elif self._state == QsoState.CONFIRM and "73" in words:
            # Looking for: 73  or  <THEIR_CALL> <MY_CALL> 73
            self._state = QsoState.LOGGED
            self._pending_tx = ""
            return None

        return None

    def set_tx_override(self, message: str) -> None:
        """Override the pending TX message without changing state."""
        self._pending_tx = message.upper()[:22]

    def clear(self) -> None:
        """Reset to IDLE, clearing all QSO data."""
        self._state = QsoState.IDLE
        self._session = Ft4QsoSession()
        self._pending_tx = ""

    # ------------------------------------------------------------------ #
    # QSO logging                                                          #
    # ------------------------------------------------------------------ #

    def ensure_table(self, conn: sqlite3.Connection) -> None:
        """Create ft4_log table if it does not exist."""
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ft4_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                qso_date      TEXT    NOT NULL,
                time_on       TEXT    NOT NULL,
                time_off      TEXT,
                call          TEXT    NOT NULL,
                gridsquare    TEXT,
                rst_sent      TEXT,
                rst_rcvd      TEXT,
                freq_hz       INTEGER,
                norad_cat_id  INTEGER,
                sat_name      TEXT
            )"""
        )
        conn.commit()

    def log_qso(self, conn: sqlite3.Connection) -> None:
        """Write the current QSO to ft4_log. Call after state == LOGGED."""
        if not self._session.their_call:
            return
        now = datetime.now(UTC)
        self.ensure_table(conn)
        qso_date = self._session.qso_start.strftime("%Y%m%d")
        time_on = self._session.qso_start.strftime("%H%M%S")
        time_off = now.strftime("%H%M%S")
        conn.execute(
            """INSERT INTO ft4_log
               (qso_date, time_on, time_off, call, gridsquare,
                rst_sent, rst_rcvd, freq_hz, norad_cat_id, sat_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                qso_date,
                time_on,
                time_off,
                self._session.their_call,
                self._session.their_grid,
                self._session.rst_sent,
                self._session.rst_rcvd,
                self._session.freq_hz,
                self._session.norad_cat_id,
                self._session.sat_name,
            ),
        )
        conn.commit()
        self._broadcast_adif(conn, qso_date, time_on, time_off)

    def _broadcast_adif(
        self, conn: sqlite3.Connection, qso_date: str, time_on: str, time_off: str
    ) -> None:
        """Send this QSO to the UDP log broadcaster (wavelog-gate / JT-Linker etc.)."""
        from comms.log_broadcast import get_log_broadcaster  # noqa: PLC0415
        from ui.adif_utils import build_adif_record  # noqa: PLC0415

        freq_mhz = f"{self._session.freq_hz / 1e6:.6f}" if self._session.freq_hz else ""
        record = build_adif_record(
            {
                "CALL": self._session.their_call,
                "QSO_DATE": qso_date,
                "TIME_ON": time_on,
                "TIME_OFF": time_off,
                "MODE": "FT4",
                "PROP_MODE": "SAT",
                "FREQ": freq_mhz,
                "SAT_NAME": self._session.sat_name,
                "RST_SENT": self._session.rst_sent,
                "RST_RCVD": self._session.rst_rcvd,
                "GRIDSQUARE": self._session.their_grid,
            }
        )
        broadcaster = get_log_broadcaster()
        broadcaster.reload_settings(conn)
        broadcaster.send_adif_record(record)
