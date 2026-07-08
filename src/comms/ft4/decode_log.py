"""Dedicated FT4 decode-timing log.

The shared application log (fbsat59.log, see src/main.py's _setup_logging())
is too noisy with Qt/rig/tracking messages to usefully see per-period FT4
decode timing at a glance. This writes a separate ft4_decode.log, in the
same directory as fbsat59.log, with one line per RX period: how long
decode_audio() actually took and how many messages it found, or that the
period was skipped because the previous decode hadn't finished yet.

Added 2026-07-10 while diagnosing whether _RxDecodeWorker's background
thread (see ui/ft4_tab.py) is enough on its own, or whether real-world
decode times under full app load (satellite tracking, map rendering, etc.
all competing for the same low-power CPU) still overrun 6s often enough to
need a heavier fix (e.g. a separate decode subprocess).
"""

from __future__ import annotations

import logging
import os

_logger: logging.Logger | None = None


def get_ft4_decode_logger() -> logging.Logger:
    """Return the FT4 decode-timing logger, creating it on first call.

    propagate=False keeps this out of the shared fbsat59.log / stderr —
    it is meant to be read on its own, not mixed in with everything else.
    """
    global _logger
    if _logger is not None:
        return _logger

    from platformdirs import user_log_dir

    log_dir = user_log_dir("FBSAT59", "FBSAT59")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "ft4_decode.log")

    logger = logging.getLogger("fbsat59.ft4_decode")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger
