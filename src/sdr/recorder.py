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
import struct
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Maximum queue depth before dropping samples (avoids unbounded memory growth)
_QUEUE_MAXSIZE = 512

# How often (seconds) the WAV header sizes are rewritten while recording, so a
# crash loses at most this much of the recording's "declared" length.
_HEADER_REFRESH_S = 5.0

# A complex sample counts as clipped when |I| or |Q| reaches this fraction of
# full scale (8-bit SDRs top out at 127/128 = 0.992).
CLIP_LEVEL = 0.98

# Clipped fraction at or above which the input counts as overloaded.
CLIP_WARN_FRACTION = 0.01

# WAV header layout: RIFF(12) + fmt(24) + fact(12) + data chunk header(8).
_WAV_HEADER_BYTES = 56
_UINT32_MAX = 0xFFFFFFFF


class _StreamingWavWriter:
    """
    Append-only CF32 stereo WAV writer.

    Samples are written to disk as they arrive; the RIFF/data sizes in the
    header are rewritten periodically (and on close).  If the process dies
    mid-recording, the file on disk is still a playable WAV containing
    everything up to the last header refresh (and the raw samples after it).
    """

    def __init__(self, path: Path, sample_rate: int) -> None:
        self._path = path
        self._sample_rate = sample_rate
        self._data_bytes = 0
        self._last_refresh = time.monotonic()
        self._fh = open(path, "wb")  # noqa: SIM115 - closed in close()
        self._write_header()

    def _write_header(self) -> None:
        data_size = min(self._data_bytes, _UINT32_MAX)
        riff_size = min(_WAV_HEADER_BYTES - 8 + self._data_bytes, _UINT32_MAX)
        frames = min(self._data_bytes // 8, _UINT32_MAX)
        header = (
            b"RIFF"
            + struct.pack("<I", riff_size)
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 3, 2, self._sample_rate, self._sample_rate * 8, 8, 32)
            + b"fact"
            + struct.pack("<II", 4, frames)
            + b"data"
            + struct.pack("<I", data_size)
        )
        self._fh.seek(0)
        self._fh.write(header)
        self._fh.seek(0, 2)

    def write(self, stereo: np.ndarray) -> None:
        """Append interleaved float32 I/Q samples."""
        self._fh.write(stereo.tobytes())
        self._data_bytes += stereo.nbytes
        now = time.monotonic()
        if now - self._last_refresh >= _HEADER_REFRESH_S:
            self._last_refresh = now
            self._write_header()
            self._fh.flush()

    def close(self) -> None:
        """Finalise the header and close the file."""
        try:
            self._write_header()
            self._fh.flush()
        finally:
            self._fh.close()


class _Decimator:
    """
    Streaming FIR low-pass + integer decimation for complex IQ (numpy only).

    Lets the recorder write at the requested bandwidth while the SDR keeps
    running at its own rate.  Kaiser-windowed sinc, ~60 dB stop-band, pass-band
    to 0.4x the output rate, so anything that would alias into the recording
    is removed before decimation.  State is carried across blocks.
    """

    _CHUNK = 2048  # outputs per matrix product (bounds temporary memory)

    def __init__(self, factor: int) -> None:
        self.factor = factor
        n_taps = 18 * factor + 1  # odd -> linear phase, integer group delay
        n = np.arange(n_taps) - (n_taps - 1) / 2
        h = np.sinc(2.0 * 0.5 / factor * n) * (1.0 / factor) * np.kaiser(n_taps, 5.65)
        h *= 1.0 / np.sum(h)  # unity DC gain
        self._taps = h.astype(np.float32)  # symmetric: convolution == correlation
        self._tail = np.zeros(0, dtype=np.complex64)

    def process(self, block: np.ndarray) -> np.ndarray:
        """Filter and decimate one block; returns the new output samples."""
        if self.factor == 1:
            return block
        n_taps = len(self._taps)
        buf = np.concatenate((self._tail, block.astype(np.complex64, copy=False)))
        n_out = (len(buf) - n_taps) // self.factor + 1 if len(buf) >= n_taps else 0
        if n_out <= 0:
            self._tail = buf
            return np.zeros(0, dtype=np.complex64)
        parts: list[np.ndarray] = []
        for start in range(0, n_out, self._CHUNK):
            m = min(self._CHUNK, n_out - start)
            first = start * self.factor
            seg = buf[first : first + (m - 1) * self.factor + n_taps]
            win = np.lib.stride_tricks.sliding_window_view(seg, n_taps)[:: self.factor]
            parts.append(win @ self._taps)
        self._tail = buf[n_out * self.factor :]
        return np.concatenate(parts).astype(np.complex64, copy=False)


