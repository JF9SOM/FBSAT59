"""Tests for MainWindow._with_ongoing_pass (in-progress pass kept on searches)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from ui.main_window import MainWindow


def _pass(aos: datetime, minutes: int = 10) -> Any:
    return SimpleNamespace(aos=aos, los=aos + timedelta(minutes=minutes))


def _call(predictor: Any, passes: list[Any], start: datetime, end: datetime) -> list[Any]:
    fake = SimpleNamespace(_pass_predictor=predictor, _selected_norad=67290)
    return MainWindow._with_ongoing_pass(fake, passes, start, end)  # type: ignore[arg-type]


def test_prepends_ongoing_pass_when_window_contains_now() -> None:
    now = datetime.now(UTC)
    current = _pass(now - timedelta(minutes=3))
    later = _pass(now + timedelta(hours=12))
    predictor = SimpleNamespace(get_current_pass=lambda norad, at: current)
    result = _call(predictor, [later], now, now + timedelta(hours=24))
    assert result == [current, later]


def test_no_duplicate_and_no_change_when_not_in_pass() -> None:
    now = datetime.now(UTC)
    later = _pass(now + timedelta(hours=12))
    none_pred = SimpleNamespace(get_current_pass=lambda norad, at: None)
    assert _call(none_pred, [later], now, now + timedelta(hours=24)) == [later]
    dup_pred = SimpleNamespace(get_current_pass=lambda norad, at: later)
    assert _call(dup_pred, [later], now, now + timedelta(hours=24)) == [later]


def test_future_window_is_left_alone() -> None:
    now = datetime.now(UTC)
    current = _pass(now - timedelta(minutes=3))
    predictor = SimpleNamespace(get_current_pass=lambda norad, at: current)
    start = now + timedelta(hours=2)
    assert _call(predictor, [], start, start + timedelta(hours=6)) == []
