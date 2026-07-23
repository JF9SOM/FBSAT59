"""Process-wide clock offset used to compensate FT4/Q65 timing for OS clock drift.

FT4/Q65 rely on ~6-60s UTC-aligned periods and tolerate only a fraction of
a second of absolute-time error before boundary detection breaks down (see
MainWindow._check_ntp_sync_background()). Correcting the OS clock itself
would require administrator privileges on Windows and is out of scope for
this app, so instead the startup NTP check measures the offset once via
core.ntp_check and stores it here; FT4/Q65 timing code reads "now" through
corrected_time()/corrected_utcnow() instead of time.time()/datetime.now(UTC)
so decoding stays aligned even while the OS clock remains off.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

_lock = threading.Lock()
_offset_s: float = 0.0


def set_clock_offset(offset_s: float) -> None:
    """Record the most recently measured NTP offset (seconds to add to time.time())."""
    global _offset_s
    with _lock:
        _offset_s = offset_s


def get_clock_offset() -> float:
    """Return the currently applied offset in seconds (0.0 before the first NTP check)."""
    with _lock:
        return _offset_s


def corrected_time() -> float:
    """time.time(), corrected by the last measured NTP offset."""
    return time.time() + get_clock_offset()


def corrected_utcnow() -> datetime:
    """datetime.now(UTC), corrected by the last measured NTP offset."""
    return datetime.fromtimestamp(corrected_time(), UTC)
