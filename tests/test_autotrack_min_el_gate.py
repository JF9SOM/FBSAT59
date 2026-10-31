"""Autotrack fires its AOS actions only for passes that reach Min El."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ui.main_window import MainWindow


class _Predictor:
    def __init__(self, current: object | None) -> None:
        self._current = current
        self.calls = 0

    def get_current_pass(self, norad: int, now: Any) -> object | None:
        self.calls += 1
        return self._current


def _fake_window(current: object | None) -> Any:
    win = SimpleNamespace(
        _pass_predictor=_Predictor(current),
        _autotrack_skipped_pass=None,
        _autotrack_aos_fired=False,
        _autotrack_meteor_record=False,
        _autotrack_iq_record=False,
        _min_pass_el=50.0,
        _at_dialog=SimpleNamespace(set_autotrack_status=lambda *a, **k: None),
        fired=0,
    )

    def on_aos(name: str) -> None:
        win.fired += 1

    win._autotrack_on_aos = on_aos
    win._autotrack_pass_meets_min_el = lambda n: MainWindow._autotrack_pass_meets_min_el(win, n)
    return win


def test_pass_below_min_el_does_not_fire() -> None:
    win = _fake_window(None)
    assert MainWindow._maybe_fire_autotrack_aos(win, 25544, "ISS", 3.0) is False
    # The verdict is cached: the next tick does not recompute the pass.
    assert MainWindow._maybe_fire_autotrack_aos(win, 25544, "ISS", 4.0) is False
    assert win._pass_predictor.calls == 1
    assert win.fired == 0
    assert win._autotrack_aos_fired is False


def test_pass_reaching_min_el_fires() -> None:
    win = _fake_window(object())
    assert MainWindow._maybe_fire_autotrack_aos(win, 25544, "ISS", 3.0) is True
    assert win.fired == 1
    assert win._autotrack_aos_fired is True
