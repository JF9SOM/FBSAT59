"""Dedicated log of Direwolf's own console output (SDR receive-only path).

Direwolf writes its startup banner, warnings, and — critically — a line per
decoded AX.25 frame (with its own audio-level quality assessment) to its
process stdout. When AudioBridge feeds Direwolf from an SDR pipeline instead
of a soundcard (see direwolf.py's receive-only branch, ``out_device is
None``), nothing ever reads that stdout, so this text was previously
discarded unread — there was no way to tell, after an SDR reception attempt,
whether Direwolf ever produced a DECODED line at all versus never even
achieving bit sync. Added 2026-09-17 after an OrigamiSat-2 AX.25 telemetry
reception attempt (20-30 degrees elevation, clean carrier on the waterfall)
produced zero frames, and neither fbsat59.log nor sdr_pipeline_diag.log had
any record either way.

propagate=False and a dedicated file (direwolf.log, same directory as
fbsat59.log) keep this out of the shared application log, matching the
existing sdr.diag_log / comms.ft4.decode_log convention.
"""

from __future__ import annotations

import logging
import os

_logger: logging.Logger | None = None


def get_direwolf_logger() -> logging.Logger:
    """Return the Direwolf console-output logger, creating it on first call."""
    global _logger
    if _logger is not None:
        return _logger

    from platformdirs import user_log_dir

    log_dir = user_log_dir("fbsat59", "fbsat59")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "direwolf.log")

    logger = logging.getLogger("fbsat59.direwolf")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger


def direwolf_log_path() -> str:
    """Return the path to direwolf.log without opening/creating the logger."""
    from platformdirs import user_log_dir

    return os.path.join(user_log_dir("fbsat59", "fbsat59"), "direwolf.log")


def reset_direwolf_log() -> None:
    """Truncate direwolf.log so a new SDR reception session starts clean.

    The underlying FileHandler is opened once (mode "a") for the life of
    the process and otherwise never closed, so without this, old sessions'
    output would keep accumulating in the same file forever -- confusing
    when checking "did *this* attempt decode anything" (2026-09-17,
    reported after the Log dialog's Refresh button appeared to do nothing
    because there was nothing new to show, but old content from an earlier
    attempt was still sitting there). Truncates the already-open stream in
    place rather than closing/reopening the handler, so no reader ever sees
    a moment where the file doesn't exist.
    """
    logger = get_direwolf_logger()
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.acquire()
            try:
                if handler.stream is not None:
                    handler.stream.seek(0)
                    handler.stream.truncate(0)
            finally:
                handler.release()
