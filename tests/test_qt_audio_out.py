"""comms.qt_audio_out: WSJT-X-style Qt TX playback (format, tail, error paths)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from comms import qt_audio_out  # noqa: E402


def _qt_multimedia_available() -> bool:
    """QtMultimedia needs system libraries (e.g. libpulse) that CI may lack."""
    try:
        import PySide6.QtMultimedia  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture(autouse=True)
def _stop_audio_thread() -> object:
    yield
    qt_audio_out._shutdown()


def test_float_to_pcm16_scales_to_int16() -> None:
    audio = np.array([0.0, 1.0, -1.0, 0.5], dtype=np.float32)
    pcm = np.frombuffer(qt_audio_out.float_to_pcm16(audio), dtype="<i2")
    assert pcm.tolist() == [0, 32767, -32767, 16384]


def test_float_to_pcm16_prepends_silent_frames() -> None:
    """Modulator's m_silentFrames: audio starts at the nominal time."""
    audio = np.array([1.0, -1.0], dtype=np.float32)
    pcm = np.frombuffer(qt_audio_out.float_to_pcm16(audio, silent_frames=3), dtype="<i2")
    assert pcm.tolist() == [0, 0, 0, 32767, -32767]


def test_float_to_pcm16_skips_frames_on_late_start() -> None:
    """Modulator's m_ic: a late start drops the first frames of the wave."""
    audio = np.array([1.0, 0.5, -1.0], dtype=np.float32)
    pcm = np.frombuffer(qt_audio_out.float_to_pcm16(audio, skip_frames=2), dtype="<i2")
    assert pcm.tolist() == [-32767]


def test_float_to_pcm16_clips_overshoot() -> None:
    pcm = np.frombuffer(
        qt_audio_out.float_to_pcm16(np.array([2.0, -2.0], dtype=np.float32)), dtype="<i2"
    )
    assert pcm[0] == 32767
    assert pcm[1] == -32768


@pytest.mark.skipif(not _qt_multimedia_available(), reason="QtMultimedia unavailable")
def test_missing_output_device_reports_error(qtbot: QtBot) -> None:
    job = qt_audio_out.PlayJob(
        pcm=b"\x00\x00" * 100,
        duration_s=0.1,
        device_name="no such output device",
        get_gain=lambda: 1.0,
    )
    qt_audio_out.play_burst(job)
    assert job.done.wait(5.0)
    assert job.error is not None
    assert "not found" in job.error


def test_missing_qt_multimedia_is_reported_as_error(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without QtMultimedia the burst fails with a message instead of crashing."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PySide6.QtMultimedia":
            raise ImportError("libpulse.so.0: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    job = qt_audio_out.PlayJob(
        pcm=b"\x00\x00", duration_s=0.1, device_name=None, get_gain=lambda: 1.0
    )
    qt_audio_out.play_burst(job)
    assert job.done.wait(5.0)
    assert job.error is not None
    assert "not available" in job.error


def test_state_slot_has_no_qt_type_annotation() -> None:
    """PySide resolves slot annotations when connecting. QtMultimedia is imported
    lazily, so a QAudio.State annotation on _on_state cannot be resolved and every
    stateChanged emission failed with a TypeError (end of playback never detected)."""
    import inspect

    sig = inspect.signature(qt_audio_out._Player._on_state)
    assert "QAudio" not in str(sig.parameters["state"].annotation)
