"""Temporary SDR pipeline timing diagnostic log.

The shared application log (fbsat59.log, see src/main.py's
_setup_logging()) has no visibility into SDRPipeline's per-block read/
demod/audio-write timing. Added 2026-07-25 while investigating a GitHub
Issue #12 report of speaker audio "motorboating" (buffer-underrun-style
stutter) when a second SDR consumer (Telemetry's AX.25 reception) runs
concurrently with SdrControlWidget's own audio playback — to determine
whether SDRPipeline.run()'s single-threaded read/demod/audio-write loop
is actually falling behind real-time (partial reads, growing per-block
lag) under that load, versus a different cause.

Remove once diagnosed — see CLAUDE.md "SDRPipeline motorboating" section.
"""

from __future__ import annotations

import logging
import os

_logger: logging.Logger | None = None


def get_sdr_diag_logger() -> logging.Logger:
    """Return the SDR pipeline diagnostic logger, creating it on first call.

    propagate=False keeps this out of the shared fbsat59.log / stderr —
    it is meant to be read on its own (sdr_pipeline_diag.log, same
    directory as fbsat59.log), not mixed in with everything else.
    """
    global _logger
    if _logger is not None:
        return _logger

    from platformdirs import user_log_dir

    log_dir = user_log_dir("fbsat59", "fbsat59")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "sdr_pipeline_diag.log")

    logger = logging.getLogger("fbsat59.sdr_pipeline_diag")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger
