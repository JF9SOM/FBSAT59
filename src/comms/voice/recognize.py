"""Speech-to-text for decoded voice messages (optional: faster-whisper).

faster-whisper is not a dependency of the app. When it is installed
(``pip install faster-whisper``) the Digital Voice tab can recognise a message; the Whisper
model (``small``, about 460 MB) is downloaded on first use into the user data folder.
CODEC2 700C is a very low bit rate codec, so expect an approximate result.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import numpy as np

MODEL_NAME = "small"
INPUT_RATE = 8000
_WHISPER_RATE = 16000


def is_available() -> bool:
    """True if faster-whisper is installed."""
    return importlib.util.find_spec("faster_whisper") is not None


def model_dir() -> Path:
    """Where the Whisper model is cached."""
    from platformdirs import user_data_dir

    return Path(user_data_dir("fbsat59")) / "whisper"


def to_whisper_audio(pcm: np.ndarray) -> np.ndarray:
    """int16 8 kHz mono -> float32 16 kHz in [-1, 1] (passed as an array, so no file decoder)."""
    x = np.asarray(pcm, dtype=np.float32) / 32768.0
    n = len(x)
    if n == 0:
        return x
    t_out = np.arange(n * 2) / 2.0
    return np.asarray(np.interp(t_out, np.arange(n), x), dtype=np.float32)


_model: Any = None


def transcribe(pcm: np.ndarray, language: str | None = None) -> tuple[str, str]:
    """Recognise *pcm* (int16, 8 kHz). Returns ``(language_code, text)``.

    *language* None lets Whisper detect it. Raises RuntimeError if faster-whisper is missing.
    """
    global _model
    if not is_available():
        raise RuntimeError("faster-whisper is not installed (pip install faster-whisper)")
    from faster_whisper import WhisperModel

    if _model is None:
        folder = model_dir()
        folder.mkdir(parents=True, exist_ok=True)
        _model = WhisperModel(
            MODEL_NAME, device="cpu", compute_type="int8", download_root=str(folder)
        )
    segments, info = _model.transcribe(
        to_whisper_audio(pcm), language=language, beam_size=5, vad_filter=False
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return str(info.language), text
