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

Measurement method: least-squares fit over every buffer arrival, not a
2-point (first/last) slope
------------------------------------------------------------------------
Two earlier versions of this worker computed ppm from just
total_samples / elapsed_wall_time, using only the first and last buffer
arrivals to define the measurement window. Diagnostic logging showed
that approach swinging between +1700ppm and +3615ppm on back-to-back runs
on the same hardware, regardless of whether the window boundaries were
time-based or sample-count-based, or whether backlogged data was drained
before starting the clock. The root cause: on RTL-SDR (and likely other
cheap SDRs), SoapySDR's readStream() delivers data in fixed-size chunks
(the driver's internal USB transfer buffer -- 131072 samples for RTL-SDR)
that complete on the hardware's own clock, roughly every
131072/sample_rate seconds, independent of when this thread happens to be
polling. Software only ever *observes* a chunk once some read_samples()
call happens to catch it already-complete -- so the very first and very
last chunks of any given window carry up to one full chunk-period
(~131ms at 1Msps) of essentially random phase error relative to the
window's official start/end timestamps. Over a 30s measurement that is a
worst case of roughly 131ms/30s * 1e6 =~ 4400ppm, which matches the
magnitude of what was actually observed.

The fix: don't rely on just the first and last arrivals. Record every
successful chunk's arrival as an (elapsed_time, cumulative_sample_count)
data point (a 30s run yields roughly 230 of them) and fit a straight line
to all of them with least squares; the slope is the measured sample rate.
Each individual point still carries the same up-to-one-chunk-period phase
jitter as before, but that jitter is a roughly constant *offset* added to
every point's timestamp (software sees each chunk within [0, one poll
interval) after it's actually ready) -- a constant offset shifts a
least-squares fit's intercept, not its slope. With ~230 points spanning
the whole window, per-point jitter averages out far more effectively than
the 2-point method's baked-in worst case.
"""

from __future__ import annotations

import logging
import time

import numpy as np
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
# Below this many recorded buffer arrivals, a least-squares fit is not
# meaningfully more robust than the 2-point method it replaces.
_MIN_DATA_POINTS = 5


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
        # (elapsed_s_since_measurement_start, cumulative_sample_count) for
        # every successfully-received buffer during the measurement phase.
        data_points: list[tuple[float, int]] = []
        total_samples = 0
        overflows_during_measurement = 0
        warmup_frac = _WARMUP_S / (_WARMUP_S + self._duration)

        logger.info(
            "PPM measure: starting. driver=%s requested_rate=%.0f duration=%.1fs chunk=%d",
            self._info.driver,
            self._sample_rate,
            self._duration,
            _CHUNK,
        )

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
            # data into the ppm calculation.
            warmup_calls = 0
            warmup_samples_drained = 0
            warmup_none_count = 0
            while True:
                now = time.monotonic()
                t_since_start = now - t_start
                if t_since_start >= _WARMUP_S:
                    break
                if self.isInterruptionRequested():
                    self.finished_err.emit(_("Measurement cancelled."))
                    return
                buf = dev.read_samples(_CHUNK)
                warmup_calls += 1
                if buf is None:
                    warmup_none_count += 1
                else:
                    warmup_samples_drained += len(buf)
                if now - last_progress_emit >= _PROGRESS_INTERVAL_S:
                    last_progress_emit = now
                    self.progress.emit(warmup_frac * min(1.0, t_since_start / _WARMUP_S))

            logger.info(
                "PPM measure: warm-up done. calls=%d none_returns=%d samples_drained=%d "
                "(discarded, not counted below) overflow_count_so_far=%d",
                warmup_calls,
                warmup_none_count,
                warmup_samples_drained,
                dev.overflow_count,
            )

            # Measurement: record every successful buffer arrival as a
            # (time, cumulative_samples) point for the requested duration.
            # See the module docstring for why this replaced a simple
            # total_samples/elapsed 2-point calculation.
            overflow_at_measure_start = dev.overflow_count
            t_measure_start = time.monotonic()
            measure_calls = 0
            measure_none_count = 0
            min_read: int | None = None
            max_read = 0
            last_log = t_measure_start
            while True:
                now = time.monotonic()
                elapsed_so_far = now - t_measure_start
                if elapsed_so_far >= self._duration:
                    break
                if self.isInterruptionRequested():
                    self.finished_err.emit(_("Measurement cancelled."))
                    return
                buf = dev.read_samples(_CHUNK)
                measure_calls += 1
                if buf is None:
                    measure_none_count += 1
                else:
                    n = len(buf)
                    total_samples += n
                    data_points.append((time.monotonic() - t_measure_start, total_samples))
                    if min_read is None or n < min_read:
                        min_read = n
                    if n > max_read:
                        max_read = n
                if now - last_progress_emit >= _PROGRESS_INTERVAL_S:
                    last_progress_emit = now
                    frac = warmup_frac + (1 - warmup_frac) * min(
                        1.0, elapsed_so_far / self._duration
                    )
                    self.progress.emit(frac)
                if now - last_log >= 2.0:
                    last_log = now
                    running_rate = total_samples / elapsed_so_far if elapsed_so_far > 0 else 0.0
                    logger.info(
                        "PPM measure: progress calls=%d total_samples=%d data_points=%d "
                        "elapsed=%.2fs running_actual_rate=%.1f overflow_count=%d",
                        measure_calls,
                        total_samples,
                        len(data_points),
                        elapsed_so_far,
                        running_rate,
                        dev.overflow_count,
                    )
            overflows_during_measurement = dev.overflow_count - overflow_at_measure_start
            logger.info(
                "PPM measure: measurement done. calls=%d none_returns=%d data_points=%d "
                "min_read=%s max_read=%d total_samples=%d overflows_during_measurement=%d",
                measure_calls,
                measure_none_count,
                len(data_points),
                min_read,
                max_read,
                total_samples,
                overflows_during_measurement,
            )
        finally:
            dev.close()

        if overflows_during_measurement > 0:
            # The driver dropped samples faster than we could read them, so
            # the recorded cumulative counts understate the device's real
            # throughput -- the resulting "ppm" would be a measurement
            # artifact, not the device's actual clock error, and can be off
            # by hundreds of ppm or more. Discard rather than report a
            # number that looks trustworthy but isn't.
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

        if len(data_points) < _MIN_DATA_POINTS:
            logger.warning(
                "PPM measurement discarded: only %d data point(s) received "
                "(need at least %d) -- device may be stalled or disconnected.",
                len(data_points),
                _MIN_DATA_POINTS,
            )
            self.finished_err.emit(_("No samples received during measurement."))
            return

        times = np.array([t for t, _n in data_points], dtype=np.float64)
        counts = np.array([n for _t, n in data_points], dtype=np.float64)
        slope, intercept = np.polyfit(times, counts, 1)
        predicted = slope * times + intercept
        rms_residual = float(np.sqrt(np.mean((counts - predicted) ** 2)))
        naive_rate = total_samples / times[-1] if times[-1] > 0 else 0.0

        ppm = 1e6 * (slope / self._sample_rate - 1.0)
        logger.info(
            "PPM measurement: %d data points over %.2fs, requested=%.0f fitted_rate=%.2f "
            "-> %.2f ppm (naive rate=%.2f -> %.2f ppm, fit rms_residual=%.1f samples)",
            len(data_points),
            times[-1],
            self._sample_rate,
            slope,
            ppm,
            naive_rate,
            1e6 * (naive_rate / self._sample_rate - 1.0),
            rms_residual,
        )
        self.finished_ok.emit(ppm)
