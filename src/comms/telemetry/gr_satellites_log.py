"""Dedicated log of gr_satellites' own console output (stdout + stderr).

Mirrors comms.aprs.direwolf_log's rationale but for the gr-satellites
backend: ``GrSatellitesBackend`` already reads the subprocess's stdout to
parse telemetry blocks for the UI (see ``_read_stdout()``), but its stderr
was previously ``subprocess.DEVNULL`` — any warning, traceback, or "no such
satellite" error from gr_satellites itself was silently discarded, leaving
no record of *why* a reception attempt produced nothing. Added 2026-09-17
alongside direwolf.log, so the Telemetry tab's "Log" button has something to
show for the gr-satellites mode too.

propagate=False and a dedicated file (gr_satellites.log, same directory as
fbsat59.log) keep this out of the shared application log, matching the
existing sdr.diag_log / comms.ft4.decode_log / comms.aprs.direwolf_log
convention.
"""

from __future__ import annotations

import logging
import os

_logger: logging.Logger | None = None


def get_gr_satellites_logger() -> logging.Logger:
    """Return the gr_satellites console-output logger, creating it on first call."""
    global _logger
    if _logger is not None:
        return _logger

    from platformdirs import user_log_dir

    log_dir = user_log_dir("fbsat59", "fbsat59")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "gr_satellites.log")

    logger = logging.getLogger("fbsat59.gr_satellites")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger


def gr_satellites_log_path() -> str:
    """Return the path to gr_satellites.log without opening/creating the logger."""
    from platformdirs import user_log_dir

    return os.path.join(user_log_dir("fbsat59", "fbsat59"), "gr_satellites.log")


def reset_gr_satellites_log() -> None:
    """Truncate gr_satellites.log so a new reception session starts clean.

    See comms.aprs.direwolf_log.reset_direwolf_log() -- same rationale, same
    approach (truncate the already-open stream in place rather than
    closing/reopening the handler).
    """
    logger = get_gr_satellites_logger()
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.acquire()
            try:
                if handler.stream is not None:
                    handler.stream.seek(0)
                    handler.stream.truncate(0)
            finally:
                handler.release()
