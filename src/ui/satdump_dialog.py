"""Help > SatDump… dialog.

Shows the current SatDump installation status and provides a link to the
official download page.  On Linux, also offers a one-click download of
SatDump's own self-contained "nightly" AppImage — SatDump's stable Debian
packages depend on the system's librtlsdr, and its SONAME can drift from
what the distro's librtlsdr package actually provides (confirmed on
Ubuntu: satdump's .deb wants librtlsdr.so.0, but librtlsdr2 ships
librtlsdr.so.2), leaving "Could not find a handler for source type:
rtlsdr!" at runtime. The nightly AppImage bundles its own librtlsdr and
sidesteps that mismatch entirely. macOS/Windows aren't affected — their
official .dmg/Portable .zip are already self-contained.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from comms.meteor.satdump import find_satdump, get_user_satdump_dir
from i18n import _
from ui.copyable_text import CommandRow

_DOWNLOAD_URL = "https://github.com/SatDump/SatDump/releases/latest"
_APPIMAGE_URL = "https://github.com/SatDump/SatDump/releases/download/nightly/SatDump.AppImage"

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


def _is_user_installed(path: Path) -> bool:
    """True if path resolves inside the user install directory."""
    try:
        path.relative_to(get_user_satdump_dir())
        return True
    except ValueError:
        return False


class _InstallWorker(QThread):
    """Downloads SatDump's own self-contained nightly AppImage (Linux only)."""

    progress = Signal(int)  # 0-100
    status = Signal(str)
    finished_ok = Signal(str)  # installed path
    finished_err = Signal(str)

    def run(self) -> None:
        self.status.emit(_("Downloading…"))
        dest_dir = get_user_satdump_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / "satdump"
        tmp_path = dest_path.with_name(dest_path.name + ".tmp")

        def _reporthook(block: int, block_size: int, total: int) -> None:
            if total > 0:
                self.progress.emit(int(block * block_size * 100 / total))

        try:
            urllib.request.urlretrieve(_APPIMAGE_URL, tmp_path, reporthook=_reporthook)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            self.finished_err.emit(str(exc))
            return

        try:
            tmp_path.chmod(0o755)
            tmp_path.replace(dest_path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            self.finished_err.emit(str(exc))
            return

        self.progress.emit(100)
        self.finished_ok.emit(str(dest_path))


class SatDumpDialog(QDialog):
    """Help > SatDump… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("SatDump"))
        self.setMinimumWidth(500)
        self._worker: _InstallWorker | None = None
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

        # Installation guidance (superseded on Linux by the self-contained
        # AppImage download below, which needs no manual steps)
        self._guide_box = QGroupBox(_("Installation"))
        gl = QVBoxLayout(self._guide_box)

        self._guide_text = QTextBrowser()
        self._guide_text.setOpenExternalLinks(False)
        self._guide_text.setFixedHeight(160)
        gl.addWidget(self._guide_text)

        self._cmd_row = CommandRow(allow_run=True)
        gl.addWidget(self._cmd_row)

        self._btn_open = QPushButton(_("Open Download Page"))
        self._btn_open.clicked.connect(self._on_open_download)
        gl.addWidget(self._btn_open)
        root.addWidget(self._guide_box)
        self._guide_box.setVisible(sys.platform != "linux")

        # --- Linux-only: download SatDump's own self-contained nightly AppImage ---
        self._download_box = QGroupBox(_("Install Self-Contained Build (Recommended for Linux)"))
        dl = QVBoxLayout(self._download_box)
        dl.addWidget(
            QLabel(
                _(
                    'Downloads SatDump\'s own self-contained "nightly" AppImage\n'
                    "(bundles its own librtlsdr etc., avoiding version conflicts\n"
                    "with your distro's packages). This is a development build\n"
                    "from the SatDump project, not a numbered release."
                )
            )
        )
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._lbl_dl_status = QLabel()
        self._lbl_dl_status.setVisible(False)
        dl.addWidget(self._progress)
        dl.addWidget(self._lbl_dl_status)
        btn_row = QHBoxLayout()
        self._btn_download = QPushButton(_("Download && Install"))
        self._btn_download.clicked.connect(self._on_download)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_download)
        dl.addLayout(btn_row)
        root.addWidget(self._download_box)
        self._download_box.setVisible(sys.platform == "linux")

        # --- Uninstall (only shown when a user-installed copy exists) ---
        uninstall_row = QHBoxLayout()
        self._btn_uninstall = QPushButton(_("Uninstall"))
        self._btn_uninstall.setStyleSheet("QPushButton{color:#cc3300;}")
        self._btn_uninstall.clicked.connect(self._on_uninstall)
        self._btn_uninstall.setVisible(False)
        uninstall_row.addStretch()
        uninstall_row.addWidget(self._btn_uninstall)
        root.addLayout(uninstall_row)

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
            self._btn_uninstall.setVisible(False)
        else:
            version = _get_satdump_version(path)
            self._lbl_status.setText(
                "<b style='color:#27ae60'>&#x2714; " + _("SatDump found") + "</b>"
            )
            self._lbl_path.setText(_("Path: ") + str(path))
            self._lbl_version.setText(_("Version: ") + version)
            self._btn_uninstall.setVisible(_is_user_installed(path))

        self._populate_guide()

    def _on_download(self) -> None:
        self._btn_download.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._lbl_dl_status.setVisible(True)
        self._lbl_dl_status.setText(_("Starting…"))

        self._worker = _InstallWorker(self)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.status.connect(self._lbl_dl_status.setText)
        self._worker.finished_ok.connect(self._on_install_ok)
        self._worker.finished_err.connect(self._on_install_err)
        self._worker.start()

    def _on_install_ok(self, path: str) -> None:
        self._progress.setValue(100)
        self._progress.setVisible(False)
        self._lbl_dl_status.setText(_("Installed: ") + path)
        self._btn_download.setEnabled(True)
        self._refresh_status()

    def _on_install_err(self, msg: str) -> None:
        self._lbl_dl_status.setText(_("Error: ") + msg)
        self._btn_download.setEnabled(True)
        self._progress.setVisible(False)

    def _on_uninstall(self) -> None:
        reply = QMessageBox.question(
            self,
            _("Uninstall SatDump"),
            _(
                "Remove the user-installed SatDump from your data directory?\n\n"
                "If the METEOR/HRPT tab is currently in use, close it first."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.rmtree(get_user_satdump_dir())
        except Exception as exc:
            QMessageBox.warning(self, _("Uninstall Failed"), str(exc))
            return
        self._refresh_status()

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
