"""comms.qt_audio_out: WSJT-X-style Qt TX playback (format, tail, error paths)."""

from __future__ import annotations

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("PySide6.QtMultimedia")

from comms import qt_audio_out  # noqa: E402


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
