"""Help > Q65 Library Installation… dialog.

Shows the current libq65 status (path, version) and provides
a one-click download-and-install from GitHub Releases.

libq65 is built from the WSJT-X source tree (lib/qra/q65/) and is
required for Q65 RX decoding.  Q65 TX encoding is pure Python and
does NOT require libq65.

Detection priority (mirrors _find_libq65()):
  1. User-installed   ~/.local/share/fbsat59/q65lib/
  2. PyInstaller bundle (_MEIPASS)
  3. Repo-local q65lib-bundle/ (development)
"""

from __future__ import annotations

import platform
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

from comms.q65.codec import _find_libq65
from i18n import _
from ui.copyable_text import make_copy_button, make_run_button

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RELEASE_TAG = "q65lib-bundle"


def _get_user_q65lib_dir() -> Path:
    import platformdirs

    return Path(platformdirs.user_data_dir("fbsat59")) / "q65lib"


def _detect_source(lib_path: str) -> str:
    """Return a human-readable source label for the resolved library path."""
    user_dir = _get_user_q65lib_dir()
    p = Path(lib_path)
    try:
        p.relative_to(user_dir)
        return _("User-installed")
    except ValueError:
        pass
    if getattr(sys, "frozen", False):
        try:
            p.relative_to(Path(getattr(sys, "_MEIPASS", "")))
            return _("Bundled")
        except ValueError:
            pass
    return _("Development / System")


# ---------------------------------------------------------------------------
# Background worker: download & install q65lib bundle
# ---------------------------------------------------------------------------


class _InstallWorker(QThread):
    """Downloads the latest q65lib bundle from GitHub Releases."""

    progress = Signal(int)  # 0-100
    status = Signal(str)
    finished_ok = Signal(str)  # installed path
    finished_err = Signal(str)

    _REPO = "JF9SOM/fbsat59"

    def run(self) -> None:
        import json
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
            suffix = f"q65lib-linux-{machine}.tar.gz"
            lib_name = "libq65.so"
        elif plat == "win32":
            suffix = "q65lib-windows-x86_64.zip"
            lib_name = "q65.dll"
        elif plat == "darwin":
            suffix = f"q65lib-macos-{machine}.tar.gz"
            lib_name = "libq65.dylib"
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
                    "No q65lib package found for this platform in the release.\n"
                    "Please build libq65 manually — see the instructions above."
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

        dest_dir = _get_user_q65lib_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            if suffix.endswith(".tar.gz"):
                with tarfile.open(tmp_path) as tar:
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
        self.finished_ok.emit(str(dest_dir / lib_name))


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------


class Q65LibDialog(QDialog):
    """Help > Q65 Library Installation… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Q65 Library Installation"))
        self.setMinimumWidth(540)
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

        # --- About libq65 ---
        info_box = QGroupBox(_("About libq65"))
        il = QVBoxLayout(info_box)
        info_lbl = QLabel(
            _(
                "libq65 is built from the "
                "<a href='https://wsjt.sourceforge.io/'>WSJT-X</a> source tree "
                "(<code>lib/qra/q65/</code>) by Joe Taylor K1JT and the WSJT-X "
                "Development Group (GPL-2.0).<br><br>"
                "It is required for:<br>"
                "  • <b>Q65 RX</b> — decoding received Q65 signals<br><br>"
                "Q65 TX (transmit) is implemented in pure Python and does "
                "<b>not</b> require libq65."
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
        manual_text.setFixedHeight(175)
        manual_text.setHtml(
            "<pre style='font-size:11px'>"
            "# Download WSJT-X source and build libq65\n"
            "wget https://wsjt.sourceforge.io/downloads/wsjtx-2.7.0.tar.gz\n"
            "tar -xzf wsjtx-2.7.0.tar.gz\n"
            "cd wsjtx-2.7.0/lib/qra/q65\n\n"
            "gcc -O2 -fPIC -shared \\\n"
            "  -I.. -I../../../ \\\n"
            "  q65.c libq65.c \\\n"
            "  ../crc.c ../qra128/qra128.c \\\n"
            "  -lm -o libq65.so\n\n"
            "mkdir -p ~/.local/share/fbsat59/q65lib/\n"
            "cp libq65.so ~/.local/share/fbsat59/q65lib/"
            "</pre>"
        )
        ml.addWidget(manual_text)
        manual_btn_row = QHBoxLayout()
        manual_btn_row.addWidget(make_copy_button(manual_text.toPlainText))
        manual_btn_row.addWidget(make_run_button(manual_text.toPlainText))
        manual_btn_row.addStretch()
        ml.addLayout(manual_btn_row)
        root.addWidget(self._manual_box)

        # --- Bundle download ---
        self._download_box = QGroupBox(_("Install from GitHub Releases (Recommended)"))
        dl = QVBoxLayout(self._download_box)
        dl.addWidget(
            QLabel(
                _(
                    "Downloads a pre-built libq65 from this project's GitHub Releases\n"
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

        # --- Buttons ---
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------ #
    # Status refresh
    # ------------------------------------------------------------------ #

    def _refresh_status(self) -> None:
        path = _find_libq65()
        if path is None:
            self._lbl_status.setText(
                "<b style='color:#e74c3c'>&#x2718; " + _("libq65 not found") + "</b>"
            )
            self._lbl_path.setText("")
            self._manual_box.setVisible(True)
        else:
            source = _detect_source(str(path))
            self._lbl_status.setText(
                "<b style='color:#27ae60'>&#x2714; " + _("libq65 found") + f" ({source})</b>"
            )
            self._lbl_path.setText(_("Path: ") + str(path))
            self._manual_box.setVisible(False)

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
