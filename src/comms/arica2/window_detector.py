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


# ---------------------------------------------------------------------------
# Detection from the rig's receive audio (no SDR)
# ---------------------------------------------------------------------------

# One analysis frame of the audio detector.
_AUDIO_FRAME_S = 0.05
# Key-down (carrier present) shows up in an FM receiver's audio as quieting: the
# discriminator noise drops to a fraction of its no-signal level. A frame is "key
# down" when its level is below this fraction of the noise reference.
AUDIO_QUIET_RATIO = 0.55
# The noise reference is a high percentile of the recent frame levels: the beacon
# keys the carrier on for roughly half of its cycle, so a median could land on the
# quiet level, while a percentile above the on-air share stays on the noise.
_AUDIO_REF_PERCENTILE = 70.0
_AUDIO_REF_SPAN_S = 30.0
# No verdict until this much audio has been seen (the reference is not meaningful yet).
_AUDIO_WARMUP_S = 5.0
# The beacon's CW pauses between words for up to ~2.4 s (measured on a real pass,
# 2026-10-09); the audio detector therefore waits longer than the I/Q one before
# it calls the item over. The wait is part of the 15 s window.
AUDIO_GAP_S = 3.0
# Weak passes make the carrier fade in and out, so short runs of key-down are common
# and unreliable; a real beacon item lasts ~20 s. Only a run of at least this long counts.
AUDIO_MIN_BURST_S = 10.0


class AudioBeaconWindowDetector:
    """Same job as :class:`BeaconWindowDetector`, but from the rig's audio.

    For a rig-only setup (no SDR): the receiver's audio is level-analysed in 50 ms
    frames, and the beacon's keyed carrier is recognised as a run of "quiet" frames
    (see ``AUDIO_QUIET_RATIO``). Like the I/Q detector it needs no CW decoding, reports
    :class:`BeaconEnded` once ``gap_s`` has passed without key-down, and counts that gap
    against the window.

    Limits: it needs the receiver's audio to carry noise (an open squelch / data
    audio). Our own transmissions mute the receive audio, which looks like key-down,
    so the caller must wrap each one in :meth:`set_transmitting`.

    Time is counted in input samples, like the I/Q detector. Not thread safe except
    for ``set_transmitting`` (a plain flag) and the read-only attributes.
    """

    def __init__(
        self,
        sample_rate: float = 48_000.0,
        quiet_ratio: float = AUDIO_QUIET_RATIO,
        gap_s: float = AUDIO_GAP_S,
        min_burst_s: float = AUDIO_MIN_BURST_S,
        window_s: float = DEFAULT_WINDOW_S,
        hold_after_tx_s: float = 0.6,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._frame_len = max(1, round(sample_rate * _AUDIO_FRAME_S))
        self._quiet_ratio = quiet_ratio
        self._gap_s = gap_s
        self._min_burst_s = min_burst_s
        self._window_s = window_s
        self._hold_after_tx_s = hold_after_tx_s
        self._levels: list[float] = []
        self._max_levels = round(_AUDIO_REF_SPAN_S / _AUDIO_FRAME_S)
        self._pending: NDArray[np.float32] = np.zeros(0, dtype=np.float32)
        self._t = 0.0
        self._burst_start: float | None = None
        self._last_seen = 0.0
        self._transmitting = False
        self._mask_until = 0.0
        self.carrier_present = False

    def set_transmitting(self, transmitting: bool) -> None:
        """Ignore the audio while our own transmission mutes the receiver.

        Audio is also ignored for ``hold_after_tx_s`` after the end, while the
        receiver comes back.
        """
        if self._transmitting and not transmitting:
            self._mask_until = self._t + self._hold_after_tx_s
        self._transmitting = transmitting

    def reset(self) -> None:
        """Forget the buffered audio, the noise reference and any burst in progress."""
        self._levels.clear()
        self._pending = np.zeros(0, dtype=np.float32)
        self._burst_start = None
        self.carrier_present = False

    def push_samples(self, audio: NDArray[np.float32]) -> list[BeaconEnded]:
        """Analyse a block of mono receive audio; return the beacon-end events it completed."""
        events: list[BeaconEnded] = []
        data = np.concatenate((self._pending, np.asarray(audio, dtype=np.float32)))
        n_frames = len(data) // self._frame_len
        self._pending = data[n_frames * self._frame_len :]
        for k in range(n_frames):
            frame = data[k * self._frame_len : (k + 1) * self._frame_len]
            self._t += _AUDIO_FRAME_S
            event = self._analyse(float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))))
            if event is not None:
                events.append(event)
        return events

    def _analyse(self, level: float) -> BeaconEnded | None:
        if self._transmitting or self._t < self._mask_until:
            return None  # our own transmission: neither signal nor noise reference
        self._levels.append(level)
        if len(self._levels) > self._max_levels:
            del self._levels[0]
        if self._t < _AUDIO_WARMUP_S:
            return None
        reference = float(np.percentile(self._levels, _AUDIO_REF_PERCENTILE))
        present = reference > 0.0 and level < self._quiet_ratio * reference
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
        self.carrier_present = False
        if burst_s < self._min_burst_s:
            return None
        remaining = self._window_s - (self._t - self._last_seen)
        return BeaconEnded(burst_s=burst_s, remaining_s=max(0.0, remaining))
