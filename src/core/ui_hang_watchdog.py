"""Temporary UI-freeze diagnostic: dump all thread stacks when the UI tick stalls.

Added 2026-09-24 while investigating a report of the app becoming unresponsive
after a manual transponder/frequency change during Autotrack + IQ recording
(the freeze was force-quit, so no evidence survived). The dump goes to
ui_hang.log next to fbsat59.log.

2026-09-25: replaced faulthandler.dump_traceback_later with a Python watchdog
thread. faulthandler dumps stacks without holding the GIL, and on 2026-09-25 a
SIGSEGV inside that dump (during a 5 s stall while IQ recording) killed the
whole process and lost the recording. sys._current_frames() runs with the GIL
held, so it cannot race with running threads. Trade-off: if a C extension
holds the GIL through the stall, this thread cannot run and no dump is taken.

Remove once the freeze is diagnosed.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import IO

logger = logging.getLogger(__name__)

_STALL_SECONDS = 5.0
_POLL_SECONDS = 1.0
_file: IO[str] | None = None
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_kick = 0.0
_dumped = False


def _dump(stalled_for: float) -> None:
    """Write every thread's Python stack to the dump file."""
    if _file is None:
        return
    lines = [f"Timeout ({stalled_for:.1f}s) at {datetime.now().isoformat(timespec='seconds')}!\n"]
    names = {t.ident: t.name for t in threading.enumerate()}
    for ident, frame in sys._current_frames().items():
        lines.append(f"Thread {ident:#x} ({names.get(ident, '?')}) (most recent call first):\n")
        for entry in reversed(traceback.extract_stack(frame)):
            lines.append(f'  File "{entry.filename}", line {entry.lineno} in {entry.name}\n')
        lines.append("\n")
    _file.write("".join(lines))
    _file.flush()


def _watch() -> None:
    """Dump once per stall when the UI thread has not kicked for a while."""
    global _dumped
    while not _stop_event.wait(_POLL_SECONDS):
        try:
            stalled = time.monotonic() - _last_kick
            if stalled >= _STALL_SECONDS and not _dumped:
                _dumped = True
                _dump(stalled)
        except Exception:
            logger.exception("UI hang watchdog: dump failed")


def start() -> None:
    """Open the dump file and start the watchdog thread (idempotent)."""
    global _file, _thread
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
    _stop_event.clear()
    _thread = threading.Thread(target=_watch, name="UIHangWatchdog", daemon=True)
    _thread.start()


def kick() -> None:
    """Report that the UI thread is alive; call from the UI thread every tick."""
    global _last_kick, _dumped
    _last_kick = time.monotonic()
    _dumped = False


def stop() -> None:
    """Stop the watchdog thread and close the dump file."""
    global _file, _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
    if _file is not None:
        _file.close()
        _file = None
