"""Q65 T/R period scheduler.

Q65 uses UTC-aligned transmission periods of 60, 30, or 15 seconds.
This module provides timing utilities to determine the current period
phase and when the next period boundary occurs.

EME standard: Q65-60A (60-second period, submode A).
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.clock_offset import corrected_time, corrected_utcnow


class Q65Scheduler:
    """Manages Q65 T/R period timing.

    The period boundary is always aligned to UTC second 0 within each
    period group.  For a 60-second period: boundaries at :00 of each
    minute.  For 30-second: :00 and :30.  For 15-second: :00, :15,
    :30, :45.

    Args:
        period_seconds: 60, 30, or 15.
    """

    def __init__(self, period_seconds: int = 60) -> None:
        if period_seconds not in (15, 30, 60):
            raise ValueError(f"period_seconds must be 15, 30, or 60; got {period_seconds}")
        self._period = period_seconds

    @property
    def period_seconds(self) -> int:
        return self._period

    def utc_now(self) -> datetime:
        return corrected_utcnow()

    def period_phase(self) -> float:
        """Return seconds elapsed since the last period boundary (0.0 – period)."""
        now = corrected_time()
        return now % self._period

    def seconds_to_next_boundary(self) -> float:
        """Return seconds until the next T/R period boundary."""
        phase = self.period_phase()
        return self._period - phase

    def period_index(self) -> int:
        """Return the current period index within one UTC minute (0-based)."""
        now = corrected_time()
        return int(now % 60) // self._period

    def rx_start_time(self) -> datetime:
        """Return the UTC datetime of the last period boundary (RX window start)."""
        now = corrected_time()
        boundary = now - self.period_phase()
        return datetime.fromtimestamp(boundary, UTC)

    def countdown_str(self) -> str:
        """Return a human-readable countdown string like '00:42 / 60'."""
        phase = self.period_phase()
        remaining = self._period - phase
        return f"{int(remaining):02d}s / {self._period}"

    def period_label(self, submode: str = "A") -> str:
        """Return a display label like 'Q65-60A'."""
        return f"Q65-{self._period}{submode}"

    def audio_buffer_samples(self, sample_rate: int = 12_000) -> int:
        """Return the number of audio samples in one full period."""
        return self._period * sample_rate
