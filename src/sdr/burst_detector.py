"""Short-burst detector for the SDR waterfall.

Digital satellite downlinks (9k6 AX.25 beacons, telemetry frames, ...) are
often only 0.2-0.3 s long and only a few dB above the noise in every FFT bin,
so they vanish in an ordinary single-FFT-per-row waterfall. This module works
on power spectra averaged over one display row and marks the rows in which a
narrow band suddenly rises above its own recent history:

  1. Every FFT bin keeps a slowly-updating baseline (exponential average,
     _BASELINE_TAU_S). Anything stationary -- spur combs, a carrier that has
     been on for a while -- is absorbed into it and never fires.
  2. The ratio spectrum/baseline is smoothed over +/-4 kHz (roughly one
     narrow-band signal). Bins more than _EXCESS_DB above the baseline are hot.
  3. A row is a *burst* if the strongest hot region is at most
     _MAX_BURST_WIDTH_HZ wide and also stands out from the smoothed spectrum
     8 kHz on either side. A row is an *impulse* if a large part of the whole
     span is hot at once (broadband interference), which is grey in the UI and
     never counted.
  4. Consecutive burst rows form an event. An event that keeps going for more
     than _MAX_BURST_S is a carrier that has just appeared, not a burst: it
     stops being marked and is never counted.

The estimator and thresholds were validated offline against a real UHF 9k6
IQ recording (GRBBeta, 2026-09-20): all 5 real bursts were found, none of the
spur lines, and one broadband impulse was classified as such (see docs/sdr.md).

Pure numpy on purpose (no scipy): the unit tests run on CI without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np
from numpy.typing import NDArray

# FFT length used for the averaged spectrum. Same as SDRPipeline's own
# spectrum FFT so the rows line up with what the waterfall shows otherwise.
FFT_SIZE: int = 1024

# Time constant of the per-bin baseline (seconds).
_BASELINE_TAU_S: float = 20.0
# Detection is held off while the baseline is still a short running mean.
_WARMUP_S: float = 10.0

# A bin is "hot" when its smoothed power is this far above the baseline (dB),
# and a burst must additionally exceed its +/-8 kHz neighbourhood by as much.
_EXCESS_DB: float = 1.5
_SMOOTH_HALF_HZ: float = 4_000.0
_FLANK_OFFSET_HZ: float = 8_000.0

# The outer part of the span (receiver filter roll-off) is never searched.
_EDGE_FRACTION: float = 0.10
# Broadband impulse: at least this fraction of all bins hot in one row.
_IMPULSE_FRACTION: float = 0.4
# Widest hot region still accepted as a burst.
_MAX_BURST_WIDTH_HZ: float = 40_000.0
# Longest event still accepted as a burst (a 1200 baud AX.25 frame is ~2 s).
_MAX_BURST_S: float = 3.0
# An event ends when no burst row has been seen for this long.
_FINALIZE_GAP_S: float = 0.6

# S/N estimate: signal power is summed over +/-8 kHz around the burst peak and
# reported relative to the noise power in an 11 kHz channel -- the reference
# bandwidth of the decoder sensitivity measurements in docs/communications.md.
_SNR_HALF_HZ: float = 8_000.0
_SNR_REF_BW_HZ: float = 11_000.0


class RowKind(IntEnum):
    """Classification of one waterfall row."""

    NONE = 0
    BURST = 1
    IMPULSE = 2


@dataclass(frozen=True)
class BurstRow:
    """One display row: averaged spectrum plus the detector's verdict.

    ``burst_bins`` is the (first, last) bin index of the burst, ``snr_db`` its
    estimated S/N in an 11 kHz channel (None if it could not be estimated),
    ``event_id`` the burst event this row belongs to and ``event_count`` the
    number of finished, counted burst events since the detector was created.
    """

    freqs_hz: NDArray[np.float64]
    power_dbfs: NDArray[np.float32]
    kind: RowKind
    burst_bins: tuple[int, int] | None
    snr_db: float | None
    event_id: int | None
    event_count: int
    warming_up: bool


def _box_filter(x: NDArray[np.float64], half: int) -> NDArray[np.float64]:
    """Moving average over 2*half+1 samples with edge replication."""
    width = 2 * half + 1
    padded = np.pad(x, half, mode="edge")
    csum = np.concatenate(([0.0], np.cumsum(padded)))
    result: NDArray[np.float64] = (csum[width:] - csum[:-width]) / width
    return result


class BurstDetector:
    """Feed I/Q blocks with feed(), then call finish_row() once per display row.

    Not thread-safe: feed() and finish_row() must be called from one thread
    (the SDRPipeline thread).
    """

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._bin_hz = self._sr / FFT_SIZE
        self._window = np.blackman(FFT_SIZE).astype(np.float32)

        self._half = max(1, round(_SMOOTH_HALF_HZ / self._bin_hz))
        self._flank = max(round(_FLANK_OFFSET_HZ / self._bin_hz), 2 * self._half + 1)
        self._snr_half = max(1, round(_SNR_HALF_HZ / self._bin_hz))
        self._edge = int(FFT_SIZE * _EDGE_FRACTION)

        # Power accumulated since the last finish_row().
        self._acc = np.zeros(FFT_SIZE, dtype=np.float64)
        self._frames = 0

        self._base: NDArray[np.float64] | None = None
        self._base_rows = 0
        self._t = 0.0  # seconds of signal processed so far

        # Event tracking.
        self._ev_active = False
        self._ev_id = 0
        self._ev_start = 0.0
        self._ev_last = 0.0
        self._ev_carrier = False
        self._count = 0

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------

    def feed(self, iq: NDArray[np.complex64]) -> None:
        """Accumulate the power spectrum of one I/Q block (any length)."""
        n = len(iq) // FFT_SIZE
        if n == 0:
            return
        frames = iq[: n * FFT_SIZE].reshape(n, FFT_SIZE) * self._window
        spec = np.fft.fft(frames, axis=1)
        power = (spec.real**2 + spec.imag**2) / float(FFT_SIZE * FFT_SIZE)
        self._acc += power.sum(axis=0, dtype=np.float64)
        self._frames += n

    def finish_row(self, center_freq_hz: float) -> BurstRow | None:
        """Close the current row; None if no samples arrived since the last one."""
        if self._frames == 0:
            return None
        psd = np.fft.fftshift(self._acc / self._frames)
        row_s = self._frames * FFT_SIZE / self._sr
        self._acc[:] = 0.0
        self._frames = 0
        self._t += row_s

        power_dbfs = (10.0 * np.log10(psd + 1e-24)).astype(np.float32)
        freqs = center_freq_hz + np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, d=1.0 / self._sr))
        warming = self._t < _WARMUP_S

        kind = RowKind.NONE
        bins: tuple[int, int] | None = None
        snr: float | None = None
        if self._base is None:
            self._base = psd.copy()
            self._base_rows = 1
        else:
            if not warming:
                kind, bins, snr = self._classify(psd, self._base)
            self._base_rows += 1
            alpha = min(1.0, max(1.0 / self._base_rows, row_s / _BASELINE_TAU_S))
            self._base += alpha * (psd - self._base)

        kind, event_id = self._track_event(kind)
        if kind != RowKind.BURST:
            bins = None
            snr = None
        return BurstRow(
            freqs_hz=freqs,
            power_dbfs=power_dbfs,
            kind=kind,
            burst_bins=bins,
            snr_db=snr,
            event_id=event_id,
            event_count=self._count,
            warming_up=warming,
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(
        self, psd: NDArray[np.float64], base: NDArray[np.float64]
    ) -> tuple[RowKind, tuple[int, int] | None, float | None]:
        """Classify one row against the baseline (before it is folded in)."""
        safe_base = np.maximum(base, 1e-30)
        smoothed = _box_filter(psd / safe_base, self._half)
        cdb = 10.0 * np.log10(np.maximum(smoothed, 1e-12))
        hot = cdb > _EXCESS_DB
        if int(hot.sum()) >= _IMPULSE_FRACTION * FFT_SIZE:
            return RowKind.IMPULSE, None, None

        lo, hi = self._edge, FFT_SIZE - self._edge
        peak = lo + int(np.argmax(cdb[lo:hi]))
        if not hot[peak]:
            return RowKind.NONE, None, None

        first = peak
        while first - 1 >= lo and hot[first - 1]:
            first -= 1
        last = peak
        while last + 1 < hi and hot[last + 1]:
            last += 1
        if (last - first + 1) * self._bin_hz > _MAX_BURST_WIDTH_HZ:
            return RowKind.NONE, None, None

        flank = 0.5 * (cdb[max(peak - self._flank, 0)] + cdb[min(peak + self._flank, FFT_SIZE - 1)])
        if cdb[peak] - flank <= _EXCESS_DB:
            return RowKind.NONE, None, None

        return RowKind.BURST, (first, last), self._estimate_snr(psd, safe_base, peak)

    def _estimate_snr(
        self, psd: NDArray[np.float64], base: NDArray[np.float64], peak: int
    ) -> float | None:
        """S/N of a burst in an 11 kHz channel (signal above baseline / baseline)."""
        start = max(peak - self._snr_half, 0)
        stop = min(peak + self._snr_half + 1, FFT_SIZE)
        signal = float((psd[start:stop] - base[start:stop]).sum())
        noise = float(base[start:stop].sum())
        if signal <= 0.0 or noise <= 0.0:
            return None
        width_hz = (stop - start) * self._bin_hz
        return 10.0 * float(np.log10(signal / noise * width_hz / _SNR_REF_BW_HZ))

    # ------------------------------------------------------------------
    # Event tracking
    # ------------------------------------------------------------------

    def _track_event(self, kind: RowKind) -> tuple[RowKind, int | None]:
        """Group burst rows into events; drop events that turn out to be carriers."""
        if kind == RowKind.BURST:
            if not self._ev_active:
                self._ev_active = True
                self._ev_id += 1
                self._ev_start = self._t
                self._ev_carrier = False
            self._ev_last = self._t
            if self._t - self._ev_start > _MAX_BURST_S:
                self._ev_carrier = True
            if self._ev_carrier:
                return RowKind.NONE, None
            return RowKind.BURST, self._ev_id
        if self._ev_active and self._t - self._ev_last > _FINALIZE_GAP_S:
            if not self._ev_carrier:
                self._count += 1
            self._ev_active = False
        return kind, None
