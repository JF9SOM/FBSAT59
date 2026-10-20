"""Decode the CW in a whole I/Q recording much faster than real time.

The live CW Decoder tab decodes a rolling 20 s audio window every 5 s. This worker
does exactly that on a recording, without waiting: the file is read block by block,
demodulated by the same CW-mode :class:`~sdr.demodulator.Demodulator` the live path
uses, and a window ending every ``WINDOW_STEP_S`` seconds of audio is handed to the
decode function. The windows are announced one by one (``window_ready``) so the CW
Decoder tab can feed them to the same transcript / majority-vote / block-extractor
code as live windows, which makes a fast decode read the same as a real-time replay.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QThread, Signal

from comms.cw.codec import MAX_AUDIO_SECONDS, MIN_AUDIO_SECONDS, DecodeResult
from comms.cw.sdr_demod import SDR_AUDIO_RATE
from comms.telemetry.iq_wav import WavError, iter_iq_blocks, read_wav_info

# A window is decoded every this many seconds of audio (the live decode interval).
WINDOW_STEP_S = 5.0

DecodeFunction = Callable[[NDArray[np.float32], int], DecodeResult]


def _make_demodulator(sample_rate: float) -> Any:
    """A CW-mode demodulator for I/Q at *sample_rate* (scipy is imported lazily)."""
    from sdr.demodulator import DemodMode, Demodulator

    demodulator = Demodulator(input_rate=float(sample_rate))
    demodulator.set_mode(DemodMode.CW)
    return demodulator


class CwFileDecoder(QThread):
    """Decode a recording in a worker thread, one 5 s step at a time.

    Usage
    -----
    job = CwFileDecoder(path, decode_function)
    job.window_ready.connect(consumer)  # (DecodeResult, float window start s, float window end s)
    job.progress.connect(show)          # (float 0..1)
    job.decoded.connect(done)           # (float recording s, float elapsed s, int windows)
    job.failed.connect(error)           # (str)
    job.start()
    ...
    job.cancel()                        # stops early; decoded is then not emitted
    """

    window_ready: Signal = Signal(object, float, float)
    progress: Signal = Signal(float)
    decoded: Signal = Signal(float, float, int)
    failed: Signal = Signal(str)

    _PROGRESS_INTERVAL_S = 0.25

    def __init__(self, path: Path, decode: DecodeFunction, parent: Any = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._decode = decode
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Ask the worker to stop at the next block and wait for it."""
        self._cancel.set()
        self.wait(10000)

    def run(self) -> None:
        try:
            info = read_wav_info(self._path)
            demodulator = _make_demodulator(info.sample_rate)
        except (WavError, ImportError, ValueError) as exc:
            self.failed.emit(str(exc))
            return
        rate = SDR_AUDIO_RATE
        max_samples = int(MAX_AUDIO_SECONDS * rate)
        step_samples = int(WINDOW_STEP_S * rate)
        started = time.monotonic()
        last_report = started
        buf = np.zeros(0, dtype=np.float32)
        buf_start = 0  # absolute audio sample index of buf[0]
        next_end = step_samples
        windows = 0
        done = 0
        try:
            for block in iter_iq_blocks(self._path, info):
                if self._cancel.is_set():
                    return
                audio = demodulator.process(block)
                done += len(block)
                if len(audio):
                    buf = np.concatenate([buf, np.asarray(audio, dtype=np.float32)])
                total_end = buf_start + len(buf)
                while next_end <= total_end:
                    if self._cancel.is_set():
                        return
                    self._decode_window(buf, buf_start, next_end, max_samples, rate)
                    windows += 1
                    next_end += step_samples
                # Keep one step more than a window: the tail window may end before next_end.
                keep_from = max(next_end - step_samples - max_samples, buf_start)
                if keep_from > buf_start:
                    buf = buf[keep_from - buf_start :]
                    buf_start = keep_from
                now = time.monotonic()
                if now - last_report >= self._PROGRESS_INTERVAL_S:
                    last_report = now
                    self.progress.emit(done / max(info.frames, 1))
            total_end = buf_start + len(buf)
            # The tail after the last full step (a frame can end there).
            if total_end - (next_end - step_samples) >= int(1.0 * rate):
                self._decode_window(buf, buf_start, total_end, max_samples, rate)
                windows += 1
        except Exception as exc:  # a failing decode must end the job, not hang the UI
            self.failed.emit(str(exc))
            return
        self.progress.emit(1.0)
        self.decoded.emit(info.duration_s, time.monotonic() - started, windows)

    def _decode_window(
        self, buf: NDArray[np.float32], buf_start: int, end: int, max_samples: int, rate: int
    ) -> None:
        """Decode the audio ending at absolute sample *end* (at most 20 s of it)."""
        start = max(end - max_samples, buf_start, 0)
        window = buf[start - buf_start : end - buf_start]
        if len(window) / rate < MIN_AUDIO_SECONDS:
            return  # too short for the model; later windows include this audio
        result = self._decode(window, rate)
        self.window_ready.emit(result, start / rate, end / rate)
