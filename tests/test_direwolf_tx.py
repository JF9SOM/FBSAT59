"""Direwolf transmit worker: config text and the fallback to the built-in generator."""

from __future__ import annotations

from typing import Any

import pytest

from comms.aprs import direwolf_tx
from comms.aprs.direwolf_tx import DirewolfStartError, DirewolfTxWorker, build_config


def test_build_config_uses_real_devices_and_valid_values() -> None:
    conf = build_config("Mic In", "USB Audio CODEC", "1200", 300, 100, 8850)
    assert 'ADEVICE "Mic In" "USB Audio CODEC"' in conf
    assert "MODEM 1200" in conf
    assert "TXDELAY 30" in conf  # 10 ms units
    assert "TXTAIL 10" in conf
    # Direwolf only accepts KISS ports 1000..10000, SLOTTIME >= 5, and 0 turns 8001 off.
    assert "KISSPORT 0\nKISSPORT 8850" in conf
    assert "SLOTTIME 5" in conf
    assert "stdin" not in conf  # a stdin input never opens the output device


def test_build_config_4800_uses_g3ruh() -> None:
    assert "MODEM 4800 G3RUH" in build_config("a", "b", "4800", 300, 100, 8850)


def test_free_port_is_in_direwolf_range() -> None:
    assert 1000 <= direwolf_tx._free_port() <= 10000


def _worker(rig: Any = None) -> DirewolfTxWorker:
    return DirewolfTxWorker("APRS", 1, rig, frame=b"x", modem="1200", txdelay_ms=300, txtail_ms=100)


def test_start_failure_is_reported_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Any) -> None:
        raise DirewolfStartError("direwolf not found")

    monkeypatch.setattr(DirewolfTxWorker, "_run_direwolf", boom)
    w = _worker()
    errors: list[str] = []
    done: list[bool] = []
    w.error.connect(errors.append)
    w.finished.connect(lambda: done.append(True))
    w.run()
    assert len(errors) == 1 and "direwolf not found" in errors[0]
    assert done == []


def test_estimated_time_grows_with_delays_and_shrinks_with_baud() -> None:
    slow = direwolf_tx._estimate_seconds(30, "1200", 300, 100)
    fast = direwolf_tx._estimate_seconds(30, "9600", 300, 100)
    assert slow > fast > 0.4
    assert direwolf_tx._estimate_seconds(30, "1200", 600, 100) == pytest.approx(slow + 0.3)
