"""FT4/Q65 transmit workers: PTT off must be verified and retried.

The rig's set_ptt(False) used to be fire-and-forget, so a PTT-off that never
reached the radio (NET mode, timed-out shared socket) went unnoticed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ui.ft4_tab import _TxWorker
from ui.q65_tab import Q65Tab


def _ft4_release(rig: MagicMock) -> bool:
    worker = _TxWorker(MagicMock(), None, rig, lambda: 1.0)
    with patch("ui.ft4_tab.time.sleep"):
        return worker._release_ptt()


def _q65_release(rig: MagicMock) -> bool:
    with patch("ui.q65_tab.time.sleep"):
        return Q65Tab._release_ptt(rig)


def test_release_ptt_succeeds_first_try() -> None:
    for release in (_ft4_release, _q65_release):
        rig = MagicMock()
        rig.set_ptt.return_value = True
        assert release(rig) is True
        rig.set_ptt.assert_called_once_with(False)


def test_release_ptt_retries_once_after_failure() -> None:
    for release in (_ft4_release, _q65_release):
        rig = MagicMock()
        rig.set_ptt.side_effect = [False, True]
        assert release(rig) is True
        assert rig.set_ptt.call_count == 2


def test_release_ptt_reports_failure_when_both_attempts_fail() -> None:
    for release in (_ft4_release, _q65_release):
        rig = MagicMock()
        rig.set_ptt.return_value = False
        assert release(rig) is False
        assert rig.set_ptt.call_count == 2


def test_release_ptt_survives_exceptions() -> None:
    for release in (_ft4_release, _q65_release):
        rig = MagicMock()
        rig.set_ptt.side_effect = [RuntimeError("boom"), True]
        assert release(rig) is True
