"""Cross-platform helper for opening a terminal and running a shell command.

Used by Help dialogs to let users run install commands (Homebrew bootstrap,
apt-get, manual build recipes) without having to copy-paste them by hand.
The command runs interactively in a real, visible terminal window, so the
user can watch the output, answer a sudo password prompt, or interrupt it
with Ctrl+C, rather than having it execute silently in a hidden subprocess.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

# Linux terminal emulators to probe, in order of preference. Each entry is
# (executable, argv template) where the command string is appended as the
# final argument. All of these accept ``bash -c "<command>"`` as separate
# argv elements (not shell-parsed), which is the most portable invocation.
_LINUX_TERMINALS: tuple[tuple[str, list[str]], ...] = (
    ("gnome-terminal", ["--", "bash", "-c"]),
    ("konsole", ["-e", "bash", "-c"]),
    ("xterm", ["-e", "bash", "-c"]),
)


def open_terminal_and_run(command: str) -> tuple[bool, str]:
    """Open the OS terminal and run ``command`` interactively.

    Returns ``(success, error_message)``; ``error_message`` is empty on
    success. The window is left open after the command finishes (or fails)
    so the user can read the output.
    """
    if sys.platform == "darwin":
        return _run_macos(command)
    if sys.platform == "win32":
        return _run_windows(command)
    return _run_linux(command)


def _run_macos(command: str) -> tuple[bool, str]:
    wrapped = f'{command}; echo; echo "--- press Enter to close ---"; read'
    # AppleScript double-quoted string literal: escape backslashes and quotes.
    escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Terminal"\nactivate\ndo script "{escaped}"\nend tell'
    try:
        subprocess.Popen(["osascript", "-e", script])
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _run_windows(command: str) -> tuple[bool, str]:
    try:
        # `cmd /k` runs the command and leaves the window open afterwards.
        subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", command])
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _run_linux(command: str) -> tuple[bool, str]:
    wrapped = f'{command}; echo; echo "--- press Enter to close ---"; read'
    for exe, argv_prefix in _LINUX_TERMINALS:
        path = shutil.which(exe)
        if not path:
            continue
        try:
            subprocess.Popen([path, *argv_prefix, wrapped])
            return True, ""
        except OSError:
            continue
    return (
        False,
        "No supported terminal emulator found "
        "(tried gnome-terminal, konsole, xterm). Use the Copy button instead.",
    )
