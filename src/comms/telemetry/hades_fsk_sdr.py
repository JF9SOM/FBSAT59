"""Run a :class:`~comms.telemetry.hades_fsk.HadesFskStream` on an SDR pipeline's raw I/Q.

Same shape as ``comms.sstv.fsk_ssdv.FskSsdvSdrDemod``: the pipeline hands every I/Q block
to :meth:`HadesFskSdrDemod.push_samples` (any thread), a worker thread does the demodulation
and the decoded frames are emitted as a Qt signal.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from comms.telemetry.hades_fsk import HadesFskStream


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
