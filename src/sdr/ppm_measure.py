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

# Larger than the pipeline's usual 16384-65536 chunk sizes on purpose: each
# read_samples() call has fixed Python-side overhead (a fresh np.zeros()
# allocation, a readStream() round-trip). At a high sample rate that
# overhead, repeated often enough, can make this thread fall behind the
# device's real throughput and trigger the very overflow this tool is
# trying to measure past -- fewer, bigger reads reduce that risk.
_CHUNK = 262144
_WARMUP_S = 5.0  # discard the first few seconds -- clock is least stable right after open()
_PROGRESS_INTERVAL_S = 0.15  # throttle progress emits; cross-thread Qt signals aren't free


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
        overflows_during_measurement = 0
        # Read until this many samples have actually been *received*, rather
        # than reading for a fixed wall-clock duration. A time-based cutoff
        # can end while data is still in flight inside the USB/driver
        # buffer -- not lost, just not yet delivered to this process -- and
        # silently excluding that not-yet-arrived data biases the result
        # without ever touching read_samples()'s overflow path. Waiting for
        # an exact sample count instead means the clock only stops once
        # every counted sample has actually been observed.
        target_samples = int(self._sample_rate * self._duration)
        warmup_frac = _WARMUP_S / (_WARMUP_S + self._duration)
        # Safety net in case the device stalls completely after warm-up --
        # without this, a dead stream would hang the measurement forever
        # since the sample-count target would never be reached.
        max_wall_time = _WARMUP_S + self._duration * 4.0
        try:
            dev.set_sample_rate(self._sample_rate)
            if not dev.open():
                self.finished_err.emit(_("Could not open the SDR device — is it in use elsewhere?"))
                return
            if not dev.start_stream():
                self.finished_err.emit(_("Could not start streaming from the SDR device."))
                return

            t_start = time.monotonic()
            last_progress_emit = 0.0

            # Warm-up: discard for a fixed wall-clock duration -- the clock
            # is least stable right after open(), and this phase feeds no
            # data into the ppm calculation, so a time-based cutoff is fine
            # here (there's nothing downstream for in-flight data to bias).
            while True:
                now = time.monotonic()
                t_since_start = now - t_start
                if t_since_start >= _WARMUP_S:
                    break
                if self.isInterruptionRequested():
                    self.finished_err.emit(_("Measurement cancelled."))
                    return
                dev.read_samples(_CHUNK)
                if now - last_progress_emit >= _PROGRESS_INTERVAL_S:
                    last_progress_emit = now
                    self.progress.emit(warmup_frac * min(1.0, t_since_start / _WARMUP_S))

            # Measurement: read until target_samples have actually arrived.
            overflow_at_measure_start = dev.overflow_count
            t_measure_start = time.monotonic()
            while total_samples < target_samples:
                now = time.monotonic()
                if now - t_start >= max_wall_time:
                    self.finished_err.emit(_("Measurement timed out — no data from the device."))
                    return
                if self.isInterruptionRequested():
                    self.finished_err.emit(_("Measurement cancelled."))
                    return
                buf = dev.read_samples(_CHUNK)
                if buf is not None:
                    total_samples += len(buf)
                if now - last_progress_emit >= _PROGRESS_INTERVAL_S:
                    last_progress_emit = now
                    frac = warmup_frac + (1 - warmup_frac) * min(
                        1.0, total_samples / target_samples
                    )
                    self.progress.emit(frac)
            elapsed = time.monotonic() - t_measure_start
            overflows_during_measurement = dev.overflow_count - overflow_at_measure_start
        finally:
            dev.close()

        if elapsed <= 0 or total_samples == 0:
            self.finished_err.emit(_("No samples received during measurement."))
            return

        if overflows_during_measurement > 0:
            # The driver dropped samples faster than we could read them, so
            # total_samples/elapsed understates the device's real throughput
            # -- the resulting "ppm" would be a measurement artifact, not the
            # device's actual clock error, and can be off by hundreds of ppm
            # or more. Discard rather than report a number that looks
            # trustworthy but isn't.
            logger.warning(
                "PPM measurement discarded: %d buffer overflow(s) during the "
                "measurement window (requested=%.0f)",
                overflows_during_measurement,
                self._sample_rate,
            )
            self.finished_err.emit(
                _(
                    "Buffer overflow during measurement — result discarded as "
                    "unreliable. Try a lower sample rate or close other apps "
                    "using the CPU, then measure again."
                )
            )
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
