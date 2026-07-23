"""FT4 6-second period scheduler.

FT4 divides UTC time into interleaved 6-second slots:
  even slots: floor(t/6) % 2 == 0  (0–6 s, 12–18 s, …)
  odd  slots: floor(t/6) % 2 == 1  (6–12 s, 18–24 s, …)

`tx_even` only decides which slot parity *this station* is allowed to
transmit in when TX Enable is on — it says nothing about who else is on
the air. Other stations may be calling CQ or exchanging on either parity
depending on who initiated their own QSO, so a station that is idle or
just monitoring must decode every single 6-second slot, not only the
slots it considers "RX". (Originally `rx_period_ended` fired only for the
non-TX-parity slot, silently discarding the other slot's audio every
cycle — this is what caused only ~half of on-air FT4 activity to ever
reach the decoder, see ui/ft4_tab.py, 2026-07-10.)

One station transmits in even slots, the other in odd slots.  The caller
decides which role to take via set_tx_even().

This scheduler only drives the TX-turn decision and the countdown/TX-RX
indicator display now — RX audio capture and decode triggering moved to
comms.ft4.rx_capture.Ft4RxCaptureWorker, which runs its own
time.sleep()-based thread instead of this QTimer, so it can never be
delayed by whatever else is happening on the Qt main thread (see that
module's docstring). This scheduler's own boundary-crossing lag is still
logged here purely as a diagnostic, to compare against
Ft4RxCaptureWorker's — see boundary_lag below.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from comms.ft4.decode_log import get_ft4_decode_logger
from core.clock_offset import corrected_time


class Ft4Scheduler(QObject):
    """Drives FT4 timing with 100 ms resolution.

    Signals:
        period_tick(is_tx, seconds_remaining):
            Fired ~10× per second.  is_tx reflects the current slot role.
        period_changed(is_tx):
            Fired once at each slot boundary (TX→RX or RX→TX) — Ft4Tab uses
            this only to decide whether to key up, not for RX audio timing.
    """

    period_tick: Signal = Signal(bool, float)  # (is_tx, seconds_remaining)
    period_changed: Signal = Signal(bool)  # (is_tx)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tx_even: bool = True
        self._running: bool = False
        self._prev_slot_num: int = -1
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def start(self, tx_even: bool = True) -> None:
        """Start the scheduler.  tx_even=True: transmit in even 6-second slots."""
        self._tx_even = tx_even
        self._running = True
        self._prev_slot_num = -1
        self._timer.start()

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        self._timer.stop()

    def set_tx_even(self, tx_even: bool) -> None:
        """Change the TX/RX assignment while running."""
        self._tx_even = tx_even

    @staticmethod
    def current_slot_info() -> tuple[bool, float]:
        """Return (is_even_slot, position_in_slot) for the current moment."""
        now = corrected_time()
        slot_num = int(now / 6.0)
        is_even = slot_num % 2 == 0
        pos = now % 6.0
        return is_even, pos

    # ------------------------------------------------------------------ #

    def _tick(self) -> None:
        if not self._running:
            return
        now = corrected_time()
        slot_num = int(now / 6.0)
        is_even = slot_num % 2 == 0
        is_tx = is_even == self._tx_even
        pos = now % 6.0
        seconds_remaining = 6.0 - pos

        if self._prev_slot_num != slot_num and self._prev_slot_num >= 0:
            # `pos` is how far past the slot boundary we are at the moment
            # this transition was actually detected — should stay well
            # under 0.1s (the QTimer's own resolution) if nothing is
            # stalling the Qt event loop. A large value, or slots_missed > 0
            # (an entire 6s slot skipped over between two consecutive
            # ticks), reveals main-thread contention. This no longer affects
            # RX audio timing (see module docstring) but is kept as a
            # diagnostic to compare against Ft4RxCaptureWorker's own
            # boundary_lag in the same log (2026-07-10).
            slots_missed = slot_num - self._prev_slot_num - 1
            get_ft4_decode_logger().info(
                "scheduler boundary_lag=%.3fs slots_missed=%d slot=%d",
                pos,
                slots_missed,
                slot_num,
            )
            self.period_changed.emit(is_tx)

        self._prev_slot_num = slot_num
        self.period_tick.emit(is_tx, seconds_remaining)
