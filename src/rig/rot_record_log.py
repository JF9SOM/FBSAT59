"""Rotator catch-up slew-speed measurement log.

The shared application log (fbsat59.log, see src/main.py's
_setup_logging()) would grow quickly if every ~2s catch-up position poll
were written there, and mixing it in with everything else makes it hard to
review. This dedicated log (rot-record.log, same directory as fbsat59.log)
exists so HamlibRotatorController's catch-up episodes and measured slew
speed can be recorded on their own for later analysis.
"""

from __future__ import annotations

import logging
import os

_logger: logging.Logger | None = None


def get_rotor_record_logger() -> logging.Logger:
    """Return the rotator catch-up record logger, creating it on first call.

    propagate=False keeps this out of the shared fbsat59.log / stderr —
    it is meant to be read on its own (rot-record.log, same directory as
    fbsat59.log), not mixed in with everything else.
    """
    global _logger
    if _logger is not None:
        return _logger

    from platformdirs import user_log_dir

    log_dir = user_log_dir("fbsat59", "fbsat59")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "rot-record.log")

    logger = logging.getLogger("fbsat59.rot_record")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger
