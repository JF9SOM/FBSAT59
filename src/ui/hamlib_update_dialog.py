"""
Hamlib Update dialog.

Opened from Help > Hamlib Update… or via the link in Rig / Rotator Settings.

Checks this project's own 'hamlib-bundle' release for the newest pre-built
package matching the current platform, architecture and Python version, then
installs it to the per-user data directory so the bundled version can be
replaced without touching the (possibly read-only) AppImage.

Platforms:
  Linux   — downloads hamlib-linux-x86_64-pyXYZ-<ver>.tar.gz (custom CI asset)
  Windows — downloads hamlib-windows-x86_64-pyXYZ-<ver>.zip (custom CI asset, flat layout)
  macOS   — downloads hamlib-macos-<arch>-pyXYZ-<ver>.tar.gz (custom CI asset)
"""

from __future__ import annotations

import json
import logging
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot
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

from core.hamlib_info import (
    HAMLIB_GITHUB_API,
    HAMLIB_GITHUB_RELEASES,
    get_hamlib_version,
    get_hamlib_version_number,
    get_user_hamlib_dir,
    get_user_hamlib_version,
    is_update_available,
    select_newest_asset,
)
from i18n import _

logger = logging.getLogger(__name__)


class _CheckWorker(QThread):
    """Fetches the newest bundled Hamlib build from the GitHub Releases API."""

    result = Signal(str, str)  # latest_version, download_url (empty if not found)
    error = Signal(str)

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                HAMLIB_GITHUB_API,
                headers={
                    "User-Agent": "fbsat59/1.0",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data: dict[str, object] = json.loads(resp.read())

            raw_assets = data.get("assets")
            assets: list[dict[str, object]] = (
                [a for a in raw_assets if isinstance(a, dict)]
                if isinstance(raw_assets, list)
                else []
            )

            version, url = select_newest_asset(assets)
            self.result.emit(version, url)
        except Exception as exc:
            self.error.emit(str(exc))


class _DownloadWorker(QThread):
    """Downloads and installs a Hamlib package to the user data directory."""

    progress = Signal(str)
    finished = Signal(bool, str)  # success, message

    def __init__(self, url: str, version: str) -> None:
        super().__init__()
        self._url = url
        self._version = version

    def run(self) -> None:
        try:
            dest_dir = get_user_hamlib_dir()
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Download
            fname = self._url.split("/")[-1]
            tmp_path = dest_dir / fname
            self.progress.emit(f"Downloading {fname}…")
            urllib.request.urlretrieve(self._url, tmp_path, reporthook=self._reporthook)

            # Extract
            self.progress.emit("Extracting…")
            if fname.endswith(".tar.gz"):
                self._extract_tarball(tmp_path, dest_dir)
            elif fname.endswith(".zip"):
                self._extract_zip(tmp_path, dest_dir)

            tmp_path.unlink(missing_ok=True)

            # Write installed version
            (dest_dir / "version.txt").write_text(self._version)

            self.finished.emit(
                True, _("Hamlib {ver} installed successfully.").format(ver=self._version)
            )
        except Exception as exc:
            logger.exception("Hamlib download/install failed")
            self.finished.emit(False, str(exc))

    def _reporthook(self, count: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            pct = min(100, int(count * block_size * 100 / total_size))
            self.progress.emit(f"Downloading… {pct}%")

    def _extract_tarball(self, path: Path, dest: Path) -> None:
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                # Strip the top-level directory (e.g. hamlib-linux-x86_64-py311-4.7.2/)
                parts = Path(member.name).parts
                if len(parts) < 2:
                    continue
                member.name = str(Path(*parts[1:]))
                tf.extract(member, dest)

    def _extract_zip(self, path: Path, dest: Path) -> None:
        # Windows package is flat (no top-level subdirectory), so extract as-is.
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(dest)


class HamlibUpdateDialog(QDialog):
    """
    Hamlib Update dialog.

    Displays the currently loaded version, checks GitHub for the latest
    release, and installs updates to the user data directory.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Hamlib Update"))
        self.resize(560, 480)
        self._check_worker: _CheckWorker | None = None
        self._action_worker: _DownloadWorker | None = None
        self._latest_version: str = ""
        self._download_url: str = ""
        self._setup_ui()
        self._refresh_current()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # -- Current installation --
        cur_grp = QGroupBox(_("Installed Hamlib"))
        cur_v = QVBoxLayout(cur_grp)
        self._cur_label = QLabel()
        cur_v.addWidget(self._cur_label)
        layout.addWidget(cur_grp)

        # -- Latest release --
        latest_grp = QGroupBox(_("Latest Release"))
        latest_v = QVBoxLayout(latest_grp)
        self._latest_label = QLabel(_("Not checked yet."))
        row = QHBoxLayout()
        row.addWidget(self._latest_label)
        row.addStretch()
        self._check_btn = QPushButton(_("Check for Updates"))
        self._check_btn.clicked.connect(self._on_check)
        row.addWidget(self._check_btn)
        latest_v.addLayout(row)
        layout.addWidget(latest_grp)

        # -- Action --
        action_grp = QGroupBox(_("Installation"))
        action_v = QVBoxLayout(action_grp)
        self._action_label = QLabel()
        self._action_label.setWordWrap(True)
        action_v.addWidget(self._action_label)
        self._install_btn = QPushButton(_("Download & Install"))
        self._install_btn.setVisible(False)
        self._install_btn.clicked.connect(self._on_install)
        action_v.addWidget(self._install_btn)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        action_v.addWidget(self._progress)
        layout.addWidget(action_grp)

        # -- Log --
        log_grp = QGroupBox(_("Log"))
        log_v = QVBoxLayout(log_grp)
        self._log = QTextBrowser()
        self._log.setMaximumHeight(110)
        log_v.addWidget(self._log)
        layout.addWidget(log_grp)

        # -- Buttons --
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        btn_row.addWidget(close_box)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh_current(self) -> None:
        bundled = get_hamlib_version()
        user_ver = get_user_hamlib_version()
        if user_ver:
            text = _("Active: <b>{ver}</b> (user-installed in {dir})").format(
                ver=bundled, dir=get_user_hamlib_dir()
            )
        else:
            text = _("Active: <b>{ver}</b> (bundled)").format(ver=bundled)
        self._cur_label.setText(text)

        install_dir = get_user_hamlib_dir()
        self._action_label.setText(
            _(
                "A portable Hamlib package will be downloaded and installed to:\n"
                "{dir}\n\n"
                "A restart is required to activate the new version."
            ).format(dir=install_dir)
        )

    # ------------------------------------------------------------------
    # Check for updates
    # ------------------------------------------------------------------

    def _on_check(self) -> None:
        self._check_btn.setEnabled(False)
        self._latest_label.setText(_("Checking…"))
        self._check_worker = _CheckWorker()
        self._check_worker.result.connect(self._on_check_result)
        self._check_worker.error.connect(self._on_check_error)
        self._check_worker.start()

    @Slot(str, str)
    def _on_check_result(self, version: str, url: str) -> None:
        self._check_btn.setEnabled(True)
        self._latest_version = version
        self._download_url = url

        # A user-installed copy takes effect only after a restart, so trust its
        # recorded version over the still-loaded one when deciding staleness.
        current = get_user_hamlib_version() or get_hamlib_version_number()
        if version:
            self._latest_label.setText(_("Latest: <b>{ver}</b>").format(ver=version))

        if not url:
            self._latest_label.setText(_("Check failed."))
            self._log.append(
                _(
                    "Pre-built package not found for this platform / Python version.\n"
                    "See {url} for manual installation."
                ).format(url=HAMLIB_GITHUB_RELEASES)
            )
            self._install_btn.setVisible(False)
            return

        if not is_update_available(version, current):
            self._log.append(_("Already up to date ({ver}).").format(ver=current))
            self._install_btn.setVisible(False)
            return

        self._install_btn.setText(_("Download & Install Hamlib {ver}").format(ver=version))
        self._install_btn.setVisible(True)

    @Slot(str)
    def _on_check_error(self, msg: str) -> None:
        self._check_btn.setEnabled(True)
        self._latest_label.setText(_("Check failed."))
        self._log.append(_("Error: {msg}").format(msg=msg))

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def _on_install(self) -> None:
        self._install_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._log.clear()

        action_worker = _DownloadWorker(self._download_url, self._latest_version)
        action_worker.progress.connect(self._on_progress)
        action_worker.finished.connect(self._on_finished)
        action_worker.start()
        self._action_worker = action_worker

    @Slot(str)
    def _on_progress(self, line: str) -> None:
        self._log.append(line)

    @Slot(bool, str)
    def _on_finished(self, success: bool, msg: str) -> None:
        self._progress.setVisible(False)
        self._install_btn.setEnabled(True)
        icon = "✅" if success else "❌"
        self._log.append(f"\n{icon}  {msg}")
        if success:
            self._refresh_current()
            self._log.append(_("\n⚠️  Restart FBSAT59 to activate the new Hamlib version."))
            # Disable install button — already installed
            self._install_btn.setVisible(False)


def open_hamlib_update_dialog(parent: QWidget | None = None) -> None:
    """Open the Hamlib Update dialog (convenience wrapper)."""
    dlg = HamlibUpdateDialog(parent)
    dlg.exec()
