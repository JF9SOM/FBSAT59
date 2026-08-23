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

    The new process is deliberately detached from this one's session/
    process group and stdio: on the dev launcher (Desktop .app -> Ghostty ->
    run.command -> `python src/main.py`), plain Popen() leaves the child
    in the same terminal session, so once this process exits and the shell
    script/terminal session tears down, the child gets SIGHUP'd along with
    it -- the app was observed to just quit instead of relaunching. Detaching
    (start_new_session on POSIX, a new process group on Windows) and
    redirecting stdio away from the parent's (about-to-vanish) handles
    avoids that.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller build: sys.executable is the frozen binary itself;
        # sys.argv[0] duplicates it and must be dropped.
        args = [sys.executable, *sys.argv[1:]]
    else:
        args = [sys.executable, *sys.argv]

    env = os.environ.copy()
    env[RESTART_ENV_VAR] = "1"

    popen_kwargs: dict[str, object] = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        popen_kwargs["start_new_session"] = True

    subprocess.Popen(args, **popen_kwargs)  # type: ignore[call-overload]

    app = QApplication.instance()
    if app is not None:
        app.quit()
