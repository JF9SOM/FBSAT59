"""Recovery from a re-enumerated USB audio device / serial PTT port (no hardware)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from comms.audio_device_manager import AudioDeviceManager, _SharedInputStream
from rig.controller import HamlibNetController


class _FakeSd(types.SimpleNamespace):
    """Stands in for sounddevice: OutputStream fails until PortAudio is re-initialised."""

    def __init__(self, fail_until_reinit: bool) -> None:
        super().__init__()
        self.healthy = not fail_until_reinit
        self.calls: list[str] = []

        def output_stream(**kwargs: Any) -> Any:
            self.calls.append("open")
            if not self.healthy:
                raise RuntimeError("Error opening OutputStream: Internal PortAudio error [-9986]")
            return types.SimpleNamespace(close=lambda: self.calls.append("close"))

        def terminate() -> None:
            self.calls.append("terminate")

        def initialize() -> None:
            self.calls.append("initialize")
            self.healthy = True

        self.OutputStream = output_stream
        self._terminate = terminate
        self._initialize = initialize
        self.stop = lambda: None


@pytest.fixture()
def manager() -> AudioDeviceManager:
    return AudioDeviceManager()


def test_a_working_output_is_not_disturbed(
    manager: AudioDeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSd(fail_until_reinit=False)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    manager.ensure_output_ready(0, 48_000)
    assert fake.calls == ["open", "close"]


def test_a_dead_output_triggers_one_reinitialisation_then_works(
    manager: AudioDeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSd(fail_until_reinit=True)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    manager.ensure_output_ready(0, 48_000)
    assert fake.calls == ["open", "terminate", "initialize", "open", "close"]


def test_an_output_that_stays_dead_raises(
    manager: AudioDeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSd(fail_until_reinit=True)
    fake._initialize = lambda: fake.calls.append("initialize")  # stays unhealthy
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    with pytest.raises(RuntimeError, match="9986"):
        manager.ensure_output_ready(0, 48_000)


def test_input_streams_keep_their_subscribers_across_a_reinitialisation(
    manager: AudioDeviceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSd(fail_until_reinit=False)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    events: list[str] = []

    shared = _SharedInputStream(1)
    shared._subscribers["cw"] = (48_000, lambda chunk: None)
    shared._stream = types.SimpleNamespace(
        stop=lambda: events.append("stop"), close=lambda: events.append("close")
    )
    reopened: list[int] = []
    monkeypatch.setattr(shared, "_open", lambda schedule_settle_reopen=True: reopened.append(1))
    manager._inputs[1] = shared

    assert manager.reinitialize_portaudio() is True
    assert events == ["stop", "close"]
    assert reopened == [1]
    assert "cw" in shared._subscribers


class _Line:
    def __init__(self, works: bool) -> None:
        self.works = works
        self.keyed: list[bool] = []

    def key(self, on: bool) -> bool:
        self.keyed.append(on)
        return self.works

    def close(self) -> None:
        pass


def _net_rig(monkeypatch: pytest.MonkeyPatch, first: _Line, second: _Line) -> HamlibNetController:
    rig = HamlibNetController()
    rig.set_ptt_config("dtr", "/dev/cu.fake")
    monkeypatch.setattr(HamlibNetController, "is_connected", property(lambda self: True))
    rig._ptt_line = first  # type: ignore[assignment]

    def reopen() -> None:
        rig._ptt_line = second  # type: ignore[assignment]

    monkeypatch.setattr(rig, "_open_ptt_line", reopen)
    return rig


def test_a_dead_ptt_line_is_reopened_once_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    dead, fresh = _Line(False), _Line(True)
    rig = _net_rig(monkeypatch, dead, fresh)
    assert rig.set_ptt(True) is True
    assert dead.keyed == [True]
    assert fresh.keyed == [True]


def test_a_ptt_line_that_cannot_be_reopened_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _net_rig(monkeypatch, _Line(False), _Line(False))
    assert rig.set_ptt(True) is False


def test_a_healthy_ptt_line_is_not_reopened(monkeypatch: pytest.MonkeyPatch) -> None:
    good, spare = _Line(True), _Line(True)
    rig = _net_rig(monkeypatch, good, spare)
    assert rig.set_ptt(True) is True
    assert spare.keyed == []
