"""Tests for the whole-recording spectrogram (sdr/overview.py)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from sdr.overview import DISPLAY_BINS, compute_overview, remove_background, to_rgb

RATE = 50_000


def _write_wav(path: Path, iq: np.ndarray, stale_header: bool = False) -> None:
    """Write a CF32 stereo WAV; optionally with a wrong (stale) data size."""
    data = np.empty(2 * len(iq), dtype="<f4")
    data[0::2], data[1::2] = iq.real, iq.imag
    raw = data.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 2, RATE, RATE * 8, 8, 32)
    size = 0 if stale_header else len(raw)
    blob = b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", size) + raw
    path.write_bytes(b"RIFF" + struct.pack("<I", len(blob)) + blob)


def _signal(seconds: float = 20.0) -> np.ndarray:
    """Noise + constant tone at +10 kHz + a 2 s burst at -5 kHz starting at t=8 s."""
    rng = np.random.default_rng(1)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    z = 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    z += 0.2 * np.exp(2j * np.pi * 10_000 * t)
    burst = (t >= 8) & (t < 10)
    z[burst] += 0.05 * np.exp(2j * np.pi * -5_000 * t[burst])
    return z.astype(np.complex64)


def _col(freq_hz: float) -> int:
    return int((freq_hz / RATE + 0.5) * DISPLAY_BINS)


def test_burst_visible_and_constant_line_removed(tmp_path: Path) -> None:
    p = tmp_path / "a.iq.wav"
    _write_wav(p, _signal())
    data = compute_overview(p)
    assert data is not None
    assert data.power_db.shape[1] == DISPLAY_BINS
    assert abs(data.duration_s - 20.0) < 0.01
    clean = remove_background(data.power_db)
    row_burst = int(9.0 / data.row_s)
    row_quiet = int(3.0 / data.row_s)
    # The burst stands out at -5 kHz; the constant +10 kHz tone is gone.
    assert clean[row_burst, _col(-5000)] - clean[row_quiet, _col(-5000)] > 15
    assert abs(clean[row_burst, _col(10000)]) < 3
    # Without removal the tone dominates the image.
    assert data.power_db[row_quiet, _col(10000)] > data.power_db[row_burst, _col(-5000)]


def test_stale_header_reads_to_real_end(tmp_path: Path) -> None:
    p = tmp_path / "b.iq.wav"
    _write_wav(p, _signal(5.0), stale_header=True)
    data = compute_overview(p)
    assert data is not None and abs(data.duration_s - 5.0) < 0.01


def test_cancel_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "c.iq.wav"
    _write_wav(p, _signal(5.0))
    assert compute_overview(p, cancelled=lambda: True) is None


def test_to_rgb_shape_and_dtype() -> None:
    rgb = to_rgb(np.random.default_rng(0).standard_normal((10, 16)), True)
    assert rgb.shape == (10, 16, 3) and rgb.dtype == np.uint8
