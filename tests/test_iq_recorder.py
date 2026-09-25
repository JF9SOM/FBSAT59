"""Tests for the streaming IQ recorder (crash-safe WAV writing)."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sdr import recorder as rec_mod
from sdr.recorder import IQRecorder, _StreamingWavWriter


def _read_header(path: Path) -> tuple[int, int, int, int]:
    """Return (riff_size, sample_rate, channels, data_size) from a CF32 WAV."""
    raw = path.read_bytes()[:56]
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    riff_size = struct.unpack("<I", raw[4:8])[0]
    fmt_tag, channels, rate = struct.unpack("<HHI", raw[20:28])
    assert fmt_tag == 3
    assert raw[36:40] == b"fact" and raw[48:52] == b"data"
    data_size = struct.unpack("<I", raw[52:56])[0]
    return riff_size, rate, channels, data_size


def test_stop_finalises_header_and_data(tmp_path: Path) -> None:
    rec = IQRecorder(save_dir=tmp_path)
    path = rec.start(sample_rate=250_000, norad=1, sat_name="T")
    iq = (np.arange(1000) + 1j * np.arange(1000)).astype(np.complex64)
    rec.put_samples(iq)
    rec.put_samples(iq)
    rec.stop()
    riff, rate, ch, data = _read_header(path)
    assert (rate, ch, data) == (250_000, 2, 2000 * 8)
    assert riff == 56 - 8 + data
    assert path.stat().st_size == 56 + data
    body = np.frombuffer(path.read_bytes()[56:], dtype=np.float32).reshape(-1, 2)
    assert body[5, 0] == 5.0 and body[5, 1] == 5.0


def test_no_samples_leaves_no_file(tmp_path: Path) -> None:
    rec = IQRecorder(save_dir=tmp_path)
    rec.start(sample_rate=250_000)
    rec.stop()
    assert list(tmp_path.iterdir()) == []


def test_data_survives_without_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A writer that is never closed (crash) still leaves data + a valid header."""
    monkeypatch.setattr(rec_mod, "_HEADER_REFRESH_S", 0.0)
    path = tmp_path / "crash.iq.wav"
    w = _StreamingWavWriter(path, 48_000)
    w.write(np.ones(800, dtype=np.float32))
    w.write(np.ones(800, dtype=np.float32))  # triggers a header refresh + flush
    _, rate, ch, data = _read_header(path)  # no close() called
    assert (rate, ch) == (48_000, 2)
    assert data == 1600 * 4


def test_cached_rotator_position_does_not_wait_for_io_lock() -> None:
    """get_cached_position() must return even while the I/O lock is held."""
    import threading

    from rig.controller import HamlibRotatorController

    ctrl = HamlibRotatorController(net_mode=True)
    with ctrl._lock:
        ctrl._rotor_state.azimuth_deg = 123.4
        ctrl._rotor_state.elevation_deg = 45.6
    result: list[float] = []
    with ctrl._io_lock:  # simulate a background transaction stuck on I/O

        def _read() -> None:
            result.append(ctrl.get_cached_position().azimuth_deg)

        t = threading.Thread(target=_read)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()
    assert result == [123.4]


def test_unchanged_sdr_settings_keep_the_live_adapter() -> None:
    from types import SimpleNamespace

    from ui.main_window import MainWindow

    live = SimpleNamespace(is_sdr=True, is_connected=True)
    cfg = {"device_label": "HackRF", "gain_db": 110}
    check: Any = MainWindow._sdr_adapter_unchanged
    assert check(live, dict(cfg), dict(cfg))
    assert not check(live, dict(cfg), {**cfg, "gain_db": 40})
    assert not check(live, None, dict(cfg))
    assert not check(SimpleNamespace(is_sdr=True, is_connected=False), dict(cfg), dict(cfg))
    assert not check(SimpleNamespace(is_sdr=False, is_connected=True), dict(cfg), dict(cfg))
    assert not check(None, dict(cfg), dict(cfg))
