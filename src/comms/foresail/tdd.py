"""Foresail-1p's windowed TDD: when the satellite transmits and when we may.

Skylink is half duplex: a node transmits in its own window, then the peer in its window.
Every satellite frame carries a TDD control extension ``(window, remaining)`` in
milliseconds. In a real recording (2026-10-18) the frames of one window read
``(250, 248)``, ``(250, 140)``, ``(250, 2)`` at 2.678, 2.782 and 2.920 s: the window ends at
*frame start + remaining* (2.926, 2.922, 2.922 s), and the next window started 1.02 s after the
first. The satellite is silent between its beacons (about every 60 s), so outside a beacon
it can hear us at any time; inside one we must keep out of its windows.

``TddTracker`` follows the windows of the frames heard and tells when a transmission can
start without overlapping one. How long after a window the satellite really listens (its gap
and switching delay) is not published, so the guard time is a setting to tune on real passes.
"""

from __future__ import annotations

from dataclasses import dataclass

# Wait this long after a satellite window ends before starting to transmit.
DEFAULT_GUARD_S = 0.30
# Keep this much clear of the next predicted satellite window.
DEFAULT_MARGIN_S = 0.10
# Window cycles seen further apart than this are not one beacon's consecutive windows.
_MAX_CYCLE_S = 3.0
_MIN_CYCLE_S = 0.3


@dataclass(frozen=True)
class TxAdvice:
    """Whether to start now, and if not how long to wait."""

    wait_s: float
    reason: str

    @property
    def go(self) -> bool:
        return self.wait_s <= 0.0


class TddTracker:
    """Tracks the satellite's transmit windows from the TDD extension of received frames."""

    def __init__(self) -> None:
        self._window_start: float | None = None
        self._window_end: float | None = None
        self._cycle_s: float | None = None

    def reset(self) -> None:
        self._window_start = None
        self._window_end = None
        self._cycle_s = None

    @property
    def window_end(self) -> float | None:
        """When the last heard satellite window ends (monotonic seconds), if any was heard."""
        return self._window_end

    @property
    def cycle_s(self) -> float | None:
        """Time between consecutive satellite windows of one beacon, once two were heard."""
        return self._cycle_s

    def observe(self, frame_start: float, window_ms: int, remaining_ms: int) -> None:
        """Note a satellite frame that started at *frame_start* (monotonic seconds)."""
        end = frame_start + remaining_ms / 1000.0
        start = end - window_ms / 1000.0
        previous_start = self._window_start
        previous_end = self._window_end
        if previous_end is not None and abs(end - previous_end) < 0.05:
            # Another frame of the same window: refine the end estimate.
            self._window_end = (previous_end + end) / 2.0
            return
        if previous_start is not None:
            cycle = start - previous_start
            if _MIN_CYCLE_S <= cycle <= _MAX_CYCLE_S:
                self._cycle_s = cycle
            else:
                self._cycle_s = None
        self._window_start = start
        self._window_end = end

    def in_satellite_window(self, now: float) -> bool:
        """Whether *now* lies inside the last heard satellite window."""
        if self._window_start is None or self._window_end is None:
            return False
        return self._window_start <= now <= self._window_end

    def advice(
        self,
        now: float,
        tx_len_s: float,
        *,
        guard_s: float = DEFAULT_GUARD_S,
        margin_s: float = DEFAULT_MARGIN_S,
    ) -> TxAdvice:
        """When a transmission of *tx_len_s* seconds can start without overlapping a window."""
        end = self._window_end
        start = self._window_start
        if end is None or start is None:
            return TxAdvice(0.0, "no satellite frames heard")
        cycle = self._cycle_s
        # The beacon may still have another window after the last one heard.
        beacon_going = now - end < (cycle if cycle is not None else _MAX_CYCLE_S)
        if not beacon_going:
            return TxAdvice(0.0, "satellite quiet")
        earliest = end + guard_s
        if now < earliest:
            return TxAdvice(earliest - now, "satellite window")
        if cycle is not None:
            next_start = start + cycle
            while next_start + 0.25 < now:  # windows that have passed
                next_start += cycle
            if now + tx_len_s + margin_s > next_start and now < next_start + 0.25 + guard_s:
                # Our frame would run into the next satellite window: go after it.
                return TxAdvice(next_start + 0.25 + guard_s - now, "next satellite window")
        return TxAdvice(0.0, "between satellite windows")
