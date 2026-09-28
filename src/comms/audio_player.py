"""AudioFilePlayer — streams a recorded audio file (MP3/WAV/...) to the speakers.

Used by Radio Control's Recording box to play back the MP3s its REC button
saves. The file is streamed block by block through a sounddevice
OutputStream callback rather than decoded up front, so opening a long
recording is instant and memory stays flat. Output goes to the system
default device on purpose: the rig's Sound Card output feeds the radio's
transmit audio, and a played-back recording must never be routed there.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
from numpy.typing import NDArray

try:
    import soundfile as _soundfile

    SOUNDFILE_AVAILABLE: bool = True
except ImportError:
    _soundfile = None
    SOUNDFILE_AVAILABLE = False


class AudioFilePlayer:
    """Play, pause and seek a single audio file. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file: Any = None
        self._stream: Any = None
        self._channels: int = 1
        self._samplerate: int = 0
        self._frames: int = 0
        self._playing: bool = False

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    @property
    def is_loaded(self) -> bool:
        """True once a file has been opened with load()."""
        return self._file is not None

    @property
    def is_playing(self) -> bool:
        """True while audio is being output (False after pause or end of file)."""
        return self._playing

    @property
    def duration_s(self) -> float:
        """Length of the loaded file in seconds (0 when nothing is loaded)."""
        return self._frames / self._samplerate if self._samplerate else 0.0

    @property
    def position_s(self) -> float:
        """Current playback position in seconds."""
        with self._lock:
            if self._file is None or not self._samplerate:
                return 0.0
            return float(self._file.tell()) / self._samplerate

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #

    def load(self, path: str) -> None:
        """Open *path*, replacing (and stopping) any previously loaded file.

        Raises RuntimeError if soundfile is missing, or whatever soundfile
        raises for an unreadable/unsupported file.
        """
        if not SOUNDFILE_AVAILABLE or _soundfile is None:
            raise RuntimeError("soundfile is not installed. Run: pip install soundfile")
        new_file = _soundfile.SoundFile(path)
        self.close()
        with self._lock:
            self._file = new_file
            self._channels = int(new_file.channels)
            self._samplerate = int(new_file.samplerate)
            self._frames = int(new_file.frames)

    def play(self) -> None:
        """Start (or resume) playback; restarts from the beginning after the end."""
        import sounddevice as sd  # optional dep

        if self._file is None or self._playing:
            return
        self._close_stream()
        with self._lock:
            if self._file.tell() >= self._frames:
                self._file.seek(0)
            self._playing = True
        try:
            stream = sd.OutputStream(
                samplerate=self._samplerate,
                channels=self._channels,
                dtype="float32",
                callback=self._on_audio,
            )
            stream.start()
        except Exception:
            self._playing = False
            raise
        self._stream = stream

    def pause(self) -> None:
        """Pause playback, keeping the position."""
        self._playing = False
        self._close_stream()

    def seek_relative(self, seconds: float) -> None:
        """Jump forward (positive) or back (negative) by *seconds*, clamped to the file."""
        with self._lock:
            if self._file is None:
                return
            target = int(self._file.tell() + seconds * self._samplerate)
            self._file.seek(max(0, min(target, self._frames)))

    def close(self) -> None:
        """Stop playback and release the file."""
        self._playing = False
        self._close_stream()
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._frames = 0
            self._samplerate = 0

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def _on_audio(
        self, outdata: NDArray[np.float32], frames: int, _time: Any, _status: Any
    ) -> None:
        """sounddevice callback: fill *outdata* from the file, then stop at its end."""
        import sounddevice as sd  # optional dep

        with self._lock:
            data = self._file.read(frames, dtype="float32", always_2d=True) if self._file else None
        got = 0 if data is None else len(data)
        if got:
            outdata[:got] = data
        if got < frames:
            outdata[got:] = 0.0
            self._playing = False
            raise sd.CallbackStop
