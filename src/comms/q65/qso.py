"""Q65 QSO state machine — manages EME QSO sequences.

State transitions:
  IDLE -> CALLING  (CQ pressed or decoded message clicked)
  CALLING -> EXCHANGE  (response heard with our callsign)
  EXCHANGE -> CONFIRM  (R+report received)
  CONFIRM -> LOGGED    (73 received or Log QSO pressed)
  any -> IDLE          (Halt pressed or error)

TX messages are generated automatically when tx_enable is True.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto


def format_report(snr_db: float) -> str:
    """Format a measured SNR as a signal report ("-12", "+03")."""
    return f"{int(round(snr_db)):+03d}"


class Q65QsoState(Enum):
    """QSO progress states."""

    IDLE = auto()
    CALLING = auto()  # CQ sent / waiting for response
    EXCHANGE = auto()  # Exchanging signal reports
    CONFIRM = auto()  # Sent RR73, waiting for 73
    LOGGED = auto()  # QSO complete


@dataclass
class Q65LogEntry:
    """One completed Q65 QSO entry."""

    qso_date: str  # YYYYMMDD UTC
    time_on: str  # HHMMSS UTC
    time_off: str  # HHMMSS UTC
    call: str  # remote callsign
    gridsquare: str
    rst_sent: str  # e.g. "-05"
    rst_rcvd: str
    freq_hz: int
    norad_cat_id: int | None
    sat_name: str


class Q65QsoManager:
    """Manages Q65 EME QSO state and generates outgoing messages.

    Args:
        conn:       Shared SQLite connection for logging.
        my_call:    Own callsign.
        my_grid:    Own Maidenhead grid locator.
        freq_hz:    Current operating frequency (Hz, for logging).
        on_tx_msg:  Callback invoked with the next TX message string.
        on_state:   Callback invoked whenever the state changes.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        my_call: str,
        my_grid: str,
        freq_hz: int = 0,
        on_tx_msg: Callable[[str], None] | None = None,
        on_state: Callable[[Q65QsoState, str], None] | None = None,
    ) -> None:
        self._conn = conn
        self.my_call = my_call.upper().strip()
        self.my_grid = my_grid.upper().strip()
        self.freq_hz = freq_hz
        self._on_tx_msg = on_tx_msg
        self._on_state = on_state

        self._state = Q65QsoState.IDLE
        self.dx_call: str = ""
        self.dx_grid: str = ""
        self.rst_sent: str = ""
        self.rst_rcvd: str = ""
        # SNR we last measured on a message from dx_call, so quick-report
        # buttons (RST/R+RST) send our real reading instead of a fixed
        # placeholder (GitHub Issue #16 -- same fix as the FT4 tab's).
        self.their_snr_db: float | None = None
        self._time_on: str = ""
        self._norad: int | None = None
        self._sat_name: str = ""

        self.tx_enable: bool = False
        self._pending_tx: str = ""  # message ready to send this period
        self._free_tx: str = ""  # one-shot free-text override

        self._ensure_table()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> Q65QsoState:
        return self._state

    def set_location(self, my_call: str, my_grid: str) -> None:
        """Update own callsign / grid (called when user edits the UI fields)."""
        self.my_call = my_call.upper().strip()
        self.my_grid = my_grid.upper().strip()

    def set_freq(self, freq_hz: int) -> None:
        """Update operating frequency for logging."""
        self.freq_hz = freq_hz

    def set_satellite(self, norad: int | None, name: str) -> None:
        """Set current satellite context for logging."""
        self._norad = norad
        self._sat_name = name

    def start_cq(self) -> None:
        """Start calling CQ (enters CALLING state)."""
        if not self.my_call or not self.my_grid:
            return
        self.dx_call = ""
        self.dx_grid = ""
        self.rst_sent = ""
        self.rst_rcvd = ""
        self._set_state(Q65QsoState.CALLING, f"CQ {self.my_call} {self.my_grid[:4]}")

    def call_station(
        self, dx_call: str, dx_grid: str = "", their_snr_db: float | None = None
    ) -> None:
        """Call a specific station (enters EXCHANGE state).

        their_snr_db is the SNR measured on the decoded row being answered
        (if any) -- the report we send reflects that measurement rather
        than a fixed placeholder (GitHub Issue #16).
        """
        self.dx_call = dx_call.upper().strip()
        self.dx_grid = dx_grid.upper().strip()
        self.their_snr_db = their_snr_db
        self._time_on = datetime.now(UTC).strftime("%H%M%S")
        report = format_report(their_snr_db) if their_snr_db is not None else "-05"
        self.rst_sent = report
        msg = f"{self.dx_call} {self.my_call} {report}"
        self._set_state(Q65QsoState.EXCHANGE, msg)

    def halt(self) -> None:
        """Abort TX immediately."""
        self.tx_enable = False
        self._pending_tx = ""
        self._set_state(Q65QsoState.IDLE, "")

    def send_free(self, text: str) -> None:
        """Send one free-text message on the next TX slot."""
        self._free_tx = text.upper().strip()
        self._pending_tx = self._free_tx

    def on_decoded(self, msg_text: str, snr: float) -> None:
        """Process a decoded message.  Updates state machine if relevant.

        Call from Q65Tab whenever a message is decoded that contains
        our callsign or signals a CQ.
        """
        text = msg_text.upper().strip()
        if not self.my_call:
            return

        if self.state == Q65QsoState.CALLING:
            # Directed messages are "<TO> <FROM> <payload>" (same packer as
            # FT4/FT8, see encoder.pack77()); someone answering our CQ
            # addresses it to us, so MY_CALL comes first, not DX_CALL
            # (GitHub Issue #16: this was backwards, so CALLING never
            # advanced on a correctly-addressed reply).
            m = re.match(
                r"^" + re.escape(self.my_call) + r"\s+([A-Z0-9/]+)\s+([-+]?\d+|[A-R]{2}\d{2})",
                text,
            )
            if m:
                self.dx_call = m.group(1)
                received = m.group(2)
                # The report we send back always reflects our own measured
                # SNR of their signal (`snr`) -- never an echo of whatever
                # they sent us (GitHub Issue #16: this previously echoed
                # their own report value back to them when they replied
                # with a report instead of a grid).
                report = format_report(snr)
                if re.match(r"[A-R]{2}\d{2}", received):
                    self.dx_grid = received
                else:
                    self.rst_rcvd = received
                self.rst_sent = report
                self.their_snr_db = snr
                self._time_on = datetime.now(UTC).strftime("%H%M%S")
                reply = f"{self.dx_call} {self.my_call} {report}"
                self._set_state(Q65QsoState.EXCHANGE, reply)
                return

        if self.state == Q65QsoState.EXCHANGE:
            # "<TO=MY_CALL> <FROM=DX_CALL> R-XX" (confirmed report) --
            # same TO/FROM order fix as the CALLING branch above.
            m = re.match(
                r"^"
                + re.escape(self.my_call)
                + r"\s+"
                + re.escape(self.dx_call)
                + r"\s+R([-+]?\d+)",
                text,
            )
            if m:
                self.rst_rcvd = m.group(1)
                reply = f"{self.dx_call} {self.my_call} RR73"
                self._set_state(Q65QsoState.CONFIRM, reply)
                return

        if self.state == Q65QsoState.CONFIRM and self.dx_call:
            # "<TO=MY_CALL> <FROM=DX_CALL> 73" -- was checking
            # text.startswith(self.dx_call), i.e. the TO/FROM order bug
            # again, and only a raw substring check for "73" (which a
            # report like "-73" would also satisfy). Word-matched here
            # instead of loosened further.
            words = text.split()
            if (
                len(words) >= 2
                and words[0] == self.my_call
                and words[1] == self.dx_call
                and "73" in words[2:]
            ):
                self._log_qso()
                self._set_state(Q65QsoState.LOGGED, "")

    def consume_tx_message(self) -> str | None:
        """Return the pending TX message and clear it.

        Called by Q65Tab at the start of each TX slot.  Returns None if
        there is nothing to send (tx_enable is False or state is IDLE).
        """
        if not self.tx_enable:
            return None
        if self._free_tx:
            msg = self._free_tx
            self._free_tx = ""
            self._pending_tx = ""
            return msg
        if self._pending_tx:
            return self._pending_tx
        return None

    def log_qso_manually(self) -> None:
        """Force-log the current QSO regardless of state."""
        self._log_qso()
        self._set_state(Q65QsoState.LOGGED, "")

    # ------------------------------------------------------------------
    # ADIF export
    # ------------------------------------------------------------------

    def export_adif(self, path: str) -> int:
        """Write all Q65 log entries to an ADIF file.  Returns count written."""
        cur = self._conn.execute(
            "SELECT qso_date,time_on,time_off,call,gridsquare,"
            "rst_sent,rst_rcvd,freq_hz,sat_name FROM q65_log ORDER BY qso_date,time_on"
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        def _field(tag: str, value: str) -> str:
            return f"<{tag}:{len(value)}>{value}"

        lines = ["<ADIF_VER:5>3.1.4", "<PROGRAMID:7>FBSAT59", "<EOH>", ""]
        for row in rows:
            qso_date, time_on, time_off, call, grid, rst_s, rst_r, freq_hz, sat = row
            freq_mhz = f"{freq_hz / 1e6:.6f}" if freq_hz else ""
            entry_parts = [
                _field("CALL", call or ""),
                _field("QSO_DATE", qso_date or ""),
                _field("TIME_ON", time_on or ""),
                _field("TIME_OFF", time_off or time_on or ""),
                _field("MODE", "Q65"),
                _field("PROP_MODE", "SAT"),
            ]
            if freq_mhz:
                entry_parts.append(_field("FREQ", freq_mhz))
            if sat:
                entry_parts.append(_field("SAT_NAME", sat))
            if rst_s:
                entry_parts.append(_field("RST_SENT", rst_s))
            if rst_r:
                entry_parts.append(_field("RST_RCVD", rst_r))
            if grid:
                entry_parts.append(_field("GRIDSQUARE", grid))
            entry_parts.append("<EOR>")
            lines.append(" ".join(entry_parts))
            lines.append("")

        with open(path, "w", encoding="ascii") as fh:
            fh.write("\n".join(lines))
        return len(rows)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_state(self, new_state: Q65QsoState, tx_msg: str) -> None:
        self._state = new_state
        self._pending_tx = tx_msg
        if self._on_state:
            self._on_state(new_state, tx_msg)

    def _log_qso(self) -> None:
        if not self.dx_call:
            return
        now = datetime.now(UTC)
        time_off = now.strftime("%H%M%S")
        qso_date = now.strftime("%Y%m%d")
        time_on = self._time_on or now.strftime("%H%M%S")
        rst_sent = self.rst_sent or "-05"
        rst_rcvd = self.rst_rcvd or "-05"
        try:
            self._conn.execute(
                "INSERT INTO q65_log "
                "(qso_date,time_on,time_off,call,gridsquare,"
                "rst_sent,rst_rcvd,freq_hz,norad_cat_id,sat_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    qso_date,
                    time_on,
                    time_off,
                    self.dx_call,
                    self.dx_grid,
                    rst_sent,
                    rst_rcvd,
                    self.freq_hz,
                    self._norad,
                    self._sat_name,
                ),
            )
            self._conn.commit()
        except Exception:
            return
        self._broadcast_adif(qso_date, time_on, time_off, rst_sent, rst_rcvd)

    def _broadcast_adif(
        self, qso_date: str, time_on: str, time_off: str, rst_sent: str, rst_rcvd: str
    ) -> None:
        """Send this QSO to the UDP log broadcaster (wavelog-gate / JT-Linker etc.)."""
        from comms.log_broadcast import get_log_broadcaster  # noqa: PLC0415
        from ui.adif_utils import build_adif_record  # noqa: PLC0415

        freq_mhz = f"{self.freq_hz / 1e6:.6f}" if self.freq_hz else ""
        record = build_adif_record(
            {
                "CALL": self.dx_call,
                "QSO_DATE": qso_date,
                "TIME_ON": time_on,
                "TIME_OFF": time_off,
                "MODE": "Q65",
                "PROP_MODE": "SAT",
                "FREQ": freq_mhz,
                "SAT_NAME": self._sat_name,
                "RST_SENT": rst_sent,
                "RST_RCVD": rst_rcvd,
                "GRIDSQUARE": self.dx_grid,
            }
        )
        broadcaster = get_log_broadcaster()
        broadcaster.reload_settings(self._conn)
        broadcaster.send_adif_record(record)

    def _ensure_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS q65_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                qso_date      TEXT NOT NULL,
                time_on       TEXT NOT NULL,
                time_off      TEXT,
                call          TEXT NOT NULL,
                gridsquare    TEXT,
                rst_sent      TEXT,
                rst_rcvd      TEXT,
                freq_hz       INTEGER,
                norad_cat_id  INTEGER,
                sat_name      TEXT
            )
            """
        )
        self._conn.commit()
