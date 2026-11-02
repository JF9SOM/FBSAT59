"""Diagnostic recording of the receive audio around each APRS transmission.

Reported 2026-10-31 (GitHub Issue #35): after transmitting to the ISS digipeater,
the operator never hears the digipeater's echo of their own packet, and neither
fbsat59.log nor direwolf.log records what the receiver audio looked like in the
seconds after the PTT dropped. Direwolf only reports frames it decoded, so a weak
or garbled echo leaves no trace at all.

``TxAudioRecorder`` keeps a short rolling buffer of the receive audio that feeds
Direwolf. Around every transmission it saves the audio from a few seconds before
the PTT went on until several seconds after it went off, as a small FLAC file in
``<log dir>/aprs_tx_audio``. The file can be replayed offline (decoded with
Direwolf's ``atest``, listened to, or inspected on a spectrogram) to tell "the
echo never arrived" from "the echo arrived but was not decoded".

The audio is decimated to 12 kHz (the AFSK tones sit below 3 kHz) to keep the
files small: the operator zips the Logs folder and attaches it to an Issue. Only
the newest ``_KEEP_FILES`` recordings are kept.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_SOURCE_RATE = 48000  # AudioBridge feeds Direwolf at 48 kHz
_DECIMATION = 4
SAVED_RATE = _SOURCE_RATE // _DECIMATION  # 12 kHz
_RING_SECONDS = 60.0  # oldest audio kept while waiting for a transmission
PRE_ROLL_S = 5.0  # audio kept from before the PTT went on
POST_ROLL_S = 12.0  # audio kept after the PTT went off (digipeater echo window)
_KEEP_FILES = 60


def tx_audio_dir() -> Path:
    """Directory the recordings are written to (inside the application log directory)."""
    from platformdirs import user_log_dir

    return Path(user_log_dir("fbsat59", "fbsat59")) / "aprs_tx_audio"


def _decimate(chunk: Any) -> np.ndarray:
    """Average groups of ``_DECIMATION`` samples (crude low-pass + downsample)."""
    samples = np.asarray(chunk, dtype=np.float32).reshape(-1)
    usable = (len(samples) // _DECIMATION) * _DECIMATION
    if usable == 0:
        return np.zeros(0, dtype=np.float32)
    return np.asarray(samples[:usable].reshape(-1, _DECIMATION).mean(axis=1), dtype=np.float32)


class TxAudioRecorder:
    """Rolling receive-audio buffer that is saved around each transmission."""

    def __init__(self, out_dir: Path | None = None) -> None:
        self._out_dir = out_dir
        self._lock = threading.Lock()
        # (monotonic time of the chunk's last sample, decimated samples)
        self._ring: collections.deque[tuple[float, np.ndarray]] = collections.deque()
        self._tx_on: float | None = None
        self._tx_off: float | None = None
        self._pending: list[threading.Timer] = []

    # -- receive side ---------------------------------------------------- #

    def feed(self, chunk: Any, now: float | None = None) -> None:
        """Append one chunk of receive audio (48 kHz mono float)."""
        samples = _decimate(chunk)
        if samples.size == 0:
            return
        t = time.monotonic() if now is None else now
        with self._lock:
            self._ring.append((t, samples))
            while self._ring and t - self._ring[0][0] > _RING_SECONDS:
                self._ring.popleft()

    # -- transmit side --------------------------------------------------- #

    def note_tx_start(self, now: float | None = None) -> None:
        """Mark the moment a transmission was started."""
        with self._lock:
            self._tx_on = time.monotonic() if now is None else now
            self._tx_off = None

    def note_tx_end(self, now: float | None = None, delay_s: float = POST_ROLL_S) -> None:
        """Mark the end of a transmission and save the recording after the echo window."""
        with self._lock:
            if self._tx_on is None:
                return
            self._tx_off = time.monotonic() if now is None else now
            tx_on, tx_off = self._tx_on, self._tx_off
            self._tx_on = None
        timer = threading.Timer(delay_s, self._save_safely, args=(tx_on, tx_off))
        timer.daemon = True
        with self._lock:
            self._pending = [t for t in self._pending if t.is_alive()]
            self._pending.append(timer)
        timer.start()

    def cancel_tx(self) -> None:
        """Forget a transmission that failed before it finished."""
        with self._lock:
            self._tx_on = None
            self._tx_off = None

    # -- saving ----------------------------------------------------------- #

    def _save_safely(self, tx_on: float, tx_off: float) -> None:
        try:
            self.save(tx_on, tx_off)
        except Exception:  # noqa: BLE001 - diagnostics must never disturb operation
            logger.exception("TX audio log: saving failed")

    def save(self, tx_on: float, tx_off: float) -> Path | None:
        """Write the audio from ``PRE_ROLL_S`` before *tx_on* to ``POST_ROLL_S`` after *tx_off*."""
        window_start = tx_on - PRE_ROLL_S
        window_end = tx_off + POST_ROLL_S
        with self._lock:
            parts = [
                (t, s)
                for t, s in self._ring
                if t > window_start and t - len(s) / SAVED_RATE < window_end
            ]
        if not parts:
            logger.info("TX audio log: no receive audio was buffered for this transmission")
            return None
        audio = np.concatenate([s for _t, s in parts])
        # Chunks are contiguous, so the first sample's time follows from the last chunk.
        end_time = parts[-1][0]
        first_time = end_time - len(audio) / SAVED_RATE
        tx_on_s = max(0.0, tx_on - first_time)
        tx_off_s = max(0.0, tx_off - first_time)

        out_dir = self._out_dir if self._out_dir is not None else tx_audio_dir()
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime(
            "%Y%m%d_%H%M%S", time.localtime(time.time() - (time.monotonic() - tx_on))
        )
        base = f"tx_{stamp}_on{tx_on_s:.1f}_off{tx_off_s:.1f}"
        path = self._write(out_dir, base, audio)
        self._prune(out_dir)
        logger.info(
            "TX audio log: saved %s (%.1f s of receive audio, PTT on at %.1f s, off at %.1f s)",
            path.name,
            len(audio) / SAVED_RATE,
            tx_on_s,
            tx_off_s,
        )
        return path

    @staticmethod
    def _write(out_dir: Path, base: str, audio: np.ndarray) -> Path:
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        try:
            import soundfile as sf

            path = out_dir / f"{base}.flac"
            sf.write(str(path), pcm, SAVED_RATE, subtype="PCM_16", format="FLAC")
            return path
        except Exception:  # noqa: BLE001 - fall back to a plain WAV
            import wave

            path = out_dir / f"{base}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAVED_RATE)
                w.writeframes(pcm.tobytes())
            return path

    @staticmethod
    def _prune(out_dir: Path) -> None:
        files = sorted(
            [p for p in out_dir.iterdir() if p.suffix in (".flac", ".wav")],
            key=lambda p: p.stat().st_mtime,
        )
        for old in files[:-_KEEP_FILES]:
            with contextlib.suppress(OSError):
                old.unlink()


_recorder: TxAudioRecorder | None = None


def get_tx_audio_recorder() -> TxAudioRecorder:
    """Return the process-wide recorder, creating it on first call."""
    global _recorder
    if _recorder is None:
        _recorder = TxAudioRecorder()
    return _recorder
