"""Tests for the fast (whole-file) CW decode: CwFileDecoder and CwTab.start_fast_decode().

The demodulator is replaced by a stub (the CI has no scipy): the recording is a 48 kHz
file whose real part is taken as the "audio", so window boundaries are exact.
"""

from __future__ import annotations

import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

import comms.cw.file_decoder as file_decoder
from comms.cw.codec import DecodeResult
from comms.cw.file_decoder import CwFileDecoder
from ui.cw_tab import CwTab

RATE = 48_000


class _StubDemod:
    def process(self, iq: np.ndarray) -> np.ndarray:
        return iq.real.astype(np.float32)


@pytest.fixture(autouse=True)
def _stub_demod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_decoder, "_make_demodulator", lambda _rate: _StubDemod())


def _write_wav(path: Path, seconds: float) -> None:
    n = int(seconds * RATE)
    pairs = np.zeros((n, 2), dtype="<f4")
    pairs[:, 0] = np.arange(n, dtype=np.float32)  # sample index, to see which audio a window got
    data = pairs.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 2, RATE, RATE * 8, 8, 32)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(data))
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 4 + len(chunks) + len(data)) + b"WAVE" + chunks + data
    )


def _result(audio: np.ndarray) -> DecodeResult:
    return DecodeResult(
        offsets=[], window_duration=len(audio) / RATE, frame_energy=np.zeros(0, np.float32)
    )


def _run(qtbot: QtBot, path: Path) -> tuple[list[tuple[float, float, float]], int]:
    """(per window: start s, end s, first sample index seen), and the window count."""
    seen: list[tuple[float, float, float]] = []

    def decode(audio: np.ndarray, rate: int) -> DecodeResult:
        assert rate == RATE
        seen.append((0.0, 0.0, float(audio[0])))
        return _result(audio)

    job = CwFileDecoder(path, decode)
    windows: list[tuple[float, float]] = []
    job.window_ready.connect(lambda _r, s, e: windows.append((s, e)))
    with qtbot.waitSignal(job.decoded, timeout=20000):
        job.start()
    job.wait(2000)
    out = [(s, e, seen[i][2]) for i, (s, e) in enumerate(windows)]
    return out, len(windows)


def test_windows_end_every_five_seconds_and_cover_at_most_twenty(
    qtbot: QtBot, tmp_path: Path
) -> None:
    path = tmp_path / "a.iq.wav"
    _write_wav(path, 32.0)
    out, count = _run(qtbot, path)
    ends = [e for _s, e, _f in out]
    assert ends == [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 32.0]
    starts = [s for s, _e, _f in out]
    assert starts == [0.0, 0.0, 0.0, 0.0, 5.0, 10.0, 12.0]
    # The window really starts at the audio sample it claims.
    for s, _e, first in out:
        assert first == int(s * RATE)
    assert count == 7


def test_a_recording_shorter_than_the_model_minimum_gives_no_window(
    qtbot: QtBot, tmp_path: Path
) -> None:
    path = tmp_path / "short.iq.wav"
    _write_wav(path, 3.0)
    _out, count = _run(qtbot, path)
    assert count == 0


def test_a_file_that_is_not_a_wav_fails(qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "bad.iq.wav"
    path.write_bytes(b"nope")
    job = CwFileDecoder(path, lambda a, r: _result(a))
    with qtbot.waitSignal(job.failed, timeout=5000):
        job.start()
    job.wait(2000)


def _make_tab(qtbot: Any) -> CwTab:
    tab = CwTab(sqlite3.connect(":memory:"))
    qtbot.addWidget(tab)
    tab._method_combo.setCurrentIndex(tab._method_combo.findData("classic"))
    return tab


def test_tab_fast_decode_times_windows_from_the_file_name(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rec_20260920T070000Z.iq.wav"
    _write_wav(path, 12.0)
    tab = _make_tab(qtbot)

    # Characters at 6.0 s and 11.0 s of the 12 s recording (window audio times are relative).
    def decode(audio: np.ndarray, rate: int) -> DecodeResult:
        first = float(audio[0]) / RATE
        offs = [
            (c, t)
            for c, t in (("A", 6.0 - first), ("B", 11.0 - first))
            if 0 <= t < len(audio) / RATE
        ]
        return DecodeResult(
            offsets=offs,
            window_duration=len(audio) / RATE,
            frame_energy=np.zeros(0, np.float32),
        )

    monkeypatch.setattr(tab, "_decode_function", lambda: decode)
    blocks: list[tuple[str, Any, Any]] = []
    tab.frame_block_ready.connect(lambda t, s, e: blocks.append((t, s, e)))
    reliable: list[bool] = []
    tab.frame_block_ready.connect(lambda *_a: reliable.append(tab.signal_time_reliable()))
    tab._block_extractor._min_chars = 1
    with qtbot.waitSignal(tab.fast_decode_finished, timeout=20000):
        assert tab.start_fast_decode(path)
    assert not tab.is_fast_decoding
    assert tab._start_btn.isEnabled()
    start = datetime(2026, 9, 20, 7, 0, 0, tzinfo=UTC)
    assert blocks, "no block came out"
    first = blocks[0]
    assert "A" in first[0]
    assert (first[1] - start).total_seconds() == pytest.approx(6.0, abs=1.0)
    assert all(reliable)


def test_tab_fast_decode_without_a_time_in_the_name_is_unreliable(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "noname.iq.wav"
    _write_wav(path, 8.0)
    tab = _make_tab(qtbot)
    during: list[bool] = []

    def decode(audio: np.ndarray, rate: int) -> DecodeResult:
        during.append(tab.signal_time_reliable())
        return _result(audio)

    monkeypatch.setattr(tab, "_decode_function", lambda: decode)
    with qtbot.waitSignal(tab.fast_decode_finished, timeout=20000) as blocker:
        assert tab.start_fast_decode(path)
    assert during and not any(during)
    assert "No start time" in blocker.args[0]


def test_tab_fast_decode_can_be_cancelled(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "long_20260920T070000Z.iq.wav"
    _write_wav(path, 20.0)
    tab = _make_tab(qtbot)
    monkeypatch.setattr(tab, "_decode_function", lambda: lambda a, r: _result(a))
    messages: list[str] = []
    tab.fast_decode_finished.connect(messages.append)
    assert tab.start_fast_decode(path)
    tab.stop_decoding()
    assert not tab.is_fast_decoding
    assert tab._start_btn.isEnabled()
    assert messages
