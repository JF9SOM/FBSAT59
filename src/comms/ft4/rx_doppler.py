"""RX-side residual Doppler correction in the audio domain (FT4 "ADC RX").

The rig's dial is not retuned during a receive period (FT-991 + slot-synchronous
CAT writes, see HamlibNetController.set_slot_sync): it sits at one frequency while
the satellite's Doppler shift keeps moving. A signal that should stay put therefore
slides across the audio passband by ``dial(t) - target(t)``. This module undoes that
in software: the period's audio is shifted by that same amount, as a function of
time, before it is decoded.

Sign: in USB the audio frequency is ``f_rf - dial``. A dial that is too high by e
(dial = target + e) puts the signal e below where it would have been, so the audio
is shifted *up* by ``dial - target``.

Pure numpy (no scipy): CI does not install it.
"""

from __future__ import annotations

import bisect
import threading
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 12_000
# How finely the target frequency is sampled across a period; the shift is
# interpolated linearly between these points.
_GRID_S = 0.25
# A read-back within this many Hz of the written value is the radio's own
# rounding (FT-991A tunes in 10 Hz steps), not a failed write.
CONFIRM_TOLERANCE_HZ = 10.0


class DialTrack:
    """Where the rig's dial actually was over time (a step function).

    ``add_write`` records a write that completed at time *t* (corrected-time
    seconds); ``confirm`` replaces the value of the latest write with what the
    radio reported when read back, so a write that was dropped or rounded is
    corrected for what really happened. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._times: list[float] = []
        self._values: list[float] = []

    def add_write(self, t: float, hz: float) -> None:
        with self._lock:
            self._times.append(t)
            self._values.append(float(hz))
            if len(self._times) > 64:
                del self._times[:-32]
                del self._values[:-32]

    def confirm(self, read_hz: float) -> bool:
        """Adopt what the radio reports as the dial.

        The reading is the truth -- including the radio's own rounding to its
        tuning step -- so it always replaces the recorded value. Returns True
        when it differed by more than rounding can explain (a write that was
        dropped or ignored), which is worth logging.
        """
        with self._lock:
            if not self._values:
                return False
            changed = abs(read_hz - self._values[-1]) > CONFIRM_TOLERANCE_HZ
            self._values[-1] = float(read_hz)
            return changed

    def value_at(self, t: float) -> float | None:
        """The dial at time *t*, or None before the first recorded write."""
        with self._lock:
            i = bisect.bisect_right(self._times, t) - 1
            if i < 0:
                return self._values[0] if self._values else None
            return self._values[i]

    def last(self) -> tuple[float, float] | None:
        with self._lock:
            return (self._times[-1], self._values[-1]) if self._times else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._times)


def _analytic(x: NDArray[np.float64]) -> NDArray[np.complex128]:
    """Analytic signal of a real array (FFT method, like scipy.signal.hilbert)."""
    n = len(x)
    spectrum = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1 : n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1 : (n + 1) // 2] = 2.0
    return np.asarray(np.fft.ifft(spectrum * h))


def shift_profile_hz(
    n_samples: int,
    t_end: float,
    dial: DialTrack,
    target_hz: Callable[[float], float | None],
    fs: int = SAMPLE_RATE,
) -> NDArray[np.float64] | None:
    """Per-sample audio shift ``dial(t) - target(t)`` in Hz, or None if unknown.

    The audio ends at *t_end* (the period boundary), so sample i was taken at
    ``t_end - (n_samples - i) / fs``. *target_hz(t)* is the ideal dial at time *t*.
    """
    duration = n_samples / fs
    t_start = t_end - duration
    grid_t = np.arange(0.0, duration + _GRID_S, _GRID_S)
    shifts = np.empty(len(grid_t))
    for k, g in enumerate(grid_t):
        t = t_start + float(g)
        d = dial.value_at(t)
        tgt = target_hz(t)
        if d is None or tgt is None:
            return None
        shifts[k] = d - tgt
    sample_t = np.arange(n_samples) / fs
    return np.asarray(np.interp(sample_t, grid_t, shifts))


def apply_rx_correction(
    audio: NDArray[np.float32],
    t_end: float,
    dial: DialTrack,
    target_hz: Callable[[float], float | None],
    fs: int = SAMPLE_RATE,
) -> tuple[NDArray[np.float32], tuple[float, float] | None]:
    """Shift *audio* by the dial-vs-target error; returns (audio, (min, max) shift in Hz).

    When the dial or the target is unknown the audio is returned untouched with None.
    """
    if len(audio) < fs:
        return audio, None
    shift = shift_profile_hz(len(audio), t_end, dial, target_hz, fs)
    if shift is None:
        return audio, None
    phase = 2.0 * np.pi * np.cumsum(shift) / fs
    corrected = np.real(_analytic(audio.astype(np.float64)) * np.exp(1j * phase))
    return corrected.astype(np.float32), (float(shift.min()), float(shift.max()))
