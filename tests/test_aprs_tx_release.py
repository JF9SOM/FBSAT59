"""A transmit tab opening closes the APRS tab when that holds the transmit sound card."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ui.main_window import MainWindow


class _Stub(SimpleNamespace):
    """Just enough of MainWindow for _release_tx_sound_card_from_aprs()."""


def _run(monkeypatch: pytest.MonkeyPatch, busy: bool | Exception, aprs_tab_open: bool) -> _Stub:
    engine = SimpleNamespace(disabled=False)

    def disable() -> None:
        engine.disabled = True

    engine.disable_tx_session = disable
    aprs_tab = object()
    stub = _Stub(
        _conn=None,
        closed=[],
        messages=[],
        _find_comms_tab=lambda key: aprs_tab if aprs_tab_open and key == "aprs" else None,
        _tab_widget=SimpleNamespace(indexOf=lambda tab: 3 if tab is aprs_tab else -1),
        statusBar=lambda: SimpleNamespace(showMessage=lambda msg, ms=0: stub.messages.append(msg)),
    )
    stub._on_tab_close_requested = lambda idx: stub.closed.append(idx)
    stub.engine = engine

    def fake_engine(_conn: Any) -> Any:
        if isinstance(busy, Exception):
            raise busy
        engine.tx_session_busy = busy
        return engine

    monkeypatch.setattr("comms.aprs.engine.get_aprs_engine", fake_engine)
    MainWindow._release_tx_sound_card_from_aprs(stub)  # type: ignore[arg-type]
    return stub


def test_closes_the_aprs_tab_when_it_holds_the_sound_card(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _run(monkeypatch, busy=True, aprs_tab_open=True)
    assert stub.closed == [3]
    assert len(stub.messages) == 1 and "APRS tab was closed" in stub.messages[0]


def test_leaves_an_aprs_tab_that_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _run(monkeypatch, busy=False, aprs_tab_open=True)
    assert stub.closed == [] and stub.messages == []


def test_stops_a_session_that_has_no_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _run(monkeypatch, busy=True, aprs_tab_open=False)
    assert stub.engine.disabled and stub.closed == []


def test_a_failing_check_never_blocks_opening_the_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _run(monkeypatch, busy=RuntimeError("boom"), aprs_tab_open=True)
    assert stub.closed == [] and stub.messages == []