class IQRecorder:
    """
    Thread-safe IQ recorder.

    Samples are accepted from the SDRPipeline thread via put_samples() and
    streamed to disk on a dedicated writer thread so the pipeline is not blocked
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
        self._decimator = _Decimator(1)
        # Clipping statistics (updated from the pipeline thread, read by the UI)
        self._clip_lock = threading.Lock()
        self._reset_clip_stats()

    def _reset_clip_stats(self) -> None:
        with self._clip_lock:
            self._clip_bucket_start = time.monotonic()
            self._clip_bucket_n = 0
            self._clip_bucket_clipped = 0
            self._clip_recent: deque[float] = deque(maxlen=2)  # last 1 s bucket fractions
            self._clip_total_n = 0
            self._clip_total_clipped = 0
            self._clip_max = 0.0

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

    @property
    def clip_fraction_recent(self) -> float:
        """Fraction of samples at full scale over roughly the last 2 s (0..1)."""
        with self._clip_lock:
            return sum(self._clip_recent) / len(self._clip_recent) if self._clip_recent else 0.0

    @property
    def clip_fraction_average(self) -> float:
        """Fraction of samples at full scale over the whole recording (0..1)."""
        with self._clip_lock:
            n = self._clip_total_n + self._clip_bucket_n
            return (self._clip_total_clipped + self._clip_bucket_clipped) / n if n else 0.0

    @property
    def clip_fraction_max(self) -> float:
        """Worst 1 s clipped fraction seen so far in this recording (0..1)."""
        with self._clip_lock:
            return self._clip_max

    def _track_clipping(self, iq: np.ndarray) -> None:
        """Accumulate how many samples sit at full scale (input overload)."""
        clipped = int(
            np.count_nonzero((np.abs(iq.real) >= CLIP_LEVEL) | (np.abs(iq.imag) >= CLIP_LEVEL))
        )
        now = time.monotonic()
        with self._clip_lock:
            self._clip_bucket_n += len(iq)
            self._clip_bucket_clipped += clipped
            if now - self._clip_bucket_start >= 1.0 and self._clip_bucket_n:
                frac = self._clip_bucket_clipped / self._clip_bucket_n
                self._clip_recent.append(frac)
                self._clip_max = max(self._clip_max, frac)
                self._clip_total_n += self._clip_bucket_n
                self._clip_total_clipped += self._clip_bucket_clipped
                self._clip_bucket_n = 0
                self._clip_bucket_clipped = 0
                self._clip_bucket_start = now

    def start(
        self,
        sample_rate: int,
        norad: int = 0,
        sat_name: str = "unknown",
        input_rate: float | None = None,
    ) -> Path:
        """
        Begin recording.  Returns the file path that will be written.
        Raises RuntimeError if already recording.

        *sample_rate* is the requested recording bandwidth.  If *input_rate*
        (the SDR's real stream rate) is higher, the stream is low-pass
        filtered and decimated by floor(input_rate / sample_rate) so the
        file is written at input_rate / factor (>= sample_rate) while the
        SDR itself keeps running at its own rate.  The WAV header carries
        the rate actually written.
        """
        if self._recording:
            raise RuntimeError("IQRecorder is already recording")

        self._save_dir.mkdir(parents=True, exist_ok=True)
        factor = 1
        if input_rate and input_rate > sample_rate:
            factor = max(1, int(input_rate // sample_rate))
            sample_rate = round(input_rate / factor)
        elif input_rate:
            sample_rate = round(input_rate)  # cannot record wider than the stream
        self._decimator = _Decimator(factor)
        self._sample_rate = sample_rate
        self._bytes_written = 0
        self._dropped = 0
        self._reset_clip_stats()
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
        logger.info(
            "IQ recording started: %s (%d Hz%s)",
            self._file_path,
            sample_rate,
            f", decimated x{factor} from the SDR stream" if factor > 1 else "",
        )
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
        if self.clip_fraction_max >= CLIP_WARN_FRACTION:
            logger.warning(
                "IQ recording was saturating: %.1f%% of samples clipped on average, "
                "%.1f%% at worst — lower the SDR gain",
                self.clip_fraction_average * 100,
                self.clip_fraction_max * 100,
            )

    def put_samples(self, iq: np.ndarray) -> None:
        """
        Enqueue a block of complex64 samples for writing.

        Drops the block silently if the queue is full (pipeline must not block).
        """
        if not self._recording:
            return
        self._track_clipping(iq)
        try:
            self._queue.put_nowait(iq.copy())
        except queue.Full:
            self._dropped += 1

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self, path: Path, sample_rate: int) -> None:
        """Stream samples to the WAV file as they arrive (crash-safe)."""
        writer: _StreamingWavWriter | None = None
        failed = False
        try:
            while True:
                block = self._queue.get()
                if block is None:
                    break
                if failed:
                    continue  # keep draining so the pipeline never blocks
                try:
                    block = self._decimator.process(block)
                    if len(block) == 0:
                        continue
                    if writer is None:
                        writer = _StreamingWavWriter(path, sample_rate)
                    # Interleave I and Q into stereo float32
                    stereo = np.empty(len(block) * 2, dtype=np.float32)
                    stereo[0::2] = block.real
                    stereo[1::2] = block.imag
                    writer.write(stereo)
                    self._bytes_written += stereo.nbytes
                except Exception:
                    logger.exception("Failed to write IQ WAV (recording aborted): %s", path)
                    failed = True
        finally:
            if writer is not None:
                try:
                    writer.close()
                    logger.info("IQ WAV written: %s (%.1f MB)", path, path.stat().st_size / 1e6)
                except Exception:
                    logger.exception("Failed to finalise IQ WAV: %s", path)
