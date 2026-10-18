"""Run a :class:`~comms.telemetry.hades_fsk.HadesFskStream` on an SDR pipeline's raw I/Q.

Same shape as ``comms.sstv.fsk_ssdv.FskSsdvSdrDemod``: the pipeline hands every I/Q block
to :meth:`HadesFskSdrDemod.push_samples` (any thread), a worker thread does the demodulation
and the decoded frames are emitted as a Qt signal.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from comms.telemetry.hades_fsk import HadesFskStream
from comms.telemetry.iq_wav import WavError, iter_iq_blocks, read_wav_info


class HadesFskSdrDemod(QThread):
    """Decode 200 baud HADES-family 2FSK bursts from an SDR pipeline in a worker thread.

    Usage
    -----
    demod = HadesFskSdrDemod(sample_rate=int(pipeline._device.sample_rate))
    demod.frame_received.connect(consumer)   # (HadesFrame, float: seconds since the burst)
    demod.start()
    pipeline.subscribe(demod.push_samples)
    ...
    pipeline.unsubscribe(demod.push_samples)
    demod.stop()

    The stream only reports a burst once its 10 s analysis window is complete, so a frame
    arrives up to ~10 s after it was received; the second signal argument is that delay, for
    the consumer to subtract from "now" to get the reception time.
    """

    frame_received: Signal = Signal(object, float)

    def __init__(self, sample_rate: int, parent: Any = None) -> None:
        super().__init__(parent)
        self._rate = float(sample_rate)
        self._stream = HadesFskStream(self._rate)
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self._stop_event = threading.Event()
        self._fed = 0  # input samples handed to the stream so far
        self._diag_drop_count = 0

    def push_samples(self, iq: np.ndarray) -> None:
        """Receive one I/Q block from SDRPipeline.subscribe() (any thread)."""
        try:
            self._q.put_nowait(iq.astype(np.complex64))
        except queue.Full:
            self._diag_drop_count += 1
            if self._diag_drop_count == 1 or self._diag_drop_count % 50 == 0:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().info(
                    "hades_fsk queue full, dropped block (total drops=%d)",
                    self._diag_drop_count,
                )

    def stop(self) -> None:
        """Stop the worker thread."""
        self._stop_event.set()
        self.wait(3000)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                iq = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                frames = self._stream.feed(iq)
            except Exception:
                from sdr.diag_log import get_sdr_diag_logger

                get_sdr_diag_logger().exception("HadesFskSdrDemod.run(): feed() raised")
                raise
            self._fed += len(iq)
            for frame in frames:
                self.frame_received.emit(frame, (self._fed - frame.sample_index) / self._rate)


class HadesFileDecoder(QThread):
    """Decode a whole I/Q recording much faster than real time, without the SDR pipeline.

    The file is read block by block and fed to a :class:`HadesFskStream`, so a 7.5 minute
    recording takes seconds. A frame is reported with its time in seconds from the start of
    the recording (the caller adds the recording's start time).

    Usage
    -----
    job = HadesFileDecoder(path)
    job.frame_found.connect(consumer)   # (HadesFrame, float: seconds from the file start)
    job.progress.connect(show)          # (float 0..1, int frames so far)
    job.decoded.connect(done)           # (float: recording seconds, float: elapsed, int frames)
    job.failed.connect(error)           # (str)
    job.start()
    ...
    job.cancel()                        # stops early; decoded is then not emitted
    """

    frame_found: Signal = Signal(object, float)
    progress: Signal = Signal(float, int)
    decoded: Signal = Signal(float, float, int)
    failed: Signal = Signal(str)

    _PROGRESS_INTERVAL_S = 0.25

    def __init__(self, path: Path, parent: Any = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Ask the worker to stop at the next block and wait for it."""
        self._cancel.set()
        self.wait(5000)

    def run(self) -> None:
        try:
            info = read_wav_info(self._path)
            stream = HadesFskStream(info.sample_rate)
        except (WavError, ValueError) as exc:
            self.failed.emit(str(exc))
            return
        started = time.monotonic()
        last_report = started
        frames = 0
        done = 0
        try:
            for block in iter_iq_blocks(self._path, info):
                if self._cancel.is_set():
                    return
                for frame in stream.feed(block):
                    frames += 1
                    self.frame_found.emit(frame, frame.sample_index / info.sample_rate)
                done += len(block)
                now = time.monotonic()
                if now - last_report >= self._PROGRESS_INTERVAL_S:
                    last_report = now
                    self.progress.emit(done / max(info.frames, 1), frames)
            for frame in stream.flush():
                frames += 1
                self.frame_found.emit(frame, frame.sample_index / info.sample_rate)
        except OSError as exc:
            self.failed.emit(str(exc))
            return
        self.progress.emit(1.0, frames)
        self.decoded.emit(info.duration_s, time.monotonic() - started, frames)
