"""Load a pre-recorded audio file (MP3/WAV/...) as mono PCM for SstvDecoder.

Used by the SSTV tab's "Decode Recording…" button to reconstruct an image
from an MP3 saved by Radio Control's or SDR Control's rig-audio recorder,
without needing WSJT-X/GQRX-style real-time playback — the whole file is
fed to SstvDecoder.push_samples() in one call, which is faster than
real-time since decoding is just numpy/scipy vector math, not audio I/O.
"""

from __future__ import annotations

from typing import cast

import numpy as np
from numpy.typing import NDArray

try:
    import soundfile as _soundfile

    SOUNDFILE_AVAILABLE: bool = True
except ImportError:
    _soundfile = None
    SOUNDFILE_AVAILABLE = False

try:
    from scipy import signal as _sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    _sp_signal = None
    _SCIPY_AVAILABLE = False


def _resample(data: NDArray[np.float32], src_rate: int, dst_rate: int) -> NDArray[np.float32]:
    """Resample mono float32 PCM from src_rate to dst_rate (anti-aliased)."""
    if src_rate == dst_rate or len(data) == 0:
        return data
    if _SCIPY_AVAILABLE and _sp_signal is not None:
        g = np.gcd(src_rate, dst_rate)
        resampled = _sp_signal.resample_poly(data, dst_rate // g, src_rate // g)
        return cast(NDArray[np.float32], resampled.astype(np.float32))
    n_out = max(1, round(len(data) * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, data).astype(np.float32)


def load_audio_mono(path: str, target_rate: int) -> NDArray[np.float32]:
    """Read an audio file and return float32 mono PCM resampled to target_rate.

    Raises RuntimeError if soundfile is not installed, or whatever exception
    soundfile itself raises for an unreadable/unsupported file.
    """
    if not SOUNDFILE_AVAILABLE or _soundfile is None:
        raise RuntimeError("soundfile is not installed. Run: pip install soundfile")
    data, samplerate = _soundfile.read(path, dtype="float32", always_2d=False)
    mono: NDArray[np.float32] = data.astype(np.float32)
    if mono.ndim == 2:
        mono = mono.mean(axis=1).astype(np.float32)
    return _resample(mono, int(samplerate), target_rate)
