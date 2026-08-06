"""FT4 QSO state machine tests.

The states are named for what we last sent, so each test reads as a real
on-air exchange: feed the messages the other station would send and check
we answer with what the FT4 convention says comes next.

Covers the protocol correction from GitHub Issue #16 -- answering a grid
with a plain report rather than jumping straight to an R-report -- plus
the two ways of answering a station and auto-starting from IDLE.
"""

from __future__ import annotations

import pytest

from comms.ft4.qso import Ft4QsoManager, QsoState, format_report

MY_CALL = "EI4GNB"
MY_GRID = "IO63"
THEIR_CALL = "JA1XYZ"
THEIR_GRID = "PM95"


@pytest.fixture
def qso() -> Ft4QsoManager:
    return Ft4QsoManager(MY_CALL, MY_GRID)


class TestFormatReport:
    def test_negative(self) -> None:
        assert format_report(-12.0) == "-12"

    def test_positive_is_zero_padded(self) -> None:
        """WSJT-X sends "+03", not "+3"."""
        assert format_report(3.0) == "+03"

    def test_rounds_to_whole_db(self) -> None:
        assert format_report(-11.6) == "-12"


class TestCallingCq:
    """We called CQ and someone comes back to us."""

    def test_grid_reply_gets_a_plain_report(self, qso: Ft4QsoManager) -> None:
        """The Issue #16 fix: a grid is answered with a report WITHOUT the
        R prefix -- R belongs one step later, after they have reported us."""
        qso.start_cq()
        reply = qso.advance(f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", their_snr=-12.0)
        assert reply == f"{THEIR_CALL} {MY_CALL} -12"
        assert qso.state == QsoState.EXCHANGE

    def test_full_exchange_to_logged(self, qso: Ft4QsoManager) -> None:
        qso.start_cq()
        qso.advance(f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", their_snr=-12.0)
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} R-08") == f"{THEIR_CALL} {MY_CALL} RR73"
        assert qso.state == QsoState.CONFIRM
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} 73") is None
        assert qso.state == QsoState.LOGGED
        assert qso.session.rst_rcvd == "-08"

    def test_report_reply_skips_the_grid_step(self, qso: Ft4QsoManager) -> None:
        """Plenty of operators answer a CQ with a report and no grid."""
        qso.start_cq()
        reply = qso.advance(f"{MY_CALL} {THEIR_CALL} -14", their_snr=-12.0)
        assert reply == f"{THEIR_CALL} {MY_CALL} R-12"
        assert qso.state == QsoState.RREPORT_SENT
        assert qso.session.rst_rcvd == "-14"

    def test_ignores_traffic_between_other_stations(self, qso: Ft4QsoManager) -> None:
        qso.start_cq()
        assert qso.advance("DL1ABC F5XYZ JN18", their_snr=-5.0) is None
        assert qso.state == QsoState.CALLING


class TestAnsweringWithGrid:
    """MyGrid button / double-click: the standard opening exchange."""

    def test_opens_with_our_grid(self, qso: Ft4QsoManager) -> None:
        msg = qso.respond_with_grid(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        assert msg == f"{THEIR_CALL} {MY_CALL} {MY_GRID}"
        assert qso.state == QsoState.GRID_SENT
        assert qso.session.their_snr_db == -9.0

    def test_their_report_gets_an_r_report_back(self, qso: Ft4QsoManager) -> None:
        qso.respond_with_grid(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        reply = qso.advance(f"{MY_CALL} {THEIR_CALL} -07", their_snr=-9.0)
        assert reply == f"{THEIR_CALL} {MY_CALL} R-09"
        assert qso.state == QsoState.RREPORT_SENT

    def test_rr73_ends_with_our_73(self, qso: Ft4QsoManager) -> None:
        qso.respond_with_grid(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        qso.advance(f"{MY_CALL} {THEIR_CALL} -07", their_snr=-9.0)
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} RR73") == f"{THEIR_CALL} {MY_CALL} 73"
        assert qso.state == QsoState.LOGGED


class TestAnsweringWithReport:
    """RST button: skip the grid, useful on a short satellite pass."""

    def test_opens_with_the_measured_report(self, qso: Ft4QsoManager) -> None:
        msg = qso.respond_with_report(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        assert msg == f"{THEIR_CALL} {MY_CALL} -09"
        assert qso.state == QsoState.EXCHANGE

    def test_full_exchange_to_logged(self, qso: Ft4QsoManager) -> None:
        qso.respond_with_report(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} R-07") == f"{THEIR_CALL} {MY_CALL} RR73"
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} 73") is None
        assert qso.state == QsoState.LOGGED


class TestAutoStartFromIdle:
    """Someone calls us while we are only monitoring."""

    def test_stays_put_unless_allowed(self, qso: Ft4QsoManager) -> None:
        """Default off: monitoring must never start answering by itself."""
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", their_snr=-15.0) is None
        assert qso.state == QsoState.IDLE

    def test_starts_when_allowed(self, qso: Ft4QsoManager) -> None:
        reply = qso.advance(
            f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", their_snr=-15.0, allow_auto_start=True
        )
        assert reply == f"{THEIR_CALL} {MY_CALL} -15"
        assert qso.state == QsoState.EXCHANGE
        assert qso.session.their_call == THEIR_CALL
        assert qso.session.their_grid == THEIR_GRID

    def test_first_caller_wins(self, qso: Ft4QsoManager) -> None:
        """Once we have a partner, later callers in the same period are not
        matched -- the tab feeds decodes in order and stops at the first."""
        first = qso.advance(
            f"{MY_CALL} {THEIR_CALL} {THEIR_GRID}", their_snr=-15.0, allow_auto_start=True
        )
        assert first is not None
        second = qso.advance(f"{MY_CALL} DL1ABC JO65", their_snr=-5.0, allow_auto_start=True)
        assert second is None
        assert qso.session.their_call == THEIR_CALL

    def test_ignores_calls_to_other_stations(self, qso: Ft4QsoManager) -> None:
        assert qso.advance("DL1ABC F5XYZ JN18", their_snr=-5.0, allow_auto_start=True) is None
        assert qso.state == QsoState.IDLE


class TestButtonStateOverride:
    def test_set_state_lets_buttons_jump_ahead(self, qso: Ft4QsoManager) -> None:
        """The quick buttons skip steps; the state has to follow or the next
        decode would be judged against a step already passed."""
        qso.respond_with_grid(THEIR_CALL, THEIR_GRID, their_snr_db=-9.0)
        qso.set_state(QsoState.CONFIRM)
        qso.pending_tx = f"{THEIR_CALL} {MY_CALL} RR73"
        assert qso.advance(f"{MY_CALL} {THEIR_CALL} 73") is None
        assert qso.state == QsoState.LOGGED
