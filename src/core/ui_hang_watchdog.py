"""Temporary UI-freeze diagnostic: dump all thread stacks when the UI tick stalls.

Added 2026-09-24 while investigating a report of the app becoming unresponsive
after a manual transponder/frequency change during Autotrack + IQ recording
(the freeze was force-quit, so no evidence survived). faulthandler's timer runs
on its own C thread without the GIL, so it still fires when the UI thread (or
the whole interpreter) is stuck. The dump goes to ui_hang.log next to
fbsat59.log.

Remove once the freeze is diagnosed.
"""

from __future__ import annotations

import faulthandler
import logging
import os
from typing import IO

logger = logging.getLogger(__name__)

_STALL_SECONDS = 5.0
_file: IO[str] | None = None


def start() -> None:
    """Open the dump file and arm the watchdog (idempotent)."""
    global _file
    if _file is not None:
        return
    try:
        from platformdirs import user_log_dir

        log_dir = user_log_dir("fbsat59", "fbsat59")
        os.makedirs(log_dir, exist_ok=True)
        _file = open(os.path.join(log_dir, "ui_hang.log"), "a", buffering=1, encoding="utf-8")  # noqa: SIM115
    except Exception:
        logger.exception("UI hang watchdog: could not open dump file")
        return
    kick()


def kick() -> None:
    """Re-arm the timer; call from the UI thread every tick."""
    if _file is None:
        return
    faulthandler.dump_traceback_later(_STALL_SECONDS, repeat=False, file=_file)


def stop() -> None:
    """Disarm the watchdog and close the dump file."""
    global _file
    faulthandler.cancel_dump_traceback_later()
    if _file is not None:
        _file.close()
        _file = None
