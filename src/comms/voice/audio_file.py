"""Load an audio file (MP3/WAV/...) as the 8 kHz mono int16 audio the voice tab works with."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from comms.voice.codec2 import SAMPLE_RATE

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

try:
    import soundfile as _soundfile

    SOUNDFILE_AVAILABLE: bool = True
except (ImportError, OSError):
    _soundfile = None
    SOUNDFILE_AVAILABLE = False


def resample(x: np.ndarray, rate: float, target: int = SAMPLE_RATE) -> np.ndarray:
    """Resample mono float audio *x* from *rate* Hz to *target* Hz."""
    if rate == target or len(x) == 0:
        return x
    if _SCIPY_AVAILABLE:
        g = math.gcd(int(rate), int(target))
        return np.asarray(sp_signal.resample_poly(x, target // g, rate // g), dtype=np.float32)
    # Without scipy: low-pass by averaging when going down, then linear interpolation.
    if rate > target:
        k = max(1, int(round(rate / target)))
        usable = len(x) // k * k
        x = x[:usable].reshape(-1, k).mean(axis=1)
        rate = rate / k
    positions = np.arange(int(len(x) * target / rate)) * rate / target
    return np.asarray(np.interp(positions, np.arange(len(x)), x), dtype=np.float32)


def load_audio_file(path: str | Path) -> np.ndarray:
    """Read *path* and return mono int16 audio at 8 kHz (any input rate or channel count).

    Raises RuntimeError if soundfile is missing, or whatever soundfile raises for an
    unreadable file; ValueError if the file holds no audio.
    """
    if not SOUNDFILE_AVAILABLE or _soundfile is None:
        raise RuntimeError("soundfile is not installed. Run: pip install soundfile")
    data, rate = _soundfile.read(str(path), dtype="float32", always_2d=True)
    if data.shape[0] == 0:
        raise ValueError("the file holds no audio")
    mono = data.mean(axis=1)
    pcm = np.clip(resample(mono, int(rate)), -1.0, 1.0)
    return np.asarray(np.round(pcm * 32767.0), dtype=np.int16)
