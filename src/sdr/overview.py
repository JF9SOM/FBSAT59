"""Whole-recording spectrogram ("pass overview") for IQ WAV files.

Reads a recorded .iq.wav once, without playing it back, and builds a
time x frequency power image of the whole recording. Weak, short signals
(bursts, a Doppler-drifting carrier) are usually buried under constant
lines (spurs, DC spike, neighbouring stations), so the image can be
background-subtracted: each frequency column has its own median over time
removed, which leaves only what changes during the recording.

numpy only (scipy is optional in CI, see recorder.py); the WAV header is
parsed by hand so a recording whose header size fields are stale (the
recorder rewrites them every few seconds) is still read to its real end.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# FFT length per row and the number of display columns after pooling.
FFT_SIZE = 2048
DISPLAY_BINS = 1024
# Never fewer seconds per row than this, and never more rows than this: a
# multi-hour recording is coarsened instead of producing a huge image.
MIN_ROW_S = 0.1
MAX_ROWS = 2000
# Rows transformed per batch (bounds memory and sets the progress cadence).
_BATCH_ROWS = 64


# Display modes. STANDARD keeps the original one-FFT-per-row image. The other two
# average every FFT inside a row (STANDARD only looks at ~8 ms of each 0.1 s row, so
# a 200 ms burst at +5 dB S/N is lost in the per-bin noise scatter):
#   WIDE   wideband signals (9k6 FSK bursts): rows are also smoothed across ~8 kHz.
#   NARROW carriers / CW / Doppler-drifting lines: 15 Hz bins, peak-held when pooled.
MODE_STANDARD = "standard"
MODE_WIDE = "wide"
MODE_NARROW = "narrow"
MODES = (MODE_STANDARD, MODE_WIDE, MODE_NARROW)

# WIDE: smoothing width in display bins (~8 kHz at 250 kHz / 1024 bins).
_WIDE_SMOOTH_BINS = 33
# NARROW: FFT length (15 Hz bins at 250 kHz) and the shortest row.
_NARROW_FFT_SIZE = 16384
_NARROW_MIN_ROW_S = 0.5
# Colour span above the noise floor for the averaged modes (dB), and the percentile
# of the image taken as the floor. The span never follows the strongest pixels, so a
# strong signal / impulse noise cannot stretch the scale over a weak signal. NARROW
# uses a fixed span. WIDE ties it to the measured pixel noise (a robust sigma that is
# only ~0.2 dB after the averaging): span = _WIDE_SPAN_SIGMAS * sigma, kept within
# [_WIDE_SPAN_MIN_DB, _WIDE_SPAN_MAX_DB], which lets a +2 dB burst fill half the palette.
_SPAN_DB = {MODE_WIDE: 6.0, MODE_NARROW: 9.0}
_FLOOR_PERCENTILE = {MODE_WIDE: 20.0, MODE_NARROW: 50.0}
_WIDE_SPAN_SIGMAS = 15.0
_WIDE_SPAN_MIN_DB = 3.0
_WIDE_SPAN_MAX_DB = 6.0
# WIDE rows are as short as 0.1 s (a 250 ms burst is not diluted into a long row).
_WIDE_MAX_ROWS = 7000


@dataclass
class OverviewData:
    """Result of :func:`compute_overview`.

    ``power_db`` is (rows, DISPLAY_BINS) in dB, frequency ascending from
    ``-sample_rate/2`` to ``+sample_rate/2`` relative to the recording's
    centre. ``row_s`` is the time step between rows.
    """

    power_db: np.ndarray
    sample_rate: float
    row_s: float
    duration_s: float
    mode: str = MODE_STANDARD


def _open_iq(path: Path) -> tuple[np.memmap, float]:
    """Return (interleaved sample memmap, sample rate) of a 2-channel WAV."""
    with open(path, "rb") as fh:
        head = fh.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        fmt: tuple[int, int, int, int] | None = None
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                raise ValueError("no data chunk")
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"fmt ":
                tag, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", fh.read(16))
                fmt = (tag, ch, rate, bits)
                fh.seek(size - 16, 1)
            elif cid == b"data":
                offset = fh.tell()
                break
            else:
                fh.seek(size + (size & 1), 1)
    if fmt is None:
        raise ValueError("no fmt chunk")
    tag, ch, rate, bits = fmt
    if ch != 2:
        raise ValueError("not a 2-channel IQ WAV")
    if tag == 3 and bits == 32:
        dtype: str = "<f4"
    elif tag == 1 and bits == 16:
        dtype = "<i2"
    else:
        raise ValueError("unsupported WAV sample format")
    # Use the real file size, not the header's data size (stale while recording).
    item = np.dtype(dtype).itemsize
    n_items = (path.stat().st_size - offset) // item
    n_items -= n_items % 2
    if n_items <= 0:
        raise ValueError("empty recording")
    return np.memmap(path, dtype=dtype, mode="r", offset=offset, shape=(n_items,)), float(rate)


def _box_smooth(x: np.ndarray, width: int) -> np.ndarray:
    """Moving average of *width* bins along the last axis (edges use the bins present)."""
    half = width // 2
    pad = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(half + 1, half)])
    cs = np.cumsum(pad, axis=-1, dtype=np.float64)
    total = cs[..., width:] - cs[..., :-width]
    ones = np.pad(np.ones(x.shape[-1]), (half + 1, half))
    cnt = np.cumsum(ones)
    count = cnt[width:] - cnt[:-width]
    return np.asarray((total / count).astype(np.float32))


def _compute_averaged(
    mm: np.ndarray,
    rate: float,
    mode: str,
    progress: Callable[[float], None] | None,
    cancelled: Callable[[], bool] | None,
) -> OverviewData | None:
    """WIDE / NARROW image: every FFT inside a row is averaged (power domain)."""
    scale = 1.0 if mm.dtype.kind == "f" else 1.0 / 32768.0
    n_samples = len(mm) // 2
    duration = n_samples / rate
    fft_size = _NARROW_FFT_SIZE if mode == MODE_NARROW else FFT_SIZE
    min_row_s = _NARROW_MIN_ROW_S if mode == MODE_NARROW else MIN_ROW_S
    max_rows = _WIDE_MAX_ROWS if mode == MODE_WIDE else MAX_ROWS
    hop = max(fft_size, int(max(min_row_s, duration / max_rows) * rate))
    n_rows = max(1, n_samples // hop)
    window = np.hanning(fft_size).astype(np.float32)
    pool = fft_size // DISPLAY_BINS
    rows = np.empty((n_rows, DISPLAY_BINS), dtype=np.float32)
    for r in range(n_rows):
        if cancelled is not None and cancelled():
            return None
        a = np.asarray(mm[2 * r * hop : 2 * min((r + 1) * hop, n_samples)], dtype=np.float32)
        iq = (a[0::2] + 1j * a[1::2]) * scale
        k = max(1, len(iq) // fft_size)
        if len(iq) < k * fft_size:
            iq = np.pad(iq, (0, k * fft_size - len(iq)))
        seg = iq[: k * fft_size].reshape(k, fft_size) * window
        spec = np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)
        p = (spec.real**2 + spec.imag**2).mean(axis=0).reshape(DISPLAY_BINS, pool)
        # NARROW keeps the peak of each pooled group so a 15 Hz line is not diluted.
        rows[r] = p.max(axis=1) if mode == MODE_NARROW else p.mean(axis=1)
        if progress is not None and r % 16 == 0:
            progress(min(1.0, (r + 1) / n_rows))
    if mode == MODE_WIDE:
        rows = _box_smooth(rows, _WIDE_SMOOTH_BINS)
    rows = 10.0 * np.log10(rows + 1e-20)
    return OverviewData(rows.astype(np.float32), rate, hop / rate, duration, mode)


def compute_overview(
    path: Path,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    mode: str = MODE_STANDARD,
) -> OverviewData | None:
    """Build the whole-recording spectrogram; None if *cancelled* returned True."""
    mm, rate = _open_iq(path)
    if mode in (MODE_WIDE, MODE_NARROW):
        return _compute_averaged(mm, rate, mode, progress, cancelled)
    scale = 1.0 if mm.dtype.kind == "f" else 1.0 / 32768.0
    n_samples = len(mm) // 2
    duration = n_samples / rate
    row_s = max(MIN_ROW_S, duration / MAX_ROWS)
    hop = max(1, int(row_s * rate))
    starts = np.arange(0, max(1, n_samples - FFT_SIZE + 1), hop)
    window = np.hanning(FFT_SIZE).astype(np.float32)
    rows = np.empty((len(starts), DISPLAY_BINS), dtype=np.float32)
    pool = FFT_SIZE // DISPLAY_BINS

    for b0 in range(0, len(starts), _BATCH_ROWS):
        if cancelled is not None and cancelled():
            return None
        idx = starts[b0 : b0 + _BATCH_ROWS]
        block = np.empty((len(idx), FFT_SIZE), dtype=np.complex64)
        for k, s in enumerate(idx):
            a = np.asarray(mm[2 * s : 2 * (s + FFT_SIZE)], dtype=np.float32) * scale
            iq = a[0::2] + 1j * a[1::2]
            if len(iq) < FFT_SIZE:
                iq = np.pad(iq, (0, FFT_SIZE - len(iq)))
            block[k] = iq
        spec = np.fft.fftshift(np.fft.fft(block * window, axis=1), axes=1)
        p = (spec.real**2 + spec.imag**2).reshape(len(idx), DISPLAY_BINS, pool).mean(axis=2)
        rows[b0 : b0 + len(idx)] = 10.0 * np.log10(p + 1e-20)
        if progress is not None:
            progress(min(1.0, (b0 + len(idx)) / len(starts)))
    return OverviewData(rows, rate, hop / rate, duration)


def remove_background(power_db: np.ndarray, mode: str = MODE_STANDARD) -> np.ndarray:
    """Subtract each frequency column's median over time (constant lines vanish).

    The averaged modes also subtract each row's median, which suppresses the
    horizontal stripes that wideband impulse noise leaves across a whole row.
    """
    out = np.asarray(power_db - np.median(power_db, axis=0, keepdims=True))
    if mode in (MODE_WIDE, MODE_NARROW):
        out = out - np.median(out, axis=1, keepdims=True)
    return np.asarray(out)


# Anchor colours of a dark-blue -> teal -> green -> yellow map (viridis-like).
_STOPS = np.array(
    [
        [68, 1, 84],
        [59, 82, 139],
        [33, 145, 140],
        [94, 201, 98],
        [253, 231, 37],
    ],
    dtype=np.float32,
)


def _span_db(power_db: np.ndarray, mode: str) -> float:
    """Colour span (dB) above the floor for an averaged mode."""
    if mode != MODE_WIDE:
        return _SPAN_DB[mode]
    sigma = 1.4826 * float(np.median(np.abs(power_db - np.median(power_db))))
    return float(np.clip(_WIDE_SPAN_SIGMAS * sigma, _WIDE_SPAN_MIN_DB, _WIDE_SPAN_MAX_DB))


def to_rgb(power_db: np.ndarray, background_removed: bool, mode: str = MODE_STANDARD) -> np.ndarray:
    """Map dB values to an (rows, cols, 3) uint8 image.

    The colour range follows the data (low/high percentiles) so that weak
    bursts stay visible whatever the absolute level of the recording is. The
    averaged modes (with the background removed) anchor the low end at the
    noise floor and use a fixed span above it instead of a high percentile.
    """
    if background_removed and mode in _SPAN_DB:
        lo = float(np.percentile(power_db, _FLOOR_PERCENTILE[mode]))
        hi = lo + _span_db(power_db, mode)
    else:
        lo_p, hi_p = (10.0, 99.95) if background_removed else (5.0, 99.8)
        lo, hi = np.percentile(power_db, [lo_p, hi_p])
    if hi - lo < 1.0:
        hi = lo + 1.0
    t = np.clip((power_db - lo) / (hi - lo), 0.0, 1.0) * (len(_STOPS) - 1)
    i0 = np.minimum(t.astype(np.int32), len(_STOPS) - 2)
    f = (t - i0)[..., None]
    rgb = _STOPS[i0] * (1.0 - f) + _STOPS[i0 + 1] * f
    return np.ascontiguousarray(rgb.astype(np.uint8))
