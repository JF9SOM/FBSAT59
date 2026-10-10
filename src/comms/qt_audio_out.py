"""Qt Multimedia audio output for TX bursts, mirroring WSJT-X's playback path.

WSJT-X plays its transmit audio with Qt's QAudioSink in pull mode: 48 kHz,
16-bit signed integer, a single channel for the "Mono" output setting, and no
explicit buffer size (Qt/the OS decides; see ``default_tx_audio_buffer_frames
= -1`` in WSJT-X's mainwindow.cpp). The Pwr slider is the sink's own volume.
This module reproduces exactly that, so the audio that reaches the rig's USB
codec is produced by the same machinery as in WSJT-X instead of PortAudio.

Like WSJT-X, the sink is created per burst from the device's preferred
format, started with the leading silence already in the buffer, left running
after the data ends, and finally stopped with reset()+stop().

The whole burst lives in a ``QBuffer`` (a C++ QIODevice), so Qt's audio thread
reads it without ever calling back into Python -- a blocking CAT call that
holds the GIL cannot cause an underrun. Volume changes made while a burst is
playing are applied by polling ``get_gain`` on the player's own thread.

QAudioSink needs a thread with an event loop, so one dedicated ``QThread``
(like WSJT-X's audio thread) owns the player; callers on any thread use
:func:`play_burst` and block on the returned job's ``done`` event.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QCoreApplication,
    QIODevice,
    QObject,
    QThread,
    QTimer,
    Signal,
)

if TYPE_CHECKING:
    from PySide6.QtMultimedia import QAudioDevice, QAudioSink

# QtMultimedia is imported lazily (inside the functions below): on a Linux
# machine without PulseAudio's libpulse the import itself raises ImportError,
# which must not stop this module -- or ui.ft4_tab, which imports it -- from
# loading. The failure is reported as a TX error when a burst is played.

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
_POLL_INTERVAL_MS = 50
# If the sink never reports the end of the data, the burst is treated as
# played this long after its nominal duration; the caller still decides when
# to stop the sink (as WSJT-X does, see TxWorker.run()).
_FINISH_MARGIN_S = 1.5
# Last-resort stop for a job nobody ever stopped.
_HARD_STOP_MARGIN_S = 15.0


@dataclass
class PlayJob:
    """One burst to play and the outcome the caller waits for."""

    pcm: bytes
    duration_s: float
    device_name: str | None
    get_gain: Callable[[], float]
    # Set when the sink has consumed the whole buffer (QAudio::IdleState at
    # EOF); the sink itself keeps running until stop is requested.
    audio_done: threading.Event = field(default_factory=threading.Event)
    # Set when the sink has been stopped and torn down.
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    # Diagnostics (filled in by the player thread).
    requested_at: float = 0.0
    started_at: float | None = None
    first_active_at: float | None = None
    processed_s: float = 0.0
    buffer_bytes: int = 0
    finished_naturally: bool = False


def float_to_pcm16(
    audio: NDArray[np.float32], silent_frames: int = 0, skip_frames: int = 0
) -> bytes:
    """Full-scale float [-1, 1] -> little-endian int16 PCM, WSJT-X style.

    ``silent_frames`` of leading silence are prepended (Modulator's
    ``m_silentFrames``: audio starts at the nominal time into the period);
    ``skip_frames`` leading audio frames are dropped for a late start
    (``m_ic``). Samples are ``qRound(32767 * wave)``.
    """
    pcm = np.clip(np.rint(audio[skip_frames:].astype(np.float64) * 32767.0), -32768, 32767)
    lead = np.zeros(max(0, silent_frames), dtype=np.float64)
    return bytes(np.concatenate([lead, pcm]).astype("<i2").tobytes())


def find_output_device(name: str | None) -> QAudioDevice | None:
    """Qt output device whose description equals *name* (None: system default)."""
    from PySide6.QtMultimedia import QMediaDevices

    if name is None:
        return QMediaDevices.defaultAudioOutput()
    for dev in QMediaDevices.audioOutputs():
        if dev.description() == name:
            return dev
    return None


class _Player(QObject):
    """Lives on the audio QThread; runs one job at a time."""

    play_requested = Signal(object)
    abort_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._job: PlayJob | None = None
        self._sink: QAudioSink | None = None
        self._buffer: QBuffer | None = None
        self._timer: QTimer | None = None
        self.play_requested.connect(self._on_play)
        self.abort_requested.connect(self._on_abort)

    def _on_play(self, job: PlayJob) -> None:
        try:
            # QAudio is imported although unused here: loading its Python enum
            # registration is what lets PySide convert QAudio::State arguments
            # when stateChanged is delivered to _on_state.
            from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink  # noqa: F401
        except ImportError as exc:
            job.error = f"Qt Multimedia is not available: {exc}"
            job.audio_done.set()
            job.done.set()
            return
        if self._job is not None:
            job.error = "audio output busy"
            job.audio_done.set()
            job.done.set()
            return
        device = find_output_device(job.device_name)
        if device is None:
            job.error = f"audio output device not found: {job.device_name!r}"
            job.audio_done.set()
            job.done.set()
            return
        # Same as WSJT-X's SoundOutput::restart(): start from the device's
        # preferred format, then force channel count, 48 kHz and signed 16 bit.
        fmt = QAudioFormat(device.preferredFormat())
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            job.error = f"{device.description()}: 48 kHz / 16-bit / mono output is not supported"
            job.audio_done.set()
            job.done.set()
            return
        logger.info(
            "qt_audio: open device=%r id=%r fmt=%d Hz/%dch/%s preferred=%d Hz/%dch/%s "
            "pcm=%d B duration=%.2fs",
            device.description(),
            bytes(device.id()),
            fmt.sampleRate(),
            fmt.channelCount(),
            fmt.sampleFormat().name,
            device.preferredFormat().sampleRate(),
            device.preferredFormat().channelCount(),
            device.preferredFormat().sampleFormat().name,
            len(job.pcm),
            job.duration_s,
        )
        sink = QAudioSink(device, fmt, self)
        buffer = QBuffer(self)
        buffer.setData(QByteArray(job.pcm))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        sink.setVolume(max(0.0, min(1.0, float(job.get_gain()))))
        sink.stateChanged.connect(self._on_state)
        self._job, self._sink, self._buffer = job, sink, buffer
        job.requested_at = time.monotonic()
        sink.start(buffer)
        job.started_at = time.monotonic()
        job.buffer_bytes = int(sink.bufferSize())
        timer = QTimer(self)
        timer.setInterval(_POLL_INTERVAL_MS)
        timer.timeout.connect(self._on_poll)
        timer.start()
        self._timer = timer
        if sink.error().name != "NoError":
            self._finish(f"audio sink error: {sink.error().name}")

    def _on_state(self, state: object) -> None:
        # Not annotated as QAudio.State on purpose: QtMultimedia is imported
        # lazily, and PySide resolves slot annotations when connecting -- an
        # unresolved name makes every stateChanged emission fail with a TypeError.
        from PySide6.QtMultimedia import QAudio

        job, buffer = self._job, self._buffer
        if job is None or buffer is None:
            return
        sink = self._sink
        logger.info(
            "qt_audio: state=%s t=%.3fs processed=%.3fs buf_pos=%d/%d error=%s",
            getattr(state, "name", state),
            time.monotonic() - job.requested_at,
            sink.processedUSecs() / 1e6 if sink is not None else -1.0,
            buffer.pos(),
            buffer.size(),
            sink.error().name if sink is not None else "?",
        )
        if state == QAudio.State.ActiveState and job.first_active_at is None:
            job.first_active_at = time.monotonic()
        elif state == QAudio.State.IdleState and buffer.atEnd():
            job.finished_naturally = True
            job.audio_done.set()

    def _on_poll(self) -> None:
        job, sink = self._job, self._sink
        if job is None or sink is None or job.started_at is None:
            return
        sink.setVolume(max(0.0, min(1.0, float(job.get_gain()))))
        if sink.error().name != "NoError":
            self._finish(f"audio sink error: {sink.error().name}")
        else:
            elapsed = time.monotonic() - job.started_at
            if elapsed > job.duration_s + _FINISH_MARGIN_S:
                job.finished_naturally = True
                job.audio_done.set()
            if elapsed > job.duration_s + _HARD_STOP_MARGIN_S:
                self._finish(None)

    def _on_abort(self) -> None:
        if self._job is not None:
            self._finish(None)

    def _finish(self, error: str | None) -> None:
        job, sink, buffer, timer = self._job, self._sink, self._buffer, self._timer
        if error and job is not None and sink is not None and buffer is not None:
            logger.warning(
                "qt_audio: %s at t=%.3fs processed=%.3fs buf_pos=%d/%d sink_state=%s",
                error,
                time.monotonic() - job.requested_at,
                sink.processedUSecs() / 1e6,
                buffer.pos(),
                buffer.size(),
                sink.state().name,
            )
        self._job = self._sink = self._buffer = self._timer = None
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if sink is not None:
            job_processed = sink.processedUSecs() / 1e6
            # WSJT-X's SoundOutput::stop(): reset() then stop().
            sink.reset()
            sink.stop()
            sink.deleteLater()
        else:
            job_processed = 0.0
        if buffer is not None:
            buffer.close()
            buffer.deleteLater()
        if job is not None:
            job.processed_s = job_processed
            if error:
                job.error = error
            job.audio_done.set()
            job.done.set()


_lock = threading.Lock()
_thread: QThread | None = None
_player: _Player | None = None


def _ensure_player() -> _Player:
    global _thread, _player
    with _lock:
        if _player is None:
            thread = QThread()
            thread.setObjectName("fbsat59-tx-audio")
            player = _Player()
            player.moveToThread(thread)
            thread.start()
            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(_shutdown)
            _thread, _player = thread, player
        return _player


def _shutdown() -> None:
    global _thread, _player
    with _lock:
        thread, _thread, _player = _thread, None, None
    if thread is not None:
        thread.quit()
        thread.wait(2000)


def play_burst(job: PlayJob) -> None:
    """Hand *job* to the audio thread (returns immediately; wait on ``job.done``)."""
    _ensure_player().play_requested.emit(job)


def abort_burst() -> None:
    """Stop whatever is playing (no-op when idle)."""
    player = _player
    if player is not None:
        player.abort_requested.emit()
