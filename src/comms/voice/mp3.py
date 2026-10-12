"""MP3 export of decoded voice (lameenc, already a dependency of SDR audio recording)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import lameenc

    LAMEENC_AVAILABLE: bool = True
except ImportError:
    lameenc = None
    LAMEENC_AVAILABLE = False

OUT_RATE = 16000  # players handle 16 kHz MP3 everywhere; 8 kHz is not universal


def save_mp3(path: Path, pcm: np.ndarray, sample_rate: int = 8000) -> None:
    """Write mono int16 *pcm* as a 64 kbit/s MP3 (resampled to 16 kHz by LAME)."""
    if not LAMEENC_AVAILABLE:
        raise RuntimeError("lameenc is not installed")
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(sample_rate)
    enc.set_out_sample_rate(OUT_RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    data = bytes(enc.encode(np.asarray(pcm, dtype=np.int16).tobytes())) + bytes(enc.flush())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
