"""Restart the running FBSAT59 process (GitHub Issue #27 follow-up, 2026-08-23).

Kept as a standalone module (rather than living in main.py) because
importing main.py runs its module-level platform-specific setup (sys.path
surgery, environment variables for frozen builds, etc.) as a side effect --
callers such as ui/autotrack_record_dialog.py must be able to trigger a
restart without re-running any of that.
"""

from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtWidgets import QApplication

RESTART_ENV_VAR = "FBSAT59_RESTART"


def restart_application() -> None:
    """Launch a fresh FBSAT59 process, then quit the current one.

    Used by the Autotrack "restart required" prompt: rig/SDR device handles
    have been observed to linger across an Enable Autotrack toggle in ways
    a fresh process reliably clears, so restarting is offered as a
    one-click action instead of asking the user to quit and relaunch by
    hand.

    The new process is started *before* this one calls QApplication.quit(),
    so it necessarily starts while the old process (and its QLockFile) may
    still be alive and shutting down (closing the DB connection, stopping
    any Communications tab subprocesses, etc.) -- that's why
    main.py's _acquire_single_instance_lock() retries for a few seconds
    when RESTART_ENV_VAR=1 is set, rather than this function waiting on the
    old process's PID itself (which would require a platform-specific
    process wait and a separate launcher process, and isn't needed since
    the retry handles it from the new process's side instead).
    """
    if getattr(sys, "frozen", False):
        # PyInstaller build: sys.executable is the frozen binary itself;
        # sys.argv[0] duplicates it and must be dropped.
        args = [sys.executable, *sys.argv[1:]]
    else:
        args = [sys.executable, *sys.argv]

    env = os.environ.copy()
    env[RESTART_ENV_VAR] = "1"
    subprocess.Popen(args, env=env)

    app = QApplication.instance()
    if app is not None:
        app.quit()
