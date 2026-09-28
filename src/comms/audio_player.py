"""AudioFilePlayer — streams a recorded audio file (MP3/WAV/...) to the speakers.

Used by Radio Control's Recording box to play back the MP3s its REC button
saves. The file is streamed block by block through a sounddevice
OutputStream callback rather than decoded up front, so opening a long
recording is instant and memory stays flat. Output goes to the system
default device on purpose: the rig's Sound Card output feeds the radio's
transmit audio, and a played-back recording must never be routed there.

Optionally the same audio is also pushed to a FeedSink (the decoder tabs), so a
recording can be decoded as if it were arriving live. The sink is fed from a
worker thread through a bounded queue: the audio callback never blocks on a
decoder, and a slow decoder drops blocks instead of glitching the speakers.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

try:
    import soundfile as _soundfile

    SOUNDFILE_AVAILABLE: bool = True
except ImportError:
    _soundfile = None
    SOUNDFILE_AVAILABLE = False


class FeedSink(Protocol):
    """Receiver of the audio being played (begin, then blocks, then end)."""

    def begin(self) -> None: ...

    def push(self, chunk: NDArray[np.float32], samplerate: int) -> None: ...

    def end(self) -> None: ...


class _FeedSession:
    """One playback run's worker thread that forwards queued blocks to a FeedSink."""

    def __init__(self, sink: FeedSink, samplerate: int) -> None:
        self._sink = sink
        self._samplerate = samplerate
        self._queue: queue.Queue[NDArray[np.float32]] = queue.Queue(maxsize=64)
        self._finished = threading.Event()
        self._sink.begin()
        self._thread = threading.Thread(target=self._run, name="audio-feed", daemon=True)
        self._thread.start()

    def put(self, chunk: NDArray[np.float32]) -> None:
        """Queue a block without blocking (dropped when the worker is behind)."""
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(chunk)

    def finish(self) -> None:
        """Ask the worker to drain what is queued, then end the sink. Never blocks."""
        self._finished.set()

    def _run(self) -> None:
        try:
            while True:
                try:
                    chunk = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._finished.is_set():
                        return
                    continue
                with contextlib.suppress(Exception):
                    self._sink.push(chunk, self._samplerate)
        finally:
            self._sink.end()


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
        self._feed_sink: FeedSink | None = None
        self._feed: _FeedSession | None = None

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

    def set_feed_sink(self, sink: FeedSink | None) -> None:
        """Also push the played audio to *sink* (None to stop). Takes effect at once."""
        self._end_feed()
        self._feed_sink = sink
        if self._playing:
            self._start_feed()

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
        self._start_feed()
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
            self._end_feed()
            raise
        self._stream = stream

    def pause(self) -> None:
        """Pause playback, keeping the position."""
        self._playing = False
        self._close_stream()
        self._end_feed()

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
        self._end_feed()
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._frames = 0
            self._samplerate = 0

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _start_feed(self) -> None:
        if self._feed_sink is not None and self._feed is None:
            self._feed = _FeedSession(self._feed_sink, self._samplerate)

    def _end_feed(self) -> None:
        feed, self._feed = self._feed, None
        if feed is not None:
            feed.finish()

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
        if got and data is not None:
            outdata[:got] = data
            feed = self._feed
            if feed is not None:
                feed.put(data.mean(axis=1).astype(np.float32))
        if got < frames:
            outdata[got:] = 0.0
            self._playing = False
            # The session's worker ends the sink once the last blocks are drained.
            feed, self._feed = self._feed, None
            if feed is not None:
                feed.finish()
            raise sd.CallbackStop
