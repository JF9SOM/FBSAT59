"""
AudioRecorder — records demodulated PCM audio to MP3.

Receives float32 PCM blocks from SDRPipeline.audio_ready signal and encodes
them to MP3 using lameenc (pure-Python, no external tools required).

Usage:
    rec = AudioRecorder(save_dir, sample_rate=48000)
    rec.start(norad=25544, sat_name="ISS")   # returns Path
    # ... pipeline.audio_ready connected to rec.put_pcm ...
    rec.stop()
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import IO, Any

import numpy as np

logger = logging.getLogger(__name__)

try:
    import lameenc as _lameenc

    LAMEENC_AVAILABLE = True
except ImportError:
    _lameenc = None
    LAMEENC_AVAILABLE = False
    logger.warning("lameenc not installed — audio recording unavailable. pip install lameenc")


class AudioRecorder:
    """Encodes incoming PCM blocks to an MP3 file via lameenc."""

    def __init__(self, save_dir: Path, sample_rate: int = 48_000) -> None:
        self._save_dir = save_dir
        self._sample_rate = sample_rate
        self._encoder: Any = None  # lameenc.Encoder | None
        self._file: IO[bytes] | None = None
        self._file_path: Path | None = None
        self._start_time: float = 0.0
        self._bytes_written: int = 0
        self._active = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def elapsed_seconds(self) -> float:
        return (time.monotonic() - self._start_time) if self._active else 0.0

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def set_save_dir(self, save_dir: Path) -> None:
        """Change the folder used by the next recording."""
        self._save_dir = save_dir

    def start(self, norad: int, sat_name: str) -> Path:
        """Open a new MP3 file and begin recording. Returns the file path."""
        if not LAMEENC_AVAILABLE or _lameenc is None:
            raise RuntimeError("lameenc is not installed. Run: pip install lameenc")
        if self._active:
            self.stop()

        self._save_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize satellite name for use in filename
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in sat_name)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{norad}_{safe_name}_{ts}.mp3"
        self._file_path = self._save_dir / filename

        enc: Any = _lameenc.Encoder()
        enc.set_bit_rate(128)
        enc.set_in_sample_rate(self._sample_rate)
        enc.set_channels(1)
        enc.set_quality(5)  # 2=highest, 7=fastest

        self._encoder = enc
        self._file = open(self._file_path, "wb")  # noqa: SIM115
        self._start_time = time.monotonic()
        self._bytes_written = 0
        self._active = True
        logger.info("Audio recording started: %s", self._file_path)
        return self._file_path

    def put_pcm(self, pcm: np.ndarray) -> None:
        """Encode a float32 PCM block and write to the MP3 file."""
        if not self._active or self._encoder is None or self._file is None:
            return
        # lameenc expects int16; convert from float32 [-1, 1]
        pcm_clipped = np.clip(pcm, -1.0, 1.0)
        pcm_int16 = (pcm_clipped * 32767).astype(np.int16)
        mp3_data: bytes = self._encoder.encode(pcm_int16.tobytes())
        if mp3_data:
            self._file.write(mp3_data)
            self._bytes_written += len(mp3_data)

    def stop(self) -> Path | None:
        """Flush encoder, close file, return saved path."""
        if not self._active:
            return None
        self._active = False
        path = self._file_path
        try:
            if self._encoder is not None and self._file is not None:
                tail: bytes = self._encoder.flush()
                if tail:
                    self._file.write(tail)
                    self._bytes_written += len(tail)
        finally:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._encoder = None
        logger.info("Audio recording stopped: %s (%.1f MB)", path, self._bytes_written / 1e6)
        return path
