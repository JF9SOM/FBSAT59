"""FT4 QSO state machine for satellite operations.

States are named for what we last SENT, so each one says exactly what we
are waiting to hear next:

  Calling CQ:
    IDLE -CQ-> CALLING -their grid-> EXCHANGE -their R+rpt-> CONFIRM
      -their 73-> LOGGED
    (if they skip the grid and answer with a report straight away,
     CALLING goes to RREPORT_SENT instead)

  Answering someone, grid first (the standard exchange, MyGrid button):
    IDLE -my grid-> GRID_SENT -their rpt-> RREPORT_SENT -their RR73->
      LOGGED

  Answering someone, report first (RST button -- common on satellites,
  where passes are short):
    IDLE -my report-> EXCHANGE -their R+rpt-> CONFIRM -their 73-> LOGGED

The manager generates TX messages at each step and tracks RST values.
Signal reports are always the measured SNR of the message we are
answering -- never a fixed placeholder (GitHub Issue #16).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto

UTC = UTC

# 4-character Maidenhead grid, as carried in a standard FT4 message.
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")
# Signal report: "-12", "+03". R-prefixed variants are matched separately.
_REPORT_RE = re.compile(r"^[+-][0-9]{1,2}$")
_R_REPORT_RE = re.compile(r"^R[+-][0-9]{1,2}$")
# Acknowledgements that end an exchange.
_ROGER_WORDS = frozenset({"RR73", "RRR", "RR"})


def format_report(snr_db: float) -> str:
    """Format a measured SNR as an FT4 signal report ("-12", "+03")."""
    return f"{int(round(snr_db)):+03d}"


class QsoState(Enum):
    """FT4 QSO state -- named for the message we last sent."""

    IDLE = auto()
    CALLING = auto()  # sent CQ, waiting for someone to answer
    GRID_SENT = auto()  # answered with our grid, waiting for their report
    EXCHANGE = auto()  # sent a plain report, waiting for their R-report
    RREPORT_SENT = auto()  # sent an R-report, waiting for their RR73/73
    CONFIRM = auto()  # sent RR73, waiting for 73
    LOGGED = auto()  # QSO complete, awaiting user confirmation to log


@dataclass
class Ft4QsoSession:
    """Data accumulated during an active QSO."""

    their_call: str = ""
    their_grid: str = ""
    rst_sent: str = ""
    rst_rcvd: str = ""
    # SNR we measured on their signal, carried from the decode we answered
    # so the report we send is the real thing rather than a placeholder.
    their_snr_db: float | None = None
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

    def set_state(self, state: QsoState) -> None:
        """Force the state, for the TX quick buttons.

        Those let the operator jump straight to a message out of the normal
        order; the state has to follow or the next decode would be judged
        against a step we already skipped past.
        """
        self._state = state

    def _begin_answer(self, their_call: str, their_grid: str, their_snr_db: float | None) -> None:
        """Shared setup for both ways of answering a station."""
        self._session = Ft4QsoSession()
        self._session.their_call = their_call.upper().strip()
        self._session.their_grid = their_grid.upper().strip()
        self._session.their_snr_db = their_snr_db
        self._session.qso_start = datetime.now(UTC)

    def respond_with_grid(
        self, their_call: str, their_grid: str = "", their_snr_db: float | None = None
    ) -> str:
        """Answer a station with our grid — the standard opening exchange.

        Transitions to GRID_SENT. Returns "<THEIR_CALL> <MY_CALL> <MY_GRID>".
        """
        self._begin_answer(their_call, their_grid, their_snr_db)
        self._state = QsoState.GRID_SENT
        msg = f"{self._session.their_call} {self._my_call} {self._my_grid}"
        self._pending_tx = msg
        return msg

    def respond_with_report(
        self, their_call: str, their_grid: str = "", their_snr_db: float | None = None
    ) -> str:
        """Answer a station with a signal report, skipping the grid step.

        Common on satellites, where a pass leaves little time for the full
        exchange. Transitions to EXCHANGE (we have sent a plain report and
        are waiting for their R-report). Returns
        "<THEIR_CALL> <MY_CALL> <REPORT>".
        """
        self._begin_answer(their_call, their_grid, their_snr_db)
        self._state = QsoState.EXCHANGE
        report = format_report(their_snr_db) if their_snr_db is not None else "-05"
        msg = f"{self._session.their_call} {self._my_call} {report}"
        self._pending_tx = msg
        self._session.rst_sent = report
        return msg

    # ------------------------------------------------------------------ #
    # State machine                                                        #
    # ------------------------------------------------------------------ #

    def advance(
        self,
        decoded_text: str,
        their_snr: float | None = None,
        *,
        allow_auto_start: bool = False,
    ) -> str | None:
        """Process a decoded message and advance state if it matches the QSO.

        Returns the next TX message string if a transition occurred, else
        None. A None return means the message was not for us, or was not the
        one this state is waiting for, and nothing changed.

        Args:
            decoded_text: One decoded FT4 message.
            their_snr: SNR we measured on it, used for the report we send back.
            allow_auto_start: Let an incoming call pull us out of IDLE into a
                QSO. Off by default so monitoring never starts transmitting
                by itself; the FT4 tab turns it on only when the operator has
                selected auto-progress (GitHub Issue #16).
        """
        # FT4 directed messages are "<TO> <FROM> <payload>"; anything shorter
        # (a bare "73", a CQ) carries nothing this state machine acts on.
        words = decoded_text.upper().split()
        if len(words) < 3:
            # A bare acknowledgement still closes out a finished exchange.
            closing = self._state in (QsoState.CONFIRM, QsoState.RREPORT_SENT)
            if closing and ("73" in words or _ROGER_WORDS & set(words)):
                return self._finish()
            return None

        to_call, from_call, payload = words[0], words[1], words[2]

        if self._state in (QsoState.IDLE, QsoState.CALLING):
            if self._state == QsoState.IDLE and not allow_auto_start:
                return None
            # Waiting for someone to come back to us: "<MY> <THEIR> <...>".
            if to_call != self._my_call:
                return None
            return self._on_answer_to_our_call(from_call, payload, their_snr)

        # Every remaining state is mid-QSO with one specific station, so the
        # message must be from them and addressed to us.
        target = self._session.their_call
        if not target or to_call != self._my_call or from_call != target:
            return None

        if self._state == QsoState.GRID_SENT:
            # We sent our grid; they should now report us.
            if _REPORT_RE.match(payload):
                self._session.rst_rcvd = payload
                return self._send_r_report(target, their_snr)
            if _R_REPORT_RE.match(payload):
                # They jumped ahead to an R-report -- accept and confirm.
                self._session.rst_rcvd = payload[1:]
                return self._send_rr73(target)
            if payload in _ROGER_WORDS or payload == "73":
                return self._finish()

        elif self._state == QsoState.EXCHANGE:
            # We sent a plain report; they should confirm with an R-report.
            if _R_REPORT_RE.match(payload):
                self._session.rst_rcvd = payload[1:]
                return self._send_rr73(target)
            if payload in _ROGER_WORDS or payload == "73":
                return self._finish()

        elif self._state == QsoState.RREPORT_SENT:
            # We sent an R-report; their RR73 (or 73) ends it.
            if payload in _ROGER_WORDS:
                msg = f"{target} {self._my_call} 73"
                self._pending_tx = msg
                self._state = QsoState.LOGGED
                return msg
            if payload == "73":
                return self._finish()

        elif self._state == QsoState.CONFIRM:
            if payload == "73" or payload in _ROGER_WORDS:
                return self._finish()

        return None

    # -- advance() helpers --

    def _on_answer_to_our_call(
        self, from_call: str, payload: str, their_snr: float | None
    ) -> str | None:
        """Someone answered our CQ (or called us out of the blue)."""
        if GRID_RE.match(payload):
            self._begin_answer(from_call, payload, their_snr)
            self._state = QsoState.EXCHANGE
            report = format_report(their_snr) if their_snr is not None else "-05"
            msg = f"{from_call} {self._my_call} {report}"
            self._pending_tx = msg
            self._session.rst_sent = report
            return msg
        if _REPORT_RE.match(payload):
            # They skipped the grid and reported us straight away.
            self._begin_answer(from_call, "", their_snr)
            self._session.rst_rcvd = payload
            return self._send_r_report(from_call, their_snr)
        return None

    def _send_r_report(self, target: str, their_snr: float | None) -> str:
        report = format_report(their_snr) if their_snr is not None else self._session.rst_sent
        if not report:
            report = "-05"
        self._session.rst_sent = report
        msg = f"{target} {self._my_call} R{report}"
        self._pending_tx = msg
        self._state = QsoState.RREPORT_SENT
        return msg

    def _send_rr73(self, target: str) -> str:
        msg = f"{target} {self._my_call} RR73"
        self._pending_tx = msg
        self._state = QsoState.CONFIRM
        return msg

    def _finish(self) -> str | None:
        """Exchange complete -- nothing left to send.

        Returns None like any other non-transition, but moves to LOGGED so
        the tab can stop transmitting and offer the QSO for logging.
        """
        self._state = QsoState.LOGGED
        self._pending_tx = ""
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
