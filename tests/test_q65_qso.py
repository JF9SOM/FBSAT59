"""Q65 QSO state machine tests.

Covers the GitHub Issue #16 fixes ported over from the FT4 tab's QSO
manager: reports must reflect the SNR we actually measured, never a fixed
"-05" placeholder and never an echo of whatever the other station sent
us.
"""

from __future__ import annotations

import sqlite3

import pytest

from comms.q65.qso import Q65QsoManager, Q65QsoState, format_report
from data.database import SCHEMA_SQL

MY_CALL = "EI4GNB"
MY_GRID = "IO63"
THEIR_CALL = "JA1XYZ"
THEIR_GRID = "PM95"


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory SQLite DB with the full schema (provides app_settings for
    the log-broadcast call that _log_qso() makes)."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    c.commit()
    return c


@pytest.fixture
def qso(conn: sqlite3.Connection) -> Q65QsoManager:
    return Q65QsoManager(conn, MY_CALL, MY_GRID)


class TestFormatReport:
    def test_negative(self) -> None:
        assert format_report(-12.0) == "-12"

    def test_positive_is_zero_padded(self) -> None:
        assert format_report(3.0) == "+03"

    def test_rounds_to_whole_db(self) -> None:
        assert format_report(-11.6) == "-12"


class TestCallingCq:
    """We called CQ and someone comes back to us.

    Directed messages are "<TO> <FROM> <payload>" (same packer as FT4/FT8):
    a reply to our CQ is addressed to us, so MY_CALL comes first.
    """

    def test_grid_reply_uses_measured_snr(self, qso: Q65QsoManager) -> None:
        qso.start_cq()
        qso.on_decoded(f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", -18.0)
        assert qso.state == Q65QsoState.EXCHANGE
        assert qso.dx_call == THEIR_CALL
        assert qso.dx_grid == THEIR_GRID
        assert qso.rst_sent == "-18"
        assert qso.their_snr_db == -18.0

    def test_report_reply_does_not_echo_their_report(self, qso: Q65QsoManager) -> None:
        """GitHub Issue #16: the outgoing reply used to just copy back
        whatever number they sent, instead of our own measured SNR."""
        qso.start_cq()
        qso.on_decoded(f"{MY_CALL} {THEIR_CALL} -07", -18.0)
        assert qso.rst_rcvd == "-07"  # what THEY reported of us
        assert qso.rst_sent == "-18"  # OUR measurement of THEM -- not "-07"

    def test_backwards_order_does_not_match(self, qso: Q65QsoManager) -> None:
        """GitHub Issue #16: the TO/FROM order used to be backwards, so a
        correctly-addressed reply (this one) never advanced the state."""
        qso.start_cq()
        qso.on_decoded(f"{THEIR_CALL} {MY_CALL} {THEIR_GRID}", -18.0)
        assert qso.state == Q65QsoState.CALLING

    def test_ignores_traffic_between_other_stations(self, qso: Q65QsoManager) -> None:
        qso.start_cq()
        qso.on_decoded("DL1ABC F5XYZ JN18", -5.0)
        assert qso.state == Q65QsoState.CALLING
        assert qso.dx_call == ""


class TestCallStation:
    """Double-clicking a decoded row (or the future Call Station button)."""

    def test_uses_the_row_s_measured_snr(self, qso: Q65QsoManager) -> None:
        qso.call_station(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        assert qso.state == Q65QsoState.EXCHANGE
        assert qso.rst_sent == "-09"
        assert qso.their_snr_db == -9.0

    def test_falls_back_to_placeholder_without_snr(self, qso: Q65QsoManager) -> None:
        qso.call_station(THEIR_CALL, THEIR_GRID)
        assert qso.rst_sent == "-05"


class TestFullExchange:
    def test_calling_to_logged(self, qso: Q65QsoManager) -> None:
        qso.start_cq()
        qso.on_decoded(f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", -18.0)
        qso.on_decoded(f"{MY_CALL} {THEIR_CALL} R-11", -18.0)
        state_after_r_report = qso.state
        assert state_after_r_report == Q65QsoState.CONFIRM
        assert qso.rst_rcvd == "-11"
        qso.on_decoded(f"{MY_CALL} {THEIR_CALL} 73", -18.0)
        state_after_73 = qso.state
        assert state_after_73 == Q65QsoState.LOGGED


class TestHalt:
    def test_resets_to_idle(self, qso: Q65QsoManager) -> None:
        qso.start_cq()
        qso.on_decoded(f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", -18.0)
        qso.halt()
        assert qso.state == Q65QsoState.IDLE
        assert qso.tx_enable is False
