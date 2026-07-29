"""Help > ft8lib Installation… dialog.

Shows the current ft8lib status (path, version) and provides
a one-click download-and-install from GitHub Releases.

ft8lib (kgoba/ft8_lib) provides FT4/FT8 message encoding and decoding.
It is required for FT4 TX/RX and Q65 TX (pack77).

Detection priority (mirrors _find_ft8lib()):
  1. User-installed   ~/.local/share/fbsat59/ft8lib/
  2. System path      ctypes.util.find_library("ft8")
  3. Bundled          _MEIPASS/libft8.so (PyInstaller)
"""

from __future__ import annotations

import ctypes
import logging
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

from comms.ft4.codec import _find_ft8lib, free_ft8lib, get_user_ft8lib_dir
from i18n import _

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_ft8lib_version(lib: ctypes.CDLL) -> str:
    """Try to retrieve a version string from the loaded library."""
    for sym in ("ft8lib_version", "ft4_lib_version", "ftx_lib_version"):
        try:
            fn = getattr(lib, sym)
            fn.restype = ctypes.c_char_p
            raw: bytes | None = fn()
            if raw is not None:
                return raw.decode("utf-8", errors="replace")
        except AttributeError:
            continue
    return _("(version symbol not available)")


def _is_user_installed(lib_path: str) -> bool:
    """True if lib_path resolves inside the user install directory."""
    try:
        Path(lib_path).relative_to(get_user_ft8lib_dir())
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
    return _("System")


# ---------------------------------------------------------------------------
# Background worker: download & install ft8lib bundle
# ---------------------------------------------------------------------------

_RELEASE_TAG = "ft8lib-bundle"


class _InstallWorker(QThread):
    """Downloads the latest ft8lib bundle from GitHub Releases."""

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
        import time
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
            suffix = f"ft8lib-linux-{machine}.tar.gz"
            lib_name = "libft8.so"
        elif plat == "win32":
            suffix = "ft8lib-windows-x86_64.zip"
            lib_name = "ft8.dll"
        elif plat == "darwin":
            suffix = f"ft8lib-macos-{machine}.tar.gz"
            lib_name = "libft8.dylib"
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
                    "No ft8lib package found for this platform in the release.\n"
                    "Please build ft8_lib manually — see the instructions above."
                )
            )
            return

        self.status.emit(_("Downloading…"))
        logger.info("[ft8lib install diag] download start url=%s", url)
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)

            def _reporthook(block: int, block_size: int, total: int) -> None:
                if total > 0:
                    self.progress.emit(int(block * block_size * 100 / total))

            urllib.request.urlretrieve(url, tmp_path, reporthook=_reporthook)
        except Exception as exc:
            logger.exception("[ft8lib install diag] download failed")
            self.finished_err.emit(str(exc))
            return
        logger.info("[ft8lib install diag] download done tmp_path=%s", tmp_path)

        self.progress.emit(95)
        self.status.emit(_("Installing…"))

        dest_dir = get_user_ft8lib_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)

        def _do_extract() -> None:
            if suffix.endswith(".tar.gz"):
                with tarfile.open(tmp_path) as tar:
                    # Strip the top-level "ft8lib-flat/" directory if present
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

        # A freshly (re)created destination file can transiently fail to
        # open for writing right after extraction starts — e.g. a real-time
        # antivirus scan grabbing the newly-written .dll for a moment. Retry
        # a few times with a short backoff before giving up, since this
        # class of lock is normally released within a few hundred ms; a
        # real, persistent lock (the FT4 tab still holding the DLL open)
        # will still fail all retries and surface the same message as before.
        _EXTRACT_RETRIES = 3
        _EXTRACT_RETRY_DELAY_S = 0.3
        last_exc: PermissionError | None = None
        try:
            logger.info("[ft8lib install diag] extraction start dest_dir=%s", dest_dir)
            for attempt in range(1, _EXTRACT_RETRIES + 1):
                try:
                    _do_extract()
                    if attempt > 1:
                        logger.info(
                            "[ft8lib install diag] extraction succeeded on retry attempt=%d",
                            attempt,
                        )
                    last_exc = None
                    break
                except PermissionError as exc:
                    last_exc = exc
                    logger.info(
                        "[ft8lib install diag] extraction attempt=%d/%d "
                        "PermissionError: %r (winerror=%s)",
                        attempt,
                        _EXTRACT_RETRIES,
                        exc,
                        getattr(exc, "winerror", None),
                    )
                    if attempt < _EXTRACT_RETRIES:
                        time.sleep(_EXTRACT_RETRY_DELAY_S)
            if last_exc is not None:
                raise last_exc
            tmp_path.unlink(missing_ok=True)
            logger.info("[ft8lib install diag] extraction done")
        except PermissionError as exc:
            # exc.filename is the raw OS path; str(exc)/format() would show it
            # through repr() instead, which escapes backslashes as "\\\\" and
            # makes Windows paths look like they contain doubled separators.
            logger.exception(
                "[ft8lib install diag] extraction PermissionError after %d attempts",
                _EXTRACT_RETRIES,
            )
            path_str = exc.filename or str(exc)
            self.finished_err.emit(
                _(
                    "Permission denied: {path}\n"
                    "The library file is in use — close the FT4 tab (if open) "
                    "and restart FBSAT59, then try installing again."
                ).format(path=path_str)
            )
            return
        except Exception as exc:
            logger.exception("[ft8lib install diag] extraction failed")
            self.finished_err.emit(str(exc))
            return

        self.progress.emit(100)
        logger.info("[ft8lib install diag] emitting finished_ok, about to return from run()")
        self.finished_ok.emit(str(dest_dir / lib_name))
        logger.info("[ft8lib install diag] finished_ok emitted, run() returning")


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------


