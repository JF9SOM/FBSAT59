"""
IQ recorder — writes raw I/Q samples to a CF32 WAV file.

File format:
  Container : WAV (RIFF)
  Encoding  : IEEE float 32-bit, 2-channel (I = left, Q = right)
  Sample rate: matches the SDR bandwidth setting (e.g. 250 000 Hz)
  Filename  : {NORAD}_{name}_{UTC_ISO}.iq.wav

Compatible with: SDR#, GQRX, SDR++, SatDump, and any WAV reader that
supports float32 stereo (scipy.io.wavfile, librosa, …).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Maximum queue depth before dropping samples (avoids unbounded memory growth)
_QUEUE_MAXSIZE = 512


class IQRecorder:
    """
    Thread-safe IQ recorder.

    Samples are accepted from the SDRPipeline thread via put_samples() and
    written to disk on a dedicated writer thread so the pipeline is not blocked
    by I/O.

    Usage:
        rec = IQRecorder(save_dir=Path("~/iq_recordings"))
        rec.start(sample_rate=250_000, norad=25544, sat_name="ISS")
        rec.put_samples(iq_block)
        ...
        rec.stop()
        path = rec.last_file_path
    """

    def __init__(self, save_dir: Path | None = None) -> None:
        self._save_dir = save_dir or Path.home() / "iq_recordings"
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._thread: threading.Thread | None = None
        self._recording = False
        self._sample_rate: int = 250_000
        self._file_path: Path | None = None
        self._bytes_written: int = 0
        self._start_time: float = 0.0
        self._dropped: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def last_file_path(self) -> Path | None:
        return self._file_path

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def elapsed_seconds(self) -> float:
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def dropped_blocks(self) -> int:
        return self._dropped

    def start(
        self,
        sample_rate: int,
        norad: int = 0,
        sat_name: str = "unknown",
    ) -> Path:
        """
        Begin recording.  Returns the file path that will be written.
        Raises RuntimeError if already recording.
        """
        if self._recording:
            raise RuntimeError("IQRecorder is already recording")

        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._sample_rate = sample_rate
        self._bytes_written = 0
        self._dropped = 0
        self._start_time = time.monotonic()

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in sat_name)
        fname = f"{norad}_{safe_name}_{ts}.iq.wav"
        self._file_path = self._save_dir / fname

        self._recording = True
        self._thread = threading.Thread(
            target=self._writer_loop,
            args=(self._file_path, sample_rate),
            daemon=True,
            name="IQRecorder",
        )
        self._thread.start()
        logger.info("IQ recording started: %s", self._file_path)
        return self._file_path

    def stop(self) -> None:
        """Stop recording and flush remaining samples to disk."""
        if not self._recording:
            return
        self._recording = False
        self._queue.put(None)  # sentinel
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info(
            "IQ recording stopped: %.1f s, %.1f MB, %d dropped blocks",
            self.elapsed_seconds,
            self._bytes_written / 1e6,
            self._dropped,
        )

    def put_samples(self, iq: np.ndarray) -> None:
        """
        Enqueue a block of complex64 samples for writing.

        Drops the block silently if the queue is full (pipeline must not block).
        """
        if not self._recording:
            return
        try:
            self._queue.put_nowait(iq.copy())
        except queue.Full:
            self._dropped += 1

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self, path: Path, sample_rate: int) -> None:
        """Accumulate samples and write WAV on close."""
        chunks: list[np.ndarray] = []
        while True:
            block = self._queue.get()
            if block is None:
                break
            # Interleave I and Q into stereo float32
            stereo = np.empty(len(block) * 2, dtype=np.float32)
            stereo[0::2] = block.real
            stereo[1::2] = block.imag
            chunks.append(stereo)
            self._bytes_written += stereo.nbytes

        if not chunks:
            return
        try:
            # Imported here, not at module level: scipy is only needed when
            # actually writing a recording, and this keeps every other
            # sdr.* module importable without it (e.g. in CI, which only
            # installs `.[dev]` — see CLAUDE.md's optional-dependency
            # notes). Confirmed live (2026-09-16): importing SDRPipeline
            # from a test pulled this module in transitively and crashed
            # CI's collection with ModuleNotFoundError before this fix.
            import scipy.io.wavfile as wav

            data = np.concatenate(chunks).reshape(-1, 2)
            wav.write(str(path), sample_rate, data)
            logger.info("IQ WAV written: %s (%.1f MB)", path, path.stat().st_size / 1e6)
        except Exception:
            logger.exception("Failed to write IQ WAV: %s", path)
