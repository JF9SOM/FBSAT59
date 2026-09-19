"""SdrDevice open/reopen robustness (fake SoapySDR, no hardware).

Regression for the 2026-09-19 SDR failure: MainWindow._load_rig_settings()
released the superseded SDR and reconnected ~35 ms later; the new handle
opened but rejected setSampleRate()/setFrequency(), open() reported success
anyway, and the SDR stayed unusable until the app was restarted.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

import sdr.device as device_mod
from sdr.device import SdrDevice, SdrDeviceInfo

# "airspy" (not rtlsdr/hackrf) so the SoapySDR path is used on every platform;
# Windows has ctypes bypasses for those two drivers.
_INFO = SdrDeviceInfo(
    driver="airspy", label="Fake SDR", serial="1", hardware="", args={"driver": "airspy"}
)


class _Env:
    """Fake SoapySDR module plus a recording stand-in for time.sleep()."""

    def __init__(self, rejecting_handles: int) -> None:
        self.constructed = 0
        self.sleeps: list[float] = []
        self._rejecting = rejecting_handles
        self.handles: list[MagicMock] = []

    def make_device(self, args: Any) -> MagicMock:
        self.constructed += 1
        dev = MagicMock()
        if self.constructed <= self._rejecting:
            dev.setSampleRate.side_effect = RuntimeError("setSampleRate failed")
            dev.setFrequency.side_effect = RuntimeError("setFrequency failed")
        self.handles.append(dev)
        return dev


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env(rejecting_handles=0)
    fake = types.ModuleType("SoapySDR")
    fake.SOAPY_SDR_RX = 0  # type: ignore[attr-defined]
    fake.SOAPY_SDR_CF32 = "CF32"  # type: ignore[attr-defined]
    fake.registerLogHandler = lambda fn: None  # type: ignore[attr-defined]
    fake.Device = e.make_device  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "SoapySDR", fake)
    monkeypatch.setattr(device_mod, "SOAPY_AVAILABLE", True)
    monkeypatch.setattr(device_mod, "_last_close_monotonic", 0.0)
    clock = types.SimpleNamespace(monotonic=time.monotonic, sleep=e.sleeps.append)
    monkeypatch.setattr(device_mod, "time", clock)
    return e


def _new_device() -> SdrDevice:
    dev = SdrDevice(_INFO)
    dev._gain_mode = "manual"  # keep the fake's gain plumbing out of the way
    return dev


def test_open_retries_when_the_handle_rejects_its_basic_settings(env: _Env) -> None:
    env._rejecting = 2  # the first two handles are "not released yet"
    dev = _new_device()

    assert dev.open() is True

    assert env.constructed == 3
    assert dev.is_open
    # The two unusable handles were dropped, the usable one kept and configured.
    assert dev._dev is env.handles[2]
    env.handles[2].setSampleRate.assert_called()
    # ...and each failure was followed by a pause before trying again.
    assert env.sleeps.count(device_mod._NOT_READY_DELAY_S) == 2


def test_open_gives_up_instead_of_returning_an_unusable_device(env: _Env) -> None:
    env._rejecting = 10_000
    dev = _new_device()

    assert dev.open() is False
    assert not dev.is_open


def test_open_waits_after_a_recent_close(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_mod, "_last_close_monotonic", time.monotonic() - 0.05)
    dev = _new_device()

    assert dev.open() is True

    waited = [s for s in env.sleeps if s > 0]
    assert waited, "open() must wait out the release grace period"
    assert waited[0] == pytest.approx(device_mod._RELEASE_SETTLE_S - 0.05, abs=0.05)


def test_open_does_not_wait_when_nothing_was_closed_recently(env: _Env) -> None:
    dev = _new_device()
    assert dev.open() is True
    assert env.sleeps == []


def test_reopen_closes_reopens_and_restarts_the_stream(env: _Env) -> None:
    dev = _new_device()
    assert dev.open() is True
    assert dev.start_stream() is True
    first = dev._dev

    assert dev.reopen() is True

    assert dev._dev is not first
    assert env.constructed == 2
    assert dev._stream is not None  # stream running again
    # reopen() went through close(), so the next open() honours the grace period.
    assert any(s > 0 for s in env.sleeps)


def test_restart_stream_keeps_the_device_and_cycles_the_stream(env: _Env) -> None:
    dev = _new_device()
    assert dev.open() is True
    assert dev.start_stream() is True
    handle = env.handles[0]

    assert dev.restart_stream() is True

    assert dev._dev is handle
    assert handle.deactivateStream.call_count == 1
    assert handle.activateStream.call_count == 2
