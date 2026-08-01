"""Help > Direwolf… dialog.

Shows the current Direwolf status (path, version) and provides
platform-specific installation guidance or a bundle-update option.

Detection priority (mirrors find_direwolf()):
  1. User-installed   ~/.local/share/fbsat59/direwolf/
  2. System PATH      which direwolf
  3. Bundled          _MEIPASS/direwolf (PyInstaller)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.direwolf import _user_direwolf_dir, find_direwolf
from i18n import _
from ui.copyable_text import CommandRow

# ---------------------------------------------------------------------------
# Version detection helper
# ---------------------------------------------------------------------------


def _get_direwolf_version(path: Path) -> str:
    """Run ``direwolf -h`` and extract the version string."""
    try:
        result = subprocess.run(
            [str(path), "-h"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout + result.stderr
        for line in output.splitlines():
            if "version" in line.lower() or "direwolf" in line.lower():
                return line.strip()[:80]
        return _("Unknown version")
    except Exception:
        return _("Unknown version")


def _detect_source(path: Path) -> str:
    """Return a human-readable source label for a resolved path."""
    user_dir = _user_direwolf_dir()
    try:
        path.relative_to(user_dir)
        return _("User-installed")
    except ValueError:
        pass
    if getattr(sys, "frozen", False):
        try:
            import sys as _sys

            path.relative_to(Path(_sys._MEIPASS))  # type: ignore[attr-defined]
            return _("Bundled")
        except (ValueError, AttributeError):
            pass
    return _("System PATH")


# ---------------------------------------------------------------------------
# Background worker: download & install bundled Direwolf
# ---------------------------------------------------------------------------


class _InstallWorker(QThread):
    """Downloads the latest bundled Direwolf from GitHub Releases."""

    progress = Signal(int)  # 0-100
    status = Signal(str)
    finished_ok = Signal(str)  # installed path
    finished_err = Signal(str)

    _REPO = "JF9SOM/fbsat59"
    _API = f"https://api.github.com/repos/{_REPO}/releases/latest"

    def run(self) -> None:
        import json
        import platform
        import tarfile
        import urllib.request
        import zipfile

        self.status.emit(_("Checking latest release…"))
        try:
            req = urllib.request.Request(
                self._API, headers={"Accept": "application/vnd.github+json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return

        assets = data.get("assets", [])
        plat = sys.platform
        machine = platform.machine().lower()

        if plat == "linux":
            suffix = f"direwolf-linux-{machine}.tar.gz"
        elif plat == "win32":
            suffix = "direwolf-windows-x86_64.zip"
        elif plat == "darwin":
            suffix = f"direwolf-macos-{machine}.tar.gz"
        else:
            self.finished_err.emit(_("Unsupported platform"))
            return

        url = next(
            (a["browser_download_url"] for a in assets if a["name"].endswith(suffix)),
            None,
        )
        if not url:
            self.finished_err.emit(
                _(
                    "No bundled Direwolf package found in the latest release.\n"
                    "Please install Direwolf manually."
                )
            )
            return

        self.status.emit(_("Downloading…"))
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)

            def _reporthook(block: int, block_size: int, total: int) -> None:
                if total > 0:
                    self.progress.emit(int(block * block_size * 100 / total))

            urllib.request.urlretrieve(url, tmp_path, reporthook=_reporthook)
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return

        self.progress.emit(95)
        self.status.emit(_("Installing…"))

        dest_dir = _user_direwolf_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            if suffix.endswith(".tar.gz"):
                with tarfile.open(tmp_path) as tar:
                    tar.extractall(dest_dir)
            else:
                with zipfile.ZipFile(tmp_path) as zf:
                    zf.extractall(dest_dir)
            tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return

        self.progress.emit(100)
        exe = "direwolf.exe" if sys.platform == "win32" else "direwolf"
        installed = dest_dir / exe
        self.finished_ok.emit(str(installed))


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------


class DirewolfDialog(QDialog):
    """Help > Direwolf… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Direwolf"))
        self.setMinimumWidth(520)
        self._worker: _InstallWorker | None = None
        self._setup_ui()
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- Status group ---
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

        # --- Install guidance ---
        self._guide_box = QGroupBox(_("Installation"))
        gl = QVBoxLayout(self._guide_box)
        self._guide_text = QTextBrowser()
        self._guide_text.setOpenExternalLinks(True)
        self._guide_text.setFixedHeight(140)
        gl.addWidget(self._guide_text)

        self._cmd_row = CommandRow(allow_run=True)
        gl.addWidget(self._cmd_row)
        root.addWidget(self._guide_box)

        # --- Bundle update ---
        self._update_box = QGroupBox(_("Update Bundled Direwolf"))
        ul = QVBoxLayout(self._update_box)
        ul.addWidget(
            QLabel(
                _(
                    "Download the latest bundled Direwolf from GitHub Releases\n"
                    "and install it to your user data directory."
                )
            )
        )
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._lbl_dl_status = QLabel()
        self._lbl_dl_status.setVisible(False)
        ul.addWidget(self._progress)
        ul.addWidget(self._lbl_dl_status)
        btn_dl_row = QHBoxLayout()
        self._btn_download = QPushButton(_("Download && Install"))
        self._btn_download.clicked.connect(self._on_download)
        btn_dl_row.addStretch()
        btn_dl_row.addWidget(self._btn_download)
        ul.addLayout(btn_dl_row)
        root.addWidget(self._update_box)

        # --- Buttons ---
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------ #
    # Status refresh
    # ------------------------------------------------------------------ #

    def _refresh_status(self) -> None:
        path = find_direwolf()
        if path is None:
            self._lbl_status.setText(
                "<b style='color:#e74c3c'>&#x2718; " + _("Direwolf not found") + "</b>"
            )
            self._lbl_path.setText("")
            self._lbl_version.setText("")
            self._guide_box.setVisible(True)
            self._update_box.setVisible(True)
        else:
            source = _detect_source(path)
            version = _get_direwolf_version(path)
            self._lbl_status.setText(
                "<b style='color:#27ae60'>&#x2714; " + _("Direwolf found") + f" ({source})</b>"
            )
            self._lbl_path.setText(_("Path: ") + str(path))
            self._lbl_version.setText(_("Version: ") + version)
            self._guide_box.setVisible(False)
            self._update_box.setVisible(True)

        self._populate_guide()

    def _populate_guide(self) -> None:
        """Fill the installation guidance for the current platform."""
        download_note = _(
            "Or use the <b>Download &amp; Install</b> button below to get the bundled version."
        )
        if sys.platform == "linux":
            html = (
                "<b>Ubuntu / Debian</b><br>"
                "<code>sudo apt install direwolf</code><br><br>"
                "<b>Fedora / RHEL</b><br>"
                "<code>sudo dnf install direwolf</code><br><br>" + download_note
            )
            cmd = "sudo apt install direwolf"
        elif sys.platform == "win32":
            windows_note = _("Download the Windows installer from the Direwolf project:")
            html = (
                f"{windows_note}<br>"
                "<a href='https://github.com/wb2osz/direwolf/releases'>"
                "github.com/wb2osz/direwolf/releases</a><br><br>" + download_note
            )
            cmd = ""
        elif sys.platform == "darwin":
            html = (
                "<b>macOS (Homebrew)</b><br>"
                "<code>brew install direwolf</code><br><br>" + download_note
            )
            cmd = "brew install direwolf"
        else:
            html = _("Please install Direwolf from your distribution's package manager.")
            cmd = ""

        self._guide_text.setHtml(html)
        self._cmd_row.setText(f"<code>{cmd}</code>" if cmd else "")

    # ------------------------------------------------------------------ #
    # Slots
    # ------------------------------------------------------------------ #

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
