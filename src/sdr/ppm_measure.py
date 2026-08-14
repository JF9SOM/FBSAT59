"""Automatic PPM (clock drift) measurement for SDR devices.

Compares the actual number of samples received over a measured wall-clock
interval against the device's configured sample rate to estimate its ADC
clock error in ppm -- the same technique the standard ``rtl_test -p`` tool
uses. No RF reference signal is needed: on RTL-SDR, HackRF, and most
low-cost SDRs, a single reference oscillator clocks both the ADC/DAC and
the RF tuning synthesizer, so ADC clock drift is a valid proxy for RF
frequency drift. The resulting value uses the same sign convention as
rtl_test's own "PPM" output, so it can be fed directly into the same
"PPM Correction" field used by SoapySDR's CORR frequency component and
SatDump's --ppm_correction flag.

Implemented in-app (rather than shelling out to rtl_test) so the same code
path works for every device SdrDevice supports, not just RTL-SDR -- there
is no HackRF/PlutoSDR equivalent of rtl_test to shell out to.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QThread, Signal

from i18n import _
from sdr.device import SdrDevice, SdrDeviceInfo

logger = logging.getLogger(__name__)

_CHUNK = 65536
_WARMUP_S = 5.0  # discard the first few seconds -- clock is least stable right after open()


class PpmMeasureWorker(QThread):
    """Runs the ppm measurement in a background thread.

    Signals
    -------
    progress(float)
        Fraction complete, 0.0-1.0 (covers warm-up + measurement).
    finished_ok(float)
        Measured ppm value (not rounded -- caller decides display precision).
    finished_err(str)
        Localized error message (device busy, no samples received, cancelled).
    """

    progress = Signal(float)
    finished_ok = Signal(float)
    finished_err = Signal(str)

    def __init__(
        self,
        info: SdrDeviceInfo,
        sample_rate_hz: float,
        duration_s: float = 30.0,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._info = info
        self._sample_rate = sample_rate_hz
        self._duration = duration_s

    def run(self) -> None:
        dev = SdrDevice(self._info)
        total_samples = 0
        elapsed = 0.0
        try:
            dev.set_sample_rate(self._sample_rate)
            if not dev.open():
                self.finished_err.emit(_("Could not open the SDR device — is it in use elsewhere?"))
                return
            if not dev.start_stream():
                self.finished_err.emit(_("Could not start streaming from the SDR device."))
                return

            total_duration = _WARMUP_S + self._duration
            t_start = time.monotonic()
            t_measure_start: float | None = None
            while True:
                now = time.monotonic()
                t_since_start = now - t_start
                if t_since_start >= total_duration:
                    break
                if self.isInterruptionRequested():
                    self.finished_err.emit(_("Measurement cancelled."))
                    return
                buf = dev.read_samples(_CHUNK)
                if t_since_start >= _WARMUP_S:
                    if t_measure_start is None:
                        t_measure_start = now
                    if buf is not None:
                        total_samples += len(buf)
                self.progress.emit(min(1.0, t_since_start / total_duration))
            if t_measure_start is not None:
                elapsed = time.monotonic() - t_measure_start
        finally:
            dev.close()

        if elapsed <= 0 or total_samples == 0:
            self.finished_err.emit(_("No samples received during measurement."))
            return

        actual_rate = total_samples / elapsed
        ppm = 1e6 * (actual_rate / self._sample_rate - 1.0)
        logger.info(
            "PPM measurement: %d samples over %.2fs, requested=%.0f actual=%.1f -> %.2f ppm",
            total_samples,
            elapsed,
            self._sample_rate,
            actual_rate,
            ppm,
        )
        self.finished_ok.emit(ppm)
