"""Detect the end of ARICA-2's keyed CW beacon from raw SDR I/Q.

ARICA-2's message box accepts uplinks only for about 15 seconds right after
its CW beacon stops. The beacon is an unmodulated carrier keyed on and off, so
"the beacon just ended" is "a narrow spectral line that was present for a
while has disappeared". The detector looks for that line near the tuned
frequency (Doppler already corrected by the SDR pipeline; ARICA-2's carrier
sits roughly 1 kHz below nominal) and needs no CW decoding at all.

The 4800 baud GMSK downlink is deliberately not mistaken for the carrier: it
spreads its power over several kHz, so it raises no single bin far above the
median of the band, which is the measure used here.

The detector is deliberately free of Qt and of wall-clock time. Time is counted
in input samples; the caller turns an event's ``remaining_s`` into a wall-clock
deadline using the moment it received the event.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

# Work rate after the crude boxcar decimation (Hz, approximate).
_TARGET_RATE_HZ = 24_000.0
# FFT length of one analysis frame at the decimated rate (about 85 ms).
_FRAME_LEN = 2048
# Spectral region searched around the tuned frequency, and the centre part left
# out (RTL-SDR style DC spike).
SEARCH_HZ = 3_000.0
DC_EXCLUDE_HZ = 250.0

# Peak bin over the band median. Pure noise already reaches ~8 dB (the largest of
# ~140 exponentially distributed bins) and exceeds 10 dB in about one frame in
# eight, so the threshold has to sit well above that.
DEFAULT_THRESHOLD_DB = 14.0
# Carrier absent this long => the beacon item is over. CW letter gaps are far shorter.
DEFAULT_GAP_S = 1.5
# A carrier shorter than this is not a beacon item (noise, a passing line).
DEFAULT_MIN_BURST_S = 3.0
# How long the uplink window stays open after the beacon stops.
DEFAULT_WINDOW_S = 15.0


@dataclass(frozen=True)
class BeaconEnded:
    """The carrier has stopped; the uplink window is open.

    *remaining_s* is how much of the window is left at the moment the event is
    reported (the detector needs ``gap_s`` of silence to be sure, which counts
    against the window).
    """

    burst_s: float
    remaining_s: float


class BeaconWindowDetector:
    """Feeds on I/Q blocks; reports each time a beacon burst has ended."""

    def __init__(
        self,
        sample_rate: float,
        threshold_db: float = DEFAULT_THRESHOLD_DB,
        gap_s: float = DEFAULT_GAP_S,
        min_burst_s: float = DEFAULT_MIN_BURST_S,
        window_s: float = DEFAULT_WINDOW_S,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._decim = max(1, round(sample_rate / _TARGET_RATE_HZ))
        self._rate = sample_rate / self._decim
        self._threshold_db = threshold_db
        self._gap_s = gap_s
        self._min_burst_s = min_burst_s
        self._window_s = window_s

        freqs = np.fft.fftfreq(_FRAME_LEN, d=1.0 / self._rate)
        self._band = (np.abs(freqs) <= SEARCH_HZ) & (np.abs(freqs) >= DC_EXCLUDE_HZ)
        self._hann = np.hanning(_FRAME_LEN).astype(np.float32)

        self._pending: NDArray[np.complex64] = np.zeros(0, dtype=np.complex64)
        self._frame_buf: NDArray[np.complex64] = np.zeros(0, dtype=np.complex64)
        self._t = 0.0  # seconds of input consumed (end of the last analysed frame)
        self._burst_start: float | None = None
        self._last_seen = 0.0
        self.last_level_db = 0.0
        self.carrier_present = False

    def reset(self) -> None:
        """Forget the buffered samples and any burst in progress."""
        self._pending = np.zeros(0, dtype=np.complex64)
        self._frame_buf = np.zeros(0, dtype=np.complex64)
        self._burst_start = None
        self.carrier_present = False

    def push_samples(self, iq: NDArray[np.complex64]) -> list[BeaconEnded]:
        """Analyse one I/Q block; return the beacon-end events it completed."""
        events: list[BeaconEnded] = []
        data = np.concatenate((self._pending, np.asarray(iq, dtype=np.complex64)))
        usable = (len(data) // self._decim) * self._decim
        self._pending = data[usable:]
        if usable == 0:
            return events
        decimated = data[:usable].reshape(-1, self._decim).mean(axis=1).astype(np.complex64)
        self._frame_buf = np.concatenate((self._frame_buf, decimated))
        while len(self._frame_buf) >= _FRAME_LEN:
            frame = self._frame_buf[:_FRAME_LEN]
            self._frame_buf = self._frame_buf[_FRAME_LEN:]
            self._t += _FRAME_LEN / self._rate
            event = self._analyse(frame)
            if event is not None:
                events.append(event)
        return events

    def _analyse(self, frame: NDArray[np.complex64]) -> BeaconEnded | None:
        spectrum = np.abs(np.fft.fft(frame * self._hann)) ** 2
        band = spectrum[self._band]
        floor = float(np.median(band)) + 1e-20
        self.last_level_db = 10.0 * float(np.log10(float(band.max()) / floor + 1e-20))
        present = self.last_level_db >= self._threshold_db
        self.carrier_present = present

        if present:
            if self._burst_start is None:
                self._burst_start = self._t
            self._last_seen = self._t
            return None

        if self._burst_start is None or self._t - self._last_seen < self._gap_s:
            return None
        burst_s = self._last_seen - self._burst_start
        self._burst_start = None
        if burst_s < self._min_burst_s:
            return None
        remaining = self._window_s - (self._t - self._last_seen)
        return BeaconEnded(burst_s=burst_s, remaining_s=max(0.0, remaining))
