"""Warning when a transmit tab opens while the APRS tab holds the transmit sound card."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import ui.main_window as main_window_mod
from ui.main_window import MainWindow


def _call(monkeypatch: pytest.MonkeyPatch, holds: bool | Exception) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []

    def fake_warning(_parent: Any, title: str, text: str) -> None:
        shown.append((title, text))

    def fake_engine(_conn: Any) -> Any:
        if isinstance(holds, Exception):
            raise holds
        return SimpleNamespace(tx_session_open=holds)

    monkeypatch.setattr(main_window_mod.QMessageBox, "warning", fake_warning)
    monkeypatch.setattr("comms.aprs.engine.get_aprs_engine", fake_engine)
    MainWindow._warn_if_aprs_holds_tx_output(SimpleNamespace(_conn=None))  # type: ignore[arg-type]
    return shown


def test_warns_when_the_aprs_tab_holds_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = _call(monkeypatch, True)
    assert len(shown) == 1
    assert "APRS tab is holding the transmit sound card" in shown[0][1]


def test_silent_when_nothing_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _call(monkeypatch, False) == []


def test_a_failing_check_never_blocks_opening_the_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _call(monkeypatch, RuntimeError("boom")) == []