class Ft8LibDialog(QDialog):
    """Help > ft8lib Installation… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("ft8lib Installation"))
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
        self._lbl_version = QLabel()
        self._lbl_version.setWordWrap(True)
        sl.addWidget(self._lbl_status)
        sl.addWidget(self._lbl_path)
        sl.addWidget(self._lbl_version)
        root.addWidget(status_box)

        # --- About ft8lib ---
        info_box = QGroupBox(_("About ft8lib"))
        il = QVBoxLayout(info_box)
        info_lbl = QLabel(
            _(
                "ft8lib (<a href='https://github.com/kgoba/ft8_lib'>kgoba/ft8_lib</a>) "
                "by Kārlis Goba YL3JG provides FT4/FT8 message encoding and decoding "
                "(GPL-2.0).<br><br>"
                "It is required for:<br>"
                "  • <b>FT4 TX/RX</b> — encoding transmit audio and decoding received messages<br>"
                "  • <b>Q65 TX</b> — packing callsign/grid into the 77-bit message payload"
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
        manual_text.setFixedHeight(160)
        manual_text.setHtml(
            "<pre style='font-size:11px'>"
            "mkdir -p ~/src &amp;&amp; cd ~/src\n"
            "git clone https://github.com/kgoba/ft8_lib.git\n"
            "cd ft8_lib\n"
            "make clean\n"
            'make -j$(nproc) CFLAGS="-O3 -DHAVE_STPCPY -I. -fPIC"\n'
            "gcc -shared -fPIC -o libft8.so \\\n"
            "  .build/ft8/*.o .build/common/*.o .build/fft/*.o\n"
            "mkdir -p ~/.local/share/fbsat59/ft8lib/\n"
            "cp libft8.so ~/.local/share/fbsat59/ft8lib/"
            "</pre>"
        )
        ml.addWidget(manual_text)
        root.addWidget(self._manual_box)

        # --- Bundle download ---
        self._download_box = QGroupBox(_("Install from GitHub Releases (Recommended)"))
        dl = QVBoxLayout(self._download_box)
        dl.addWidget(
            QLabel(
                _(
                    "Downloads a pre-built ft8lib from this project's GitHub Releases\n"
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
                "Remove the user-installed ft8lib from your data directory.\n"
                "If FT4 or Q65 is currently in use, this may fail with a\n"
                "permission error — close those tabs first."
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
        logger.info("[ft8lib install diag] _refresh_status: calling _find_ft8lib()")
        lib = _find_ft8lib()
        logger.info("[ft8lib install diag] _refresh_status: _find_ft8lib() returned lib=%s", lib)
        if lib is None:
            self._lbl_status.setText(
                "<b style='color:#e74c3c'>&#x2718; " + _("ft8lib not found") + "</b>"
            )
            self._lbl_path.setText("")
            self._lbl_version.setText("")
            self._manual_box.setVisible(True)
            self._download_box.setVisible(True)
            self._btn_uninstall.setVisible(False)
        else:
            # Try to determine where it was loaded from
            lib_path = getattr(lib, "_name", "") or _("unknown")
            source = _detect_source(lib_path)
            logger.info("[ft8lib install diag] _refresh_status: calling _get_ft8lib_version()")
            version = _get_ft8lib_version(lib)
            logger.info("[ft8lib install diag] _refresh_status: version=%s", version)
            self._lbl_status.setText(
                "<b style='color:#27ae60'>&#x2714; " + _("ft8lib found") + f" ({source})</b>"
            )
            self._lbl_path.setText(_("Path: ") + lib_path)
            self._lbl_version.setText(_("Version: ") + version)
            self._manual_box.setVisible(False)
            self._download_box.setVisible(True)
            self._btn_uninstall.setVisible(_is_user_installed(lib_path))
            logger.info("[ft8lib install diag] _refresh_status: calling free_ft8lib()")
            free_ft8lib(lib)
            logger.info("[ft8lib install diag] _refresh_status: free_ft8lib() returned")

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
        logger.info("[ft8lib install diag] _on_install_ok received path=%s", path)
        self._progress.setValue(100)
        self._lbl_dl_status.setText(_("Installed: ") + path)
        self._btn_download.setEnabled(True)
        self._refresh_status()
        logger.info("[ft8lib install diag] _on_install_ok: _refresh_status() returned")

    def _on_install_err(self, msg: str) -> None:
        self._lbl_dl_status.setText(_("Error: ") + msg)
        self._btn_download.setEnabled(True)
        self._progress.setVisible(False)

    def _on_uninstall(self) -> None:
        reply = QMessageBox.question(
            self,
            _("Uninstall ft8lib"),
            _(
                "Remove the user-installed ft8lib from your data directory?\n\n"
                "If FT4 or Q65 is currently in use, this may fail — close "
                "those tabs (or restart FBSAT59) and try again."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        lib = _find_ft8lib()
        if lib is not None:
            free_ft8lib(lib)
        try:
            shutil.rmtree(get_user_ft8lib_dir())
        except Exception as exc:
            QMessageBox.warning(self, _("Uninstall Failed"), str(exc))
            return
        self._refresh_status()
