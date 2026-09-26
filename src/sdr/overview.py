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


def compute_overview(
    path: Path,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> OverviewData | None:
    """Build the whole-recording spectrogram; None if *cancelled* returned True."""
    mm, rate = _open_iq(path)
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


def remove_background(power_db: np.ndarray) -> np.ndarray:
    """Subtract each frequency column's median over time (constant lines vanish)."""
    return np.asarray(power_db - np.median(power_db, axis=0, keepdims=True))


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


def to_rgb(power_db: np.ndarray, background_removed: bool) -> np.ndarray:
    """Map dB values to an (rows, cols, 3) uint8 image.

    The colour range follows the data (low/high percentiles) so that weak
    bursts stay visible whatever the absolute level of the recording is.
    """
    lo_p, hi_p = (10.0, 99.95) if background_removed else (5.0, 99.8)
    lo, hi = np.percentile(power_db, [lo_p, hi_p])
    if hi - lo < 1.0:
        hi = lo + 1.0
    t = np.clip((power_db - lo) / (hi - lo), 0.0, 1.0) * (len(_STOPS) - 1)
    i0 = np.minimum(t.astype(np.int32), len(_STOPS) - 2)
    f = (t - i0)[..., None]
    rgb = _STOPS[i0] * (1.0 - f) + _STOPS[i0 + 1] * f
    return np.ascontiguousarray(rgb.astype(np.uint8))
