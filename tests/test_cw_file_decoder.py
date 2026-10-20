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
from comms.cw.file_decoder import CwFileDecoder, find_cw_carrier_hz
from comms.telemetry.iq_wav import read_wav_info
from ui.cw_tab import CwTab

RATE = 48_000


class _StubDemod:
    def process(self, iq: np.ndarray) -> np.ndarray:
        return iq.real.astype(np.float32)


@pytest.fixture(autouse=True)
def _stub_demod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_decoder, "_make_demodulator", lambda _rate: _StubDemod())
    # The recordings below are ramps, not signals: no carrier search (it has its own tests).
    monkeypatch.setattr(file_decoder, "find_cw_carrier_hz", lambda _path, _info: None)


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


def test_tab_speed_choice_decides_between_replay_and_file_dialog(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QObject, Signal

    class _Sdr(QObject):
        overview_seek_done = Signal(float)
        opened = 0

        def open_recording(self) -> None:
            self.opened += 1

    class _Radio(QObject):
        def __init__(self) -> None:
            super().__init__()
            self._sdr_control = _Sdr()

    radio = _Radio()
    tab = CwTab(sqlite3.connect(":memory:"), radio)
    qtbot.addWidget(tab)
    monkeypatch.setattr(tab, "_start", lambda: None)
    dialogs: list[int] = []
    monkeypatch.setattr(
        "ui.cw_tab.QFileDialog.getOpenFileName",
        lambda *a, **k: (dialogs.append(1), "")[1:] and ("", ""),
    )
    # Normal: SDR Control opens the file; decoding starts when the overview is clicked.
    assert tab._speed_combo.currentData() == "normal"
    tab._fast_btn.click()
    assert radio._sdr_control.opened == 1 and not dialogs
    radio._sdr_control.overview_seek_done.emit(10.0)
    assert tab._start_btn.isChecked()
    tab._start_btn.setChecked(False)
    # Fast: our own file dialog.
    tab._speed_combo.setCurrentIndex(tab._speed_combo.findData("fast"))
    tab._fast_btn.click()
    assert dialogs and radio._sdr_control.opened == 1


def _write_iq(path: Path, iq: np.ndarray, rate: int) -> None:
    pairs = np.stack([iq.real, iq.imag], axis=1).astype("<f4")
    data = pairs.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 2, rate, rate * 8, 8, 32)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(data))
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 4 + len(chunks) + len(data)) + b"WAVE" + chunks + data
    )


def _keyed_tone(rate: int, seconds: float, freq: float, steady_hz: float | None = None) -> Any:
    """Noise plus a tone keyed 0.4 s on / 0.4 s off (and optionally a constant spur)."""
    rng = np.random.default_rng(1)
    n = int(rate * seconds)
    t = np.arange(n) / rate
    iq = 0.02 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    key = (np.floor(t / 0.4) % 2 == 0).astype(np.float64)
    iq = iq + 0.3 * key * np.exp(2j * np.pi * freq * t)
    if steady_hz is not None:
        iq = iq + 0.3 * np.exp(2j * np.pi * steady_hz * t)
    return iq.astype(np.complex64)


@pytest.mark.parametrize("freq", [0.0, 2500.0, -37000.0])
def test_find_carrier_locates_keyed_line_and_ignores_constant_spur(
    tmp_path: Path, freq: float
) -> None:
    rate = 250_000
    path = tmp_path / "c.iq.wav"
    _write_iq(path, _keyed_tone(rate, 12.0, freq, steady_hz=60_000.0), rate)
    found = find_cw_carrier_hz(path, read_wav_info(path))
    assert found is not None
    assert abs(found - freq) < 80.0


def test_find_carrier_returns_none_for_noise_only(tmp_path: Path) -> None:
    rate = 250_000
    path = tmp_path / "n.iq.wav"
    rng = np.random.default_rng(2)
    n = rate * 6
    _write_iq(
        path, (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64), rate
    )
    assert find_cw_carrier_hz(path, read_wav_info(path)) is None


def test_fast_decode_moves_the_found_line_to_the_target_pitch(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rate = 48_000
    path = tmp_path / "s.iq.wav"
    n = rate * 8
    t = np.arange(n) / rate
    _write_iq(path, np.exp(2j * np.pi * 0.0 * t).astype(np.complex64), rate)  # DC "line"
    monkeypatch.setattr(file_decoder, "find_cw_carrier_hz", lambda _path, _info: 0.0)
    seen: list[np.ndarray] = []

    def decode(audio: np.ndarray, _rate: int) -> DecodeResult:
        seen.append(audio.copy())
        return _result(audio)

    job = CwFileDecoder(path, decode)
    with qtbot.waitSignal(job.decoded, timeout=20000):
        job.start()
    job.wait(2000)
    assert seen
    audio = seen[0]
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    peak_hz = float(np.argmax(spectrum)) * RATE / len(audio)
    assert abs(peak_hz - file_decoder.CW_TARGET_PITCH_HZ) < 5.0
