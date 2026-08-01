"""Help > SatDump… dialog.

Shows the current SatDump installation status and provides a link to the
official download page.  No automatic bundling — users install SatDump
themselves.
"""

from __future__ import annotations

import re
import subprocess
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from comms.meteor.satdump import find_satdump
from i18n import _
from ui.copyable_text import CommandRow

_DOWNLOAD_URL = "https://github.com/SatDump/SatDump/releases/latest"

# Matches ANSI escape/color sequences (e.g. "\x1b[31m") some SatDump builds
# emit even when stdout isn't a tty.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _get_satdump_version(path: object) -> str:
    """Run ``satdump --version`` and return the version string.

    Some SatDump builds don't recognize ``--version`` and instead print a
    (possibly ANSI-colored) usage/help message; that's not a real version
    string, so it's reported as unknown rather than shown verbatim.
    """
    from pathlib import Path

    if not isinstance(path, Path):
        return _("Unknown version")
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = _ANSI_ESCAPE_RE.sub("", result.stdout + result.stderr).strip()
        if "usage" in output.lower():
            # --version wasn't recognized by this build; it printed a
            # usage/help message instead of an actual version string.
            return _("Unknown version")
        for line in output.splitlines():
            line = line.strip()
            if line:
                return line[:80]
        return _("Unknown version")
    except Exception:
        return _("Unknown version")


class SatDumpDialog(QDialog):
    """Help > SatDump… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("SatDump"))
        self.setMinimumWidth(500)
        self._setup_ui()
        self._refresh_status()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # Status group
        status_box = QGroupBox(_("Current Status"))
        sl = QVBoxLayout(status_box)
        self._lbl_status = QLabel(_("Checking…"))
        self._lbl_status.setWordWrap(True)
        self._lbl_path = QLabel()
        self._lbl_path.setWordWrap(True)
        self._lbl_version = QLabel()
        self._lbl_version.setWordWrap(True)
        sl.addWidget(self._lbl_status)
        sl.addWidget(self._lbl_path)
        sl.addWidget(self._lbl_version)
        root.addWidget(status_box)

        # Installation guidance
        guide_box = QGroupBox(_("Installation"))
        gl = QVBoxLayout(guide_box)

        self._guide_text = QTextBrowser()
        self._guide_text.setOpenExternalLinks(False)
        self._guide_text.setFixedHeight(160)
        gl.addWidget(self._guide_text)

        self._cmd_row = CommandRow(allow_run=True)
        gl.addWidget(self._cmd_row)

        self._btn_open = QPushButton(_("Open Download Page"))
        self._btn_open.clicked.connect(self._on_open_download)
        gl.addWidget(self._btn_open)
        root.addWidget(guide_box)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _refresh_status(self) -> None:
        path = find_satdump()
        if path is None:
            self._lbl_status.setText(
                "<b style='color:#e74c3c'>&#x2718; " + _("SatDump not found") + "</b>"
            )
            self._lbl_path.setText("")
            self._lbl_version.setText("")
        else:
            version = _get_satdump_version(path)
            self._lbl_status.setText(
                "<b style='color:#27ae60'>&#x2714; " + _("SatDump found") + "</b>"
            )
            self._lbl_path.setText(_("Path: ") + str(path))
            self._lbl_version.setText(_("Version: ") + version)

        self._populate_guide()

    def _populate_guide(self) -> None:
        cmd = ""
        if sys.platform == "linux":
            cmd = "sudo apt install satdump"
            html = (
                "<b>Ubuntu / Debian</b><br>"
                f"<code>{cmd}</code>"
                "&nbsp;&nbsp;(if available in your repo)<br><br>"
                "<b>AppImage (recommended)</b><br>"
                "Download the <code>.AppImage</code> from the releases page, "
                "make it executable, and place it anywhere on your PATH "
                "(e.g. <code>~/bin/satdump</code>).<br><br>"
                f"<a href='{_DOWNLOAD_URL}'>{_DOWNLOAD_URL}</a>"
            )
        elif sys.platform == "win32":
            html = (
                "Download the Windows installer (<code>.exe</code>) from:<br>"
                f"<a href='{_DOWNLOAD_URL}'>{_DOWNLOAD_URL}</a><br><br>"
                "After installation, make sure <code>satdump.exe</code> "
                "is on your system PATH, or place it in:<br>"
                "<code>%APPDATA%\\fbsat59\\satdump\\satdump.exe</code>"
            )
        elif sys.platform == "darwin":
            cmd = "brew install satdump"
            html = (
                "<b>macOS (Homebrew)</b><br>"
                f"<code>{cmd}</code><br><br>"
                "Or download the <code>.dmg</code> from:<br>"
                f"<a href='{_DOWNLOAD_URL}'>{_DOWNLOAD_URL}</a>"
            )
        else:
            html = (
                "Please install SatDump from your distribution's package manager "
                "or download it from:<br>"
                f"<a href='{_DOWNLOAD_URL}'>{_DOWNLOAD_URL}</a>"
            )

        self._guide_text.setHtml(html)
        self._cmd_row.setText(f"<code>{cmd}</code>" if cmd else "")

    def _on_open_download(self) -> None:
        QDesktopServices.openUrl(QUrl(_DOWNLOAD_URL))
