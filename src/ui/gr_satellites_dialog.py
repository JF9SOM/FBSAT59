"""Help > gr-satellites… dialog.

Detects whether gr-satellites is available (bundled or system) and offers a
one-click download of the bundled conda-pack environment, plus manual
installation guidance as a fallback.

Detection priority (mirrors find_gr_satellites_executable()):
  1. Bundled conda-pack env   ~/.local/share/fbsat59/gr-satellites-env/
  2. System install           shutil.which("gr_satellites")  (apt/conda/etc.)

Note on gr-satellites' PyPI (non-)availability: earlier versions of this
dialog recommended ``pip install gr-satellites``, but no such package exists
on PyPI (confirmed: https://pypi.org/simple/gr-satellites/ returns 404).
The correct methods — conda-forge, the Ubuntu PPA, or building from source
— are documented at
https://gr-satellites.readthedocs.io/en/latest/installation.html and
https://gr-satellites.readthedocs.io/en/latest/installation_conda.html
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

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
    QVBoxLayout,
    QWidget,
)

from comms.telemetry.gr_satellites_install import (
    bundled_version,
    find_gr_satellites_executable,
    is_bundle_installed,
    uninstall_bundle,
    user_gr_satellites_dir,
)
from i18n import _
from ui.copyable_text import CommandRow, make_selectable

logger = logging.getLogger(__name__)

_RELEASE_TAG = "gr-satellites-bundle"
_REPO = "JF9SOM/fbsat59"

# ---------------------------------------------------------------------------
# Detection helper
# ---------------------------------------------------------------------------


def _detect_gr_satellites() -> tuple[bool, str, bool]:
    """Return (is_installed, detail, is_bundled)."""
    resolved = find_gr_satellites_executable()
    if resolved is None:
        return False, "", False
    exe_path, bundled = resolved
    if bundled:
        ver = bundled_version()
        detail = f"Bundled ({ver})" if ver else "Bundled"
        return True, detail, True
    return True, f"System: {exe_path}", False


# ---------------------------------------------------------------------------
# Platform-specific manual installation instructions (fallback)
# ---------------------------------------------------------------------------


def _get_instructions() -> tuple[str, str]:
    """Build platform-specific *manual* installation guidance.

    This is shown as a fallback for platforms the bundled download doesn't
    (yet) cover, or for users who prefer to manage GNU Radio themselves.
    Built lazily (not as module-level constants) so the narrative text picks
    up the current UI language each time the dialog is opened.

    Returns ``(html, primary_command)``. ``primary_command`` covers only the
    GNU Radio installation step (via the OS package manager), which is safe
    to run unconditionally — it is what the Copy/Run-in-Terminal buttons act
    on. Installing gr-satellites itself has three valid methods (PPA,
    conda-forge, source build) with no single "correct" default, so those
    are shown as reference text only (still mouse-selectable).
    """
    conda_cmd = "conda install -c conda-forge gnuradio-satellites"
    source_build = (
        "git clone https://github.com/daniestevez/gr-satellites.git\n"
        "cd gr-satellites\n"
        "mkdir build && cd build\n"
    )
    if sys.platform == "linux":
        html = (
            "<b>Ubuntu / Debian</b><br>\n"
            "gr-satellites requires GNU Radio 3.10+ (Ubuntu 22.04+).<br><br>\n"
            "<pre>sudo apt install gnuradio python3-gnuradio</pre>\n\n"
            "Then install gr-satellites itself via one of:<br><br>\n"
            "<b>Ubuntu PPA</b> (Ubuntu 20.04–25.10):<br>\n"
            "<pre>sudo add-apt-repository ppa:daniestevez/gr-satellites\n"
            "sudo apt update\n"
            "sudo apt install gr-satellites</pre>\n\n"
            "<b>conda-forge</b> (if you already use conda/mamba):<br>\n"
            f"<pre>{conda_cmd}</pre>\n\n"
            "<b>Or build from source</b>:<br>\n"
            f"<pre>{source_build}"
            "cmake .. && make -j$(nproc)\n"
            "sudo make install\n"
            "sudo ldconfig</pre>\n\n"
            "After installation, verify with: <tt>gr_satellites --help</tt>\n"
        )
        return html, "sudo apt install gnuradio python3-gnuradio"
    if sys.platform == "darwin":
        html = (
            "<b>macOS (Homebrew)</b><br><br>\n"
            "<pre>brew install gnuradio</pre>\n\n"
            "Homebrew does not package gr-satellites itself. Install it via:<br><br>\n"
            "<b>conda-forge</b> (if you already use conda/mamba, e.g. radioconda):<br>\n"
            f"<pre>{conda_cmd}</pre>\n\n"
            "<b>Or build from source</b>:<br>\n"
            f"<pre>{source_build}"
            "cmake .. && make -j$(sysctl -n hw.logicalcpu)\n"
            "sudo make install</pre>\n"
        )
        return html, "brew install gnuradio"
    if sys.platform == "win32":
        html = (
            "<b>Windows</b><br><br>\n"
            "The bundled environment above (Install Bundled Environment) is "
            "the recommended way to get gr-satellites on Windows — it is "
            "built and verified by this project's own CI.<br><br>\n"
            "If you'd rather manage GNU Radio yourself, the official docs "
            "recommend conda as the easiest way to install GNU Radio + "
            "gr-satellites together on Windows:<br>\n"
            "<pre>conda install -c conda-forge gnuradio gnuradio-satellites</pre>\n"
        )
        return html, ""
    generic_note = _(
        "Install GNU Radio 3.10+ from your system package manager or\n"
        '<a href="https://www.gnuradio.org/">gnuradio.org</a>, then install '
        "gr-satellites via conda-forge or from source:"
    )
    return f"{generic_note}<br>\n<pre>{conda_cmd}</pre>\n", ""


# ---------------------------------------------------------------------------
# Background worker: download & install the bundled conda-pack environment
# ---------------------------------------------------------------------------


class _InstallWorker(QThread):
    """Downloads and extracts the bundled gr-satellites conda-pack environment."""

    progress = Signal(int)  # 0-100
    status = Signal(str)
    finished_ok = Signal(str)  # installed path
    finished_err = Signal(str)

    def run(self) -> None:
        import json
        import platform
        import tarfile
        import tempfile
        import urllib.request
        from pathlib import Path

        self.status.emit(_("Checking latest release…"))
        api_url = f"https://api.github.com/repos/{_REPO}/releases/tags/{_RELEASE_TAG}"
        try:
            req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            self.finished_err.emit(str(exc))
            return

        assets = data.get("assets", [])
        machine = platform.machine().lower()
        if sys.platform == "darwin":
            suffix = f"gr-satellites-macos-{machine}.tar.gz"
        elif sys.platform == "linux":
            suffix = f"gr-satellites-linux-{machine}.tar.gz"
        elif sys.platform == "win32":
            suffix = "gr-satellites-windows-x86_64.tar.gz"
        else:
            self.finished_err.emit(_("Unsupported platform"))
            return

        url = next((a["browser_download_url"] for a in assets if a["name"] == suffix), None)
        if not url:
            self.finished_err.emit(
                _(
                    "No pre-built bundle is available yet for this platform "
                    "({suffix}). Use the manual installation instructions above."
                ).format(suffix=suffix)
            )
            return

        self.status.emit(_("Downloading… (this is a large download, ~hundreds of MB)"))
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)

            def _reporthook(block: int, block_size: int, total: int) -> None:
                if total > 0:
                    self.progress.emit(int(block * block_size * 100 / total))

            urllib.request.urlretrieve(url, tmp_path, reporthook=_reporthook)
        except Exception as exc:
            logger.exception("gr-satellites bundle download failed")
            self.finished_err.emit(str(exc))
            return

        self.progress.emit(95)
        self.status.emit(_("Extracting…"))

        dest_dir = user_gr_satellites_dir()
        shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            if suffix.endswith(".tar.gz"):
                with tarfile.open(tmp_path) as tar:
                    tar.extractall(dest_dir)
            else:
                import zipfile

                with zipfile.ZipFile(tmp_path) as zf:
                    zf.extractall(dest_dir)
            tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.exception("gr-satellites bundle extraction failed")
            self.finished_err.emit(str(exc))
            return

        self.status.emit(_("Fixing up environment paths (conda-unpack)…"))
        # Windows conda envs place entry points as Scripts/<name>.exe
        # launcher stubs (self-contained, run directly) with python.exe at
        # the env root; macOS/Linux use bin/<name> shebang scripts that
        # must be handed to bin/python explicitly (see
        # gr_satellites_install.py's module docstring for why).
        unpack_cmd: list[str] | None = None
        if sys.platform == "win32":
            unpack_exe = dest_dir / "Scripts" / "conda-unpack.exe"
            if unpack_exe.exists():
                unpack_cmd = [str(unpack_exe)]
        else:
            unpack_script = dest_dir / "bin" / "conda-unpack"
            python_bin = dest_dir / "bin" / "python"
            if unpack_script.exists() and python_bin.exists():
                unpack_cmd = [str(python_bin), str(unpack_script)]
        if unpack_cmd is not None:
            try:
                subprocess.run(
                    unpack_cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except Exception as exc:
                logger.exception("conda-unpack failed")
                self.finished_err.emit(_("conda-unpack failed: ") + str(exc))
                return

        self.progress.emit(100)
        self.finished_ok.emit(str(dest_dir))


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class GrSatellitesDialog(QDialog):
    """Help > gr-satellites… — status, one-click bundle install, and manual guidance."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("gr-satellites Installation"))
        self.resize(580, 560)
        self._worker: _InstallWorker | None = None
        self._setup_ui()
        self._refresh_status()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # -- Detection status --
        status_grp = QGroupBox(_("Detection Status"))
        sv = QVBoxLayout(status_grp)
        self._status_lbl = QLabel(_("Checking…"))
        self._status_lbl.setWordWrap(True)
        sv.addWidget(self._status_lbl)
        layout.addWidget(status_grp)

        # -- What is gr-satellites --
        about_grp = QGroupBox(_("About gr-satellites"))
        av = QVBoxLayout(about_grp)
        about_lbl = QLabel(
            _(
                "gr-satellites is an open-source GNU Radio out-of-tree (OOT) module "
                "that decodes telemetry from 100+ amateur satellites.\n\n"
                "When installed, FBSAT59 can launch gr_satellites as a "
                "subprocess and display decoded telemetry in the Telemetry tab "
                "(9600 baud and other non-AFSK modes will become available)."
            )
        )
        about_lbl.setWordWrap(True)
        av.addWidget(about_lbl)
        layout.addWidget(about_grp)

        # -- Bundle download (recommended) --
        self._download_grp = QGroupBox(_("Install Bundled Environment (Recommended)"))
        dl = QVBoxLayout(self._download_grp)
        dl.addWidget(
            QLabel(
                _(
                    "Downloads a pre-built, headless GNU Radio + gr-satellites "
                    "environment (no Qt/GUI components) from this project's "
                    "GitHub Releases and installs it to your user data "
                    "directory. This is a large download (hundreds of MB)."
                )
            )
        )
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._lbl_dl_status = QLabel()
        self._lbl_dl_status.setWordWrap(True)
        self._lbl_dl_status.setVisible(False)
        dl.addWidget(self._progress)
        dl.addWidget(self._lbl_dl_status)
        btn_row = QHBoxLayout()
        self._btn_download = QPushButton(_("Download && Install"))
        self._btn_download.clicked.connect(self._on_download)
        self._btn_uninstall = QPushButton(_("Uninstall"))
        self._btn_uninstall.setStyleSheet("QPushButton{color:#cc3300;}")
        self._btn_uninstall.clicked.connect(self._on_uninstall)
        btn_row.addWidget(self._btn_uninstall)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_download)
        dl.addLayout(btn_row)
        layout.addWidget(self._download_grp)

        # -- Manual installation instructions (fallback) --
        inst_grp = QGroupBox(_("Manual Installation (Alternative)"))
        iv = QVBoxLayout(inst_grp)
        instructions_html, primary_cmd = _get_instructions()
        inst_lbl = QLabel(instructions_html)
        inst_lbl.setWordWrap(True)
        inst_lbl.setOpenExternalLinks(True)
        inst_lbl.setTextFormat(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextFormat.RichText)
        make_selectable(inst_lbl)
        iv.addWidget(inst_lbl)
        if primary_cmd:
            iv.addWidget(CommandRow(primary_cmd, allow_run=True))
        layout.addWidget(inst_grp)

        # -- Close button --
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ------------------------------------------------------------------ #
    # Status refresh
    # ------------------------------------------------------------------ #

    def _refresh_status(self) -> None:
        is_installed, detail, _bundled = _detect_gr_satellites()
        if is_installed:
            self._status_lbl.setText(
                "<b style='color:#27ae60'>&#x2714; "
                + _("gr-satellites is installed: ")
                + detail
                + "</b>"
            )
        else:
            self._status_lbl.setText(
                "<b style='color:#e74c3c'>&#x2718; " + _("gr-satellites is NOT installed.") + "</b>"
            )
        self._btn_uninstall.setVisible(is_bundle_installed())

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

    def _on_uninstall(self) -> None:
        reply = QMessageBox.question(
            self,
            _("Uninstall gr-satellites"),
            _(
                "Remove the bundled gr-satellites environment from your data "
                "directory?\n\nIf the Telemetry tab is currently using it, "
                "this may fail — close that tab first."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            uninstall_bundle()
        except Exception as exc:
            QMessageBox.warning(self, _("Uninstall Failed"), str(exc))
            return
        self._refresh_status()
