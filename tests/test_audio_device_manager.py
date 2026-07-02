"""Unit tests for comms/audio_device_manager.py.

No real sounddevice hardware is used — the underlying InputStream is faked
via sys.modules so these tests run in headless CI.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

import comms.audio_device_manager as adm
from comms.audio_device_manager import AudioDeviceManager, _resample

# ---------------------------------------------------------------------------
# Fake sounddevice.InputStream
# ---------------------------------------------------------------------------


class _FakeInputStream:
    """Records lifecycle calls and lets tests push audio through the callback."""

    instances: list[_FakeInputStream] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        _FakeInputStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def push(self, mono_samples: np.ndarray) -> None:
        indata = mono_samples.reshape(-1, 1).astype(np.float32)
        self.kwargs["callback"](indata, len(mono_samples), None, None)


@pytest.fixture
def fake_sounddevice(monkeypatch: pytest.MonkeyPatch) -> type[_FakeInputStream]:
    _FakeInputStream.instances = []
    fake_module = types.SimpleNamespace(InputStream=_FakeInputStream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_module)
    return _FakeInputStream


# ---------------------------------------------------------------------------
# _resample
# ---------------------------------------------------------------------------


class TestResample:
    def test_same_rate_is_passthrough(self) -> None:
        chunk = np.arange(10, dtype=np.float32)
        out = _resample(chunk, 48_000, 48_000)
        assert out is chunk

    def test_empty_chunk(self) -> None:
        out = _resample(np.empty(0, dtype=np.float32), 48_000, 3_200)
        assert len(out) == 0

    def test_integer_decimation(self) -> None:
        chunk = np.arange(4800, dtype=np.float32)
        out = _resample(chunk, 48_000, 3_200)  # factor of 15
        assert len(out) == 4800 // 15
        assert out.dtype == np.float32

    def test_scipy_resample_ratio(self) -> None:
        chunk = np.sin(np.linspace(0, 4 * np.pi, 4800)).astype(np.float32)
        out = _resample(chunk, 48_000, 44_100)
        # 4800 samples @ 48kHz -> ~4410 samples @ 44.1kHz
        assert abs(len(out) - 4410) <= 2
        assert out.dtype == np.float32

    def test_fallback_without_scipy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adm, "_SCIPY_AVAILABLE", False)
        chunk = np.sin(np.linspace(0, 4 * np.pi, 4800)).astype(np.float32)
        out = _resample(chunk, 48_000, 44_100)
        assert abs(len(out) - 4410) <= 2
        assert out.dtype == np.float32


# ---------------------------------------------------------------------------
# TX — exclusive output lock
# ---------------------------------------------------------------------------


class TestOutputLock:
    def test_first_owner_acquires(self) -> None:
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("ft4", 2) is True
        assert mgr.output_owner(2) == "ft4"

    def test_second_owner_is_rejected(self) -> None:
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("ft4", 2) is True
        assert mgr.acquire_output("q65", 2) is False
        assert mgr.output_owner(2) == "ft4"

    def test_same_owner_can_reacquire(self) -> None:
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("ft4", 2) is True
        assert mgr.acquire_output("ft4", 2) is True

    def test_release_frees_device_for_others(self) -> None:
        mgr = AudioDeviceManager()
        mgr.acquire_output("ft4", 2)
        mgr.release_output("ft4", 2)
        assert mgr.output_owner(2) is None
        assert mgr.acquire_output("q65", 2) is True

    def test_release_by_non_owner_is_noop(self) -> None:
        mgr = AudioDeviceManager()
        mgr.acquire_output("ft4", 2)
        mgr.release_output("q65", 2)
        assert mgr.output_owner(2) == "ft4"

    def test_different_devices_are_independent(self) -> None:
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("ft4", 2) is True
        assert mgr.acquire_output("q65", 3) is True

    def test_none_device_is_a_valid_key(self) -> None:
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("q65", None) is True
        assert mgr.acquire_output("ft4", None) is False


# ---------------------------------------------------------------------------
# RX — shared input (pub/sub)
# ---------------------------------------------------------------------------


class TestInputSharing:
    def test_first_subscriber_opens_stream(self, fake_sounddevice: type[_FakeInputStream]) -> None:
        mgr = AudioDeviceManager()
        received: list[np.ndarray] = []
        mgr.acquire_input("cw", 5, 48_000, received.append)
        assert len(fake_sounddevice.instances) == 1
        assert fake_sounddevice.instances[0].started is True

    def test_second_subscriber_shares_existing_stream(
        self, fake_sounddevice: type[_FakeInputStream]
    ) -> None:
        mgr = AudioDeviceManager()
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        mgr.acquire_input("sstv", 5, 44_100, lambda c: None)
        assert len(fake_sounddevice.instances) == 1

    def test_audio_fans_out_to_all_subscribers(
        self, fake_sounddevice: type[_FakeInputStream]
    ) -> None:
        mgr = AudioDeviceManager()
        received_a: list[np.ndarray] = []
        received_b: list[np.ndarray] = []
        mgr.acquire_input("cw", 5, 48_000, received_a.append)
        mgr.acquire_input("ft4", 5, 12_000, received_b.append)

        stream = fake_sounddevice.instances[0]
        stream.push(np.arange(4800, dtype=np.float32))

        assert len(received_a) == 1 and len(received_a[0]) == 4800
        assert len(received_b) == 1 and len(received_b[0]) == 1200  # 48000/12000=4

    def test_stream_closes_only_after_last_subscriber_releases(
        self, fake_sounddevice: type[_FakeInputStream]
    ) -> None:
        mgr = AudioDeviceManager()
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        mgr.acquire_input("sstv", 5, 44_100, lambda c: None)
        stream = fake_sounddevice.instances[0]

        mgr.release_input("cw", 5)
        assert stream.closed is False

        mgr.release_input("sstv", 5)
        assert stream.closed is True

    def test_devices_get_independent_streams(
        self, fake_sounddevice: type[_FakeInputStream]
    ) -> None:
        mgr = AudioDeviceManager()
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        mgr.acquire_input("sstv", 6, 44_100, lambda c: None)
        assert len(fake_sounddevice.instances) == 2

    def test_reopening_after_full_release_creates_new_stream(
        self, fake_sounddevice: type[_FakeInputStream]
    ) -> None:
        mgr = AudioDeviceManager()
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        mgr.release_input("cw", 5)
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        assert len(fake_sounddevice.instances) == 2


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_instance_returns_same_object(self) -> None:
        assert AudioDeviceManager.instance() is AudioDeviceManager.instance()

    def test_get_audio_device_manager_matches_instance(self) -> None:
        assert adm.get_audio_device_manager() is AudioDeviceManager.instance()


# ---------------------------------------------------------------------------
# Linux/PipeWire pinning (pactl subprocess calls are mocked — no real pactl
# or Linux host required, so this runs the same on every CI platform)
# ---------------------------------------------------------------------------


class TestPinNewStream:
    def test_noop_when_no_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: set())

        def fake_run(cmd: list[str], **kwargs: Any) -> None:
            calls.append(cmd)

        monkeypatch.setattr(adm.subprocess, "run", fake_run)
        adm._pin_new_stream("sink-inputs", "move-sink-input", "target", before=set())
        assert calls == []

    def test_moves_the_single_new_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"41", "42"})

        def fake_run(cmd: list[str], **kwargs: Any) -> None:
            calls.append(cmd)

        monkeypatch.setattr(adm.subprocess, "run", fake_run)
        adm._pin_new_stream("sink-inputs", "move-sink-input", "target-sink", before={"41"})
        assert calls == [["pactl", "move-sink-input", "42", "target-sink"]]

    def test_ambiguous_new_streams_are_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"41", "42", "43"})

        def fake_run(cmd: list[str], **kwargs: Any) -> None:
            calls.append(cmd)

        monkeypatch.setattr(adm.subprocess, "run", fake_run)
        adm._pin_new_stream("sink-inputs", "move-sink-input", "target-sink", before={"41"})
        assert calls == []

    def test_gives_up_after_timeout_without_matching_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"41"})
        monkeypatch.setattr(adm.time, "sleep", lambda seconds: None)

        def fake_run(cmd: list[str], **kwargs: Any) -> None:
            calls.append(cmd)

        monkeypatch.setattr(adm.subprocess, "run", fake_run)
        adm._pin_new_stream("sink-inputs", "move-sink-input", "target-sink", before={"41"})
        assert calls == []


class TestSharedInputStreamPinning:
    def test_opens_without_pinning_when_no_target_configured(
        self, fake_sounddevice: type[_FakeInputStream], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adm, "_read_pin_targets", lambda: (None, None))
        pin_calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: pin_calls.append((a, k)))
        mgr = AudioDeviceManager()
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        assert pin_calls == []

    def test_pins_input_when_target_configured(
        self, fake_sounddevice: type[_FakeInputStream], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adm, "_read_pin_targets", lambda: (None, "my-source"))
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: set())
        pin_calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: pin_calls.append(a))
        mgr = AudioDeviceManager()
        mgr.acquire_input("cw", 5, 48_000, lambda c: None)
        assert pin_calls == [("source-outputs", "move-source-output", "my-source", set())]


class TestOutputPinning:
    def test_pin_active_output_is_noop_without_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adm, "_read_pin_targets", lambda: (None, None))
        pin_calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: pin_calls.append(a))
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("ft4", 2) is True
        mgr.pin_active_output("ft4")
        assert pin_calls == []

    def test_pin_active_output_moves_the_stream_when_target_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adm, "_read_pin_targets", lambda: ("my-sink", None))
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"99"})
        pin_calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: pin_calls.append(a))
        mgr = AudioDeviceManager()
        assert mgr.acquire_output("ft4", 2) is True
        mgr.pin_active_output("ft4")
        assert pin_calls == [("sink-inputs", "move-sink-input", "my-sink", {"99"})]

    def test_pin_active_output_only_fires_once_per_acquire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adm, "_read_pin_targets", lambda: ("my-sink", None))
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"99"})
        pin_calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: pin_calls.append(a))
        mgr = AudioDeviceManager()
        mgr.acquire_output("ft4", 2)
        mgr.pin_active_output("ft4")
        mgr.pin_active_output("ft4")  # e.g. Direwolf calling this once per audio chunk
        assert len(pin_calls) == 1

    def test_release_output_clears_pending_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adm, "_read_pin_targets", lambda: ("my-sink", None))
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"99"})
        pin_calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: pin_calls.append(a))
        mgr = AudioDeviceManager()
        mgr.acquire_output("ft4", 2)
        mgr.release_output("ft4", 2)
        mgr.pin_active_output("ft4")
        assert pin_calls == []


class TestPublicPinHelpers:
    def test_snapshot_and_pin_output_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adm, "_snapshot_stream_ids", lambda kind: {"7"})
        assert adm.snapshot_output_streams() == {"7"}

        calls: list[Any] = []
        monkeypatch.setattr(adm, "_pin_new_stream", lambda *a, **k: calls.append(a))
        adm.pin_output_stream("target-sink", before=set())
        assert calls == [("sink-inputs", "move-sink-input", "target-sink", set())]
