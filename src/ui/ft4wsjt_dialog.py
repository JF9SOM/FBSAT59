"""Help > FT4 Enhanced Decoder Installation… dialog.

Shows the current libft4wsjt status (path) and provides a one-click
download-and-install from GitHub Releases.

libft4wsjt exposes WSJT-X's own FT4 decode engine (3-pass signal
subtraction + BP/OSD hybrid decode), giving substantially better
recovery of weak/overlapping stations than the lightweight
kgoba/ft8_lib single-pass decoder that FT4 TX and the fallback RX path
use (see comms/ft4/codec.py and comms/ft4/wsjt_decoder.py).

Detection priority (mirrors _find_libft4wsjt()):
  1. User-installed   ~/.local/share/fbsat59/ft4wsjt/
  2. Bundled          _MEIPASS/libft4wsjt.so (PyInstaller)
  3. Development      <repo>/ft4wsjt-bundle/
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal
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

from comms.ft4.wsjt_decoder import (
    _find_libft4wsjt,
    free_libft4wsjt,
    get_user_ft4wsjt_dir,
    reload_libft4wsjt,
)
from i18n import _
from ui.copyable_text import make_copy_button

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_user_installed(lib_path: str) -> bool:
    """True if lib_path resolves inside the user install directory."""
    try:
        Path(lib_path).relative_to(get_user_ft4wsjt_dir())
        return True
    except ValueError:
        return False


def _detect_source(lib_path: str) -> str:
    """Return a human-readable source label for the resolved library path."""
    if _is_user_installed(lib_path):
        return _("User-installed")
    if getattr(sys, "frozen", False):
        p = Path(lib_path)
        try:
            p.relative_to(Path(getattr(sys, "_MEIPASS", "")))
            return _("Bundled")
        except ValueError:
            pass
    return _("Development")


# ---------------------------------------------------------------------------
# Background worker: download & install ft4wsjt bundle
# ---------------------------------------------------------------------------

_RELEASE_TAG = "ft4wsjt-bundle"


class _InstallWorker(QThread):
    """Downloads the latest libft4wsjt bundle from GitHub Releases."""

    progress = Signal(int)  # 0-100
    status = Signal(str)
    finished_ok = Signal(str)  # installed path
    finished_err = Signal(str)

    _REPO = "JF9SOM/fbsat59"

    def run(self) -> None:
        import json
        import platform
        import tarfile
        import tempfile
        import urllib.request
        import zipfile

        self.status.emit(_("Checking latest release…"))
        api_url = f"https://api.github.com/repos/{self._REPO}/releases/tags/{_RELEASE_TAG}"
        try:
            req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return

        assets = data.get("assets", [])
        plat = sys.platform
        machine = platform.machine().lower()

        if plat == "linux":
            suffix = f"ft4wsjt-linux-{machine}.tar.gz"
            lib_name = "libft4wsjt.so"
        elif plat == "win32":
            suffix = "ft4wsjt-windows-x86_64.zip"
            lib_name = "ft4wsjt.dll"
        elif plat == "darwin":
            suffix = f"ft4wsjt-macos-{machine}.tar.gz"
            lib_name = "libft4wsjt.dylib"
        else:
            self.finished_err.emit(_("Unsupported platform"))
            return

        url = next(
            (a["browser_download_url"] for a in assets if a["name"] == suffix),
            None,
        )
        if not url:
            self.finished_err.emit(
                _(
                    "No libft4wsjt package found for this platform in the release.\n"
                    "Please build it manually — see the instructions above."
                )
            )
            return

        self.status.emit(_("Downloading…"))
        try:
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

        dest_dir = get_user_ft4wsjt_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)

        # A prior FT4 session in this run may already have loaded
        # ft4wsjt.dll (kept open for the process's whole lifetime -- unlike
        # ft8_lib, libft4wsjt was never designed to be freed/reloaded), which
        # locks the file on Windows and makes overwriting it fail with
        # PermissionError. Release it first (GitHub Issue #16).
        free_libft4wsjt()

        try:
            if suffix.endswith(".tar.gz"):
                with tarfile.open(tmp_path) as tar:
                    # Strip the top-level "ft4wsjt-flat/" directory if present
                    members = tar.getmembers()
                    prefix = members[0].name.split("/")[0] + "/" if members else ""
                    for m in members:
                        if m.name.startswith(prefix):
                            m.name = m.name[len(prefix) :]
                        if m.name:
                            tar.extract(m, dest_dir)
            else:
                with zipfile.ZipFile(tmp_path) as zf:
                    zf.extractall(dest_dir)
            tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return

        self.progress.emit(100)
        # Pick up the newly installed library immediately -- no app restart
        # needed for FT4 RX to start using it on its next decode call.
        reload_libft4wsjt()
        self.finished_ok.emit(str(dest_dir / lib_name))


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------


class Ft4WsjtDialog(QDialog):
    """Help > FT4 Enhanced Decoder Installation… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("FT4 Enhanced Decoder Installation"))
        self.setMinimumWidth(560)
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
        sl.addWidget(self._lbl_status)
        sl.addWidget(self._lbl_path)
        root.addWidget(status_box)

        # --- About ---
        info_box = QGroupBox(_("About the Enhanced FT4 Decoder"))
        il = QVBoxLayout(info_box)
        info_lbl = QLabel(
            _(
                "libft4wsjt is built from <a href='https://github.com/wsjtx/wsjtx'>"
                "WSJT-X</a>'s own FT4 decode engine (GPL-3.0) via a small C bridge — "
                "the same 3-pass signal subtraction + BP/OSD hybrid decode used by "
                "WSJT-X itself.<br><br>"
                "Without it, FT4 RX falls back to the lightweight ft8_lib "
                "single-pass decoder, which recovers noticeably fewer stations "
                "in a crowded pass (weak or closely-spaced overlapping signals "
                "are more likely to be missed).<br><br>"
                "FT4 TX always uses ft8_lib regardless — this only affects RX."
            )
        )
        info_lbl.setOpenExternalLinks(True)
        info_lbl.setWordWrap(True)
        il.addWidget(info_lbl)
        root.addWidget(info_box)

        # --- Manual build instructions ---
        self._manual_box = QGroupBox(_("Manual Build (Linux / macOS)"))
        ml = QVBoxLayout(self._manual_box)
        manual_text = QTextBrowser()
        manual_text.setOpenExternalLinks(True)
        manual_text.setFixedHeight(120)
        manual_text.setHtml(
            "<pre style='font-size:11px'>"
            "cd /path/to/fbsat59\n"
            "./scripts/build_ft4wsjt.sh  # clones wsjtx/wsjtx and builds libft4wsjt\n"
            "mkdir -p ~/.local/share/fbsat59/ft4wsjt/\n"
            "cp ft4wsjt-out/libft4wsjt.* ~/.local/share/fbsat59/ft4wsjt/"
            "</pre>"
        )
        ml.addWidget(manual_text)
        ml.addWidget(make_copy_button(manual_text.toPlainText))
        root.addWidget(self._manual_box)

        # --- Bundle download ---
        self._download_box = QGroupBox(_("Install from GitHub Releases (Recommended)"))
        dl = QVBoxLayout(self._download_box)
        dl.addWidget(
            QLabel(
                _(
                    "Downloads a pre-built libft4wsjt from this project's GitHub Releases\n"
                    "and installs it to your user data directory."
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

        # --- Uninstall (only shown when a user-installed copy exists) ---
        uninstall_row = QHBoxLayout()
        self._btn_uninstall = QPushButton(_("Uninstall"))
        self._btn_uninstall.setStyleSheet("QPushButton{color:#cc3300;}")
        self._btn_uninstall.setToolTip(
            _(
                "Remove the user-installed libft4wsjt from your data directory.\n"
                "If FT4 is currently in use, this may fail with a permission\n"
                "error — close that tab first."
            )
        )
        self._btn_uninstall.clicked.connect(self._on_uninstall)
        self._btn_uninstall.setVisible(False)
        uninstall_row.addStretch()
        uninstall_row.addWidget(self._btn_uninstall)
        root.addLayout(uninstall_row)

        # --- Buttons ---
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------ #
    # Status refresh
    # ------------------------------------------------------------------ #

    def _refresh_status(self) -> None:
        path = _find_libft4wsjt()
        if path is None:
            self._lbl_status.setText(
                "<b style='color:#e74c3c'>&#x2718; " + _("libft4wsjt not found") + "</b>"
            )
            self._lbl_path.setText("")
            self._manual_box.setVisible(True)
            self._download_box.setVisible(True)
            self._btn_uninstall.setVisible(False)
        else:
            source = _detect_source(str(path))
            self._lbl_status.setText(
                "<b style='color:#27ae60'>&#x2714; " + _("libft4wsjt found") + f" ({source})</b>"
            )
            self._lbl_path.setText(_("Path: ") + str(path))
            self._manual_box.setVisible(False)
            self._download_box.setVisible(True)
            self._btn_uninstall.setVisible(_is_user_installed(str(path)))

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
            _("Uninstall FT4 Enhanced Decoder"),
            _(
                "Remove the user-installed libft4wsjt from your data directory?\n\n"
                "If FT4 is currently in use, this may fail — close that tab "
                "(or restart FBSAT59) and try again."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        free_libft4wsjt()
        try:
            shutil.rmtree(get_user_ft4wsjt_dir())
        except Exception as exc:
            QMessageBox.warning(self, _("Uninstall Failed"), str(exc))
            return
        self._refresh_status()
