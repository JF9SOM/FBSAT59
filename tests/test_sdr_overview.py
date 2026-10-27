"""Tests for the whole-recording spectrogram (sdr/overview.py)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from pytestqt.qtbot import QtBot

from sdr.overview import (
    DISPLAY_BINS,
    MODE_NARROW,
    MODE_STANDARD,
    MODE_WIDE,
    compute_overview,
    remove_background,
    to_rgb,
)

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


def test_click_in_overview_requests_seek(tmp_path: Path, qtbot: QtBot) -> None:
    """A click at the vertical middle of the image asks to seek to mid-recording."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from ui.sdr_overview_dialog import SdrOverviewDialog

    p = tmp_path / "d.iq.wav"
    _write_wav(p, _signal(10.0))
    dlg = SdrOverviewDialog(p)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: dlg._data is not None, timeout=10000)
    canvas = dlg._canvas
    r = canvas._plot_rect(canvas.width(), canvas.height())
    got: list[tuple[float, float, bool]] = []
    dlg.seek_requested.connect(lambda t, f, with_offset: got.append((t, f, with_offset)))
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(r.center().x(), r.center().y()))
    assert len(got) == 1 and abs(got[0][0] - 5.0) < 0.2
    # Horizontal middle of the image = the recording's centre frequency.
    assert abs(got[0][1]) < 1000.0
    # The "On click" menu starts on "time and offset".
    assert got[0][2] is True

    # Switching the menu to "time only" is reported with the click and as a signal.
    from ui.sdr_overview_dialog import CLICK_TIME_ONLY

    modes: list[int] = []
    dlg.click_mode_changed.connect(modes.append)
    dlg._click_combo.setCurrentIndex(dlg._click_combo.findData(CLICK_TIME_ONLY))
    assert modes == [CLICK_TIME_ONLY]
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(r.center().x(), r.center().y()))
    assert got[-1][2] is False


def _weak_signal() -> np.ndarray:
    """Noise + a weak 3 s wideband burst at -5 kHz (t=10 s) + a weak tone at +7 kHz (18-24 s)."""
    rng = np.random.default_rng(3)
    n = 30 * RATE
    t = np.arange(n) / RATE
    z = 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    b = (t >= 10) & (t < 13)
    w = rng.standard_normal(int(b.sum())) + 1j * rng.standard_normal(int(b.sum()))
    spec = np.fft.fft(w)
    spec[np.abs(np.fft.fftfreq(len(w), 1 / RATE)) > 2000] = 0
    w = np.fft.ifft(spec)
    w *= np.sqrt(3.2e-5 / np.mean(np.abs(w) ** 2))
    z[b] += w * np.exp(-2j * np.pi * 5000 * t[b])
    c = (t >= 18) & (t < 24)
    z[c] += 0.002 * np.exp(2j * np.pi * 7000 * t[c])
    return z.astype(np.complex64)


def _zscores(path: Path, mode: str) -> tuple[float, float]:
    """(wideband burst, narrow tone) contrast over the quiet rows, in noise sigmas."""
    data = compute_overview(path, mode=mode)
    assert data is not None and data.mode == mode
    clean = remove_background(data.power_db, mode)
    quiet = slice(int(2 / data.row_s), int(8 / data.row_s))
    burst = slice(int(10.3 / data.row_s), int(12.7 / data.row_s))
    cols = slice(_col(-6000), _col(-4000))
    zb = (clean[burst, cols].mean() - clean[quiet, cols].mean()) / clean[quiet, cols].mean(1).std()
    tone = slice(int(18.5 / data.row_s), int(23.5 / data.row_s))
    tc = slice(_col(7000) - 1, _col(7000) + 2)
    zt = (clean[tone, tc].max(1).mean() - clean[quiet, tc].max(1).mean()) / clean[quiet, tc].max(
        1
    ).std()
    return float(zb), float(zt)


def test_averaged_modes_show_weak_signals_better(tmp_path: Path) -> None:
    p = tmp_path / "w.iq.wav"
    _write_wav(p, _weak_signal())
    std_b, std_t = _zscores(p, MODE_STANDARD)
    wide_b, _wide_t = _zscores(p, MODE_WIDE)
    _nar_b, nar_t = _zscores(p, MODE_NARROW)
    assert wide_b > std_b * 1.3  # the wideband burst stands out more
    assert nar_t > std_t * 2.0  # the weak carrier stands out much more


def test_averaged_modes_shape_and_rgb(tmp_path: Path) -> None:
    p = tmp_path / "s.iq.wav"
    _write_wav(p, _signal(6.0))
    for mode in (MODE_WIDE, MODE_NARROW):
        data = compute_overview(p, mode=mode)
        assert data is not None
        assert data.power_db.shape[1] == DISPLAY_BINS and np.isfinite(data.power_db).all()
        assert abs(data.duration_s - 6.0) < 0.01
        for bg in (True, False):
            db = remove_background(data.power_db, mode) if bg else data.power_db
            rgb = to_rgb(db, bg, mode)
            assert rgb.shape == (*data.power_db.shape, 3) and rgb.dtype == np.uint8
    assert compute_overview(p, cancelled=lambda: True, mode=MODE_WIDE) is None


def test_mode_combo_switches_and_caches(tmp_path: Path, qtbot: QtBot) -> None:
    from ui.sdr_overview_dialog import SdrOverviewDialog

    p = tmp_path / "m.iq.wav"
    _write_wav(p, _signal(6.0))
    dlg = SdrOverviewDialog(p)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: dlg._data is not None, timeout=10000)
    assert dlg._mode_combo.currentData() == MODE_STANDARD and dlg._data.mode == MODE_STANDARD
    standard = dlg._data
    dlg._mode_combo.setCurrentIndex(dlg._mode_combo.findData(MODE_NARROW))
    qtbot.waitUntil(lambda: dlg._data is not None and dlg._data.mode == MODE_NARROW, timeout=10000)
    # Back to Standard comes from the cache, without a new read.
    dlg._mode_combo.setCurrentIndex(dlg._mode_combo.findData(MODE_STANDARD))
    assert dlg._data is standard


def test_wide_colour_span_shows_a_2_db_burst() -> None:
    """After averaging, noise is ~0.2 dB, so a +2 dB patch must fill about half the palette."""
    rng = np.random.default_rng(5)
    db = rng.normal(0.0, 0.2, (400, 300)).astype(np.float32)
    db[200:210, 100:140] += 2.0
    rgb = to_rgb(db, True, MODE_WIDE).astype(int)
    patch = rgb[200:210, 100:140].reshape(-1, 3).mean(0)
    quiet = rgb[20:100, 20:80].reshape(-1, 3).mean(0)
    assert patch.sum() - quiet.sum() > 150  # clearly brighter than the noise background


def test_wide_colour_span_ignores_one_strong_pixel() -> None:
    """A single very strong outlier must not stretch the scale over the weak burst."""
    rng = np.random.default_rng(6)
    db = rng.normal(0.0, 0.2, (400, 300)).astype(np.float32)
    db[200:210, 100:140] += 2.0
    base = to_rgb(db, True, MODE_WIDE).astype(int)[200:210, 100:140].mean()
    db[5, 5] = 80.0
    with_outlier = to_rgb(db, True, MODE_WIDE).astype(int)[200:210, 100:140].mean()
    assert abs(with_outlier - base) < 1.0


def test_max_pool_keeps_a_single_bright_row() -> None:
    from ui.sdr_overview_dialog import _max_pool_rgb

    rgb = np.zeros((1000, 300, 3), dtype=np.uint8)
    rgb[437, 120] = (250, 230, 40)  # one pixel in one row
    small = _max_pool_rgb(rgb, 100, 100)
    assert small.shape == (100, 100, 3)
    assert small.reshape(-1, 3).max(0).tolist() == [250, 230, 40]
    # A recording already smaller than the target is left alone.
    assert _max_pool_rgb(rgb[:50, :60], 100, 100).shape == (50, 60, 3)
