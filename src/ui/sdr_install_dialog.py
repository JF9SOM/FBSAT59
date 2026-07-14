"""
SDR Device Installation dialog.

Opened from Help > SDR Device Installation.

Detects connected SDR USB devices and installed SoapySDR drivers.
Provides OS-specific installation guidance and, where possible, launches
automatic installation with a single button press.

OS support:
  Linux  — pkexec apt-get install (fully automatic)
  Windows — downloads PothosSDR installer + Zadig (semi-automatic)
  macOS  — brew install (automatic if Homebrew present)
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from i18n import _
from sdr import SOAPY_AVAILABLE
from sdr.device import SdrDeviceInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known SoapySDR apt/brew/pip package names per driver
# ---------------------------------------------------------------------------
_APT_PACKAGES: dict[str, list[str]] = {
    "SoapyRTLSDR": ["python3-soapysdr", "soapysdr-module-rtlsdr", "rtl-sdr"],
    "SoapyHackRF": ["python3-soapysdr", "soapysdr-module-hackrf", "hackrf"],
    "SoapyAirspy": ["python3-soapysdr", "soapysdr-module-airspy"],
    "SoapySDRPlay": ["python3-soapysdr", "soapysdr-module-sdrplay3"],
}
_BREW_PACKAGES: dict[str, list[str]] = {
    "SoapyRTLSDR": ["soapysdr", "soapyrtlsdr"],
    "SoapyHackRF": ["soapysdr", "soapyhackrf"],
    "SoapyAirspy": ["soapysdr", "soapyairspy"],
    "SoapySDRPlay": ["soapysdr", "soapysdrplay"],
}

# Windows: SoapySDR is bundled in the installer (extracted from conda-forge in CI).
# Zadig is still needed to switch RTL-SDR to the WinUSB driver.
_ZADIG_URL = "https://zadig.akeo.ie/"


class _InstallWorker(QThread):
    """Background thread that runs an installation command."""

    progress = Signal(str)
    finished = Signal(bool, str)  # success, message

    def __init__(self, cmd: list[str]) -> None:
        super().__init__()
        self._cmd = cmd

    def run(self) -> None:
        try:
            proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.progress.emit(line.rstrip())
            proc.wait()
            success = proc.returncode == 0
            msg = _("Installation complete.") if success else _("Installation failed (see log).")
            self.finished.emit(success, msg)
        except Exception as exc:
            self.finished.emit(False, str(exc))


class _EnumWorker(QObject):
    """Background worker that runs SdrDevice.enumerate() off the UI thread."""

    # Emits (soapy_devices, usb_devices) once the scan completes
    done = Signal(object, object)

    def run(self) -> None:
        from sdr.device import SdrDevice as _SdrDevice

        # Run a real enumerate so Rescan always reflects current state.
        # On Windows we use the subprocess path (same as Rig Settings) to
        # avoid segfaults in the native DLL loader.  On other platforms we
        # call enumerate() directly.  force=True bypasses the process-level
        # cache so the result is always fresh.
        try:
            soapy: list[SdrDeviceInfo] = _SdrDevice.enumerate(force=True)
        except Exception:
            logger.exception("SDR install dialog: SoapySDR enumerate failed")
            from sdr.device import _enumerate_cache

            soapy = list(_enumerate_cache) if _enumerate_cache is not None else []
        try:
            usb = _SdrDevice.enumerate_usb()
        except Exception:
            logger.exception("SDR install dialog: USB enumerate failed")
            usb = []
        self.done.emit(soapy, usb)


class SdrInstallDialog(QDialog):
    """
    SDR Device Installation dialog.

    Shows:
      1. Connected USB devices (with driver status)
      2. Installed SoapySDR module status
      3. OS-appropriate install button / guidance
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("SDR Device Installation"))
        self.resize(620, 560)
        self._worker: _InstallWorker | None = None
        self._enum_thread: QThread | None = None
        self._enum_worker: _EnumWorker | None = None
        self._setup_ui()
        # Delay initial enumerate by 300 ms so the dialog renders first and
        # the USB driver (especially libusbK / RTL-SDR on Windows) has time to
        # settle after any previous enumerate call from Rig Settings.
        QTimer.singleShot(300, self._start_refresh)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        # Wrap everything in a scroll area so the dialog is usable on small screens.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)

        # -- Connected devices --
        dev_grp = QGroupBox(_("Connected USB Devices"))
        self._dev_layout = QVBoxLayout(dev_grp)
        self._dev_placeholder = QLabel(_("Scanning…"))
        self._dev_layout.addWidget(self._dev_placeholder)
        layout.addWidget(dev_grp)

        # -- Installation status --
        status_grp = QGroupBox(_("Driver Status"))
        self._status_layout = QVBoxLayout(status_grp)
        self._soapy_status = QLabel()
        self._status_layout.addWidget(self._soapy_status)
        layout.addWidget(status_grp)

        # -- Action area --
        action_grp = QGroupBox(_("Installation"))
        action_v = QVBoxLayout(action_grp)

        self._action_label = QLabel()
        self._action_label.setWordWrap(True)
        action_v.addWidget(self._action_label)

        self._install_btn = QPushButton(_("Install Selected Packages"))
        self._install_btn.clicked.connect(self._on_install)
        action_v.addWidget(self._install_btn)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._progress_bar.setVisible(False)
        action_v.addWidget(self._progress_bar)

        layout.addWidget(action_grp)

        # -- Log output --
        log_grp = QGroupBox(_("Log"))
        log_v = QVBoxLayout(log_grp)
        self._log = QTextBrowser()
        self._log.setMaximumHeight(120)
        log_v.addWidget(self._log)
        layout.addWidget(log_grp)

        # -- SDRplay note --
        sdrplay_grp = QGroupBox(_("Note for SDRplay Users"))
        sdrplay_v = QVBoxLayout(sdrplay_grp)
        sdrplay_note = QLabel(
            _(
                "SDRplay devices (RSP1, RSP2, RSPdx, etc.) are not bundled on any platform "
                "because SoapySDRPlay3 depends on the proprietary SDRplay API library, "
                "which cannot be redistributed.\n\n"
                "To use an SDRplay device (all platforms):\n"
                "  1. Install the SDRplay API from https://www.sdrplay.com/downloads/\n"
                "     (Windows/macOS installer or Linux .run script)\n"
                "  2. Install SoapySDRPlay3:\n"
                "       Linux:   sudo apt install soapysdr-module-sdrplay3\n"
                "       macOS:   conda install -c conda-forge soapysdr-module-sdrplay3\n"
                "       Windows: conda install -c conda-forge soapysdr-module-sdrplay3\n"
                "                or build from https://github.com/pothosware/SoapySDRPlay3\n"
                "  3. Restart this software — your device will be detected automatically."
            )
        )
        sdrplay_note.setWordWrap(True)
        sdrplay_v.addWidget(sdrplay_note)
        layout.addWidget(sdrplay_grp)

        # -- ADALM-Pluto note --
        pluto_grp = QGroupBox(_("Note for ADALM-Pluto Users"))
        pluto_v = QVBoxLayout(pluto_grp)
        pluto_note = QLabel(
            _(
                "ADALM-Pluto (PlutoSDR) is not bundled on any platform and must be installed "
                "manually. Linux and macOS users can use package managers; Windows users need "
                "conda or a source build.\n\n"
                "How PlutoSDR networking works:\n"
                "  When connected via USB, PlutoSDR creates a virtual Ethernet adapter.\n"
                "  No special driver (Zadig / WinUSB) is needed on any platform.\n"
                "  The device is reachable at IP address 192.168.2.1.\n\n"
                "To use ADALM-Pluto (all platforms):\n"
                "  1. Connect PlutoSDR via USB (USB network adapter installs automatically).\n"
                "  2. Install libiio:\n"
                "       Linux:   sudo apt install libiio-dev\n"
                "       macOS:   brew install libiio\n"
                "       Windows: installer from https://github.com/analogdevicesinc/libiio/releases\n"
                "  3. Install SoapyPlutoSDR:\n"
                "       Linux:   sudo apt install soapysdr-module-plutosdr\n"
                "       macOS:   brew install soapyplutosdr\n"
                "                or conda install -c conda-forge soapysdr-module-plutosdr\n"
                "       Windows: conda install -c conda-forge soapysdr-module-plutosdr\n"
                "                or build from https://github.com/pothosware/SoapyPlutoSDR\n"
                "  4. Restart this software — PlutoSDR will be detected automatically."
            )
        )
        pluto_note.setWordWrap(True)
        pluto_v.addWidget(pluto_note)
        layout.addWidget(pluto_grp)

        # -- Remote SDR (SoapyRemote) note --
        remote_grp = QGroupBox(_("Note for Remote SDR Users"))
        remote_v = QVBoxLayout(remote_grp)
        remote_note = QLabel(
            _(
                "An SDR on another machine (e.g. a receiver in a separate location) can be "
                "used via SoapyRemote. Windows already bundles the client module; Linux and "
                "macOS users install it themselves like other SoapySDR modules.\n\n"
                "On the machine the SDR is physically connected to:\n"
                "  1. Install that SDR's own SoapySDR module as usual (e.g. "
                "soapysdr-module-rtlsdr).\n"
                "  2. Install and run the server:\n"
                "       Linux: sudo apt install soapyremote-server && SoapySDRServer\n"
                "       (see https://github.com/pothosware/SoapyRemote/wiki for other "
                "platforms)\n\n"
                "On this machine (the client):\n"
                "  Linux: sudo apt install soapysdr-module-remote\n"
                "  macOS: brew install soapyremote\n"
                "  Windows: bundled — nothing to install\n\n"
                "Devices on the same LAN as the server are often auto-discovered. If the "
                "server is on a different network (e.g. reachable only via VPN), use "
                "'Add Remote Host…' in Rig Settings > SDR Settings to enter its address "
                "directly."
            )
        )
        remote_note.setWordWrap(True)
        remote_v.addWidget(remote_note)
        layout.addWidget(remote_grp)

        # -- Rescan + close --
        btn_row = QHBoxLayout()
        self._rescan_btn = QPushButton(_("🔍 Rescan Devices"))
        self._rescan_btn.clicked.connect(self._start_refresh)
        btn_row.addWidget(self._rescan_btn)
        btn_row.addStretch()
        close_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn.rejected.connect(self.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Scan and populate
    # ------------------------------------------------------------------

    def _start_refresh(self) -> None:
        """Kick off an asynchronous device scan (does not block the UI thread)."""
        if self._enum_thread is not None and self._enum_thread.isRunning():
            return

        self._rescan_btn.setEnabled(False)
        self._dev_placeholder.setText(_("Scanning…"))

        obj = _EnumWorker()
        thread = QThread(self)
        obj.moveToThread(thread)
        obj.done.connect(self._on_enum_done)
        obj.done.connect(thread.quit)
        thread.started.connect(obj.run)
        thread.start()
        self._enum_thread = thread
        # keep a reference to the worker so it isn't GC'd before done fires
        self._enum_worker = obj

    def _refresh(self) -> None:
        """Legacy alias for install-completion refresh."""
        self._start_refresh()

    @Slot(object, object)
    def _on_enum_done(
        self, soapy_devices: list[SdrDeviceInfo], usb_devices: list[SdrDeviceInfo]
    ) -> None:
        """Called on the UI thread when background enumerate completes."""
        self._rescan_btn.setEnabled(True)

        logger.info("SDR dialog refresh: SOAPY_AVAILABLE=%s", SOAPY_AVAILABLE)
        logger.info("SDR dialog: soapy_devices=%s", [(d.driver, d.label) for d in soapy_devices])
        logger.info("SDR dialog: usb_devices=%s", [(d.driver, d.label) for d in usb_devices])

        # Clear device list
        while self._dev_layout.count():
            item = self._dev_layout.takeAt(0)
            if item:
                w = item.widget()
                if w:
                    w.deleteLater()

        all_devices = soapy_devices or usb_devices

        if not all_devices:
            self._dev_layout.addWidget(QLabel(_("No SDR devices detected.")))
        else:
            for dev in all_devices:
                row = QHBoxLayout()
                if dev.driver is not None:
                    icon = "✅"
                    status = _("Ready")
                    color = "#2ecc71"
                else:
                    icon = "❓"
                    status = _("Driver not installed")
                    color = "#e67e22"
                name_lbl = QLabel(f"{icon}  {dev.display_name}")
                status_lbl = QLabel(status)
                status_lbl.setStyleSheet(f"color: {color}; font-weight: bold;")
                row.addWidget(name_lbl)
                row.addStretch()
                row.addWidget(status_lbl)
                w = QWidget()
                w.setLayout(row)
                self._dev_layout.addWidget(w)

        # SoapySDR overall status
        if platform.system() == "Windows":
            # On Windows, RTL-SDR and HackRF bypass SoapySDR via ctypes.
            # SoapySDR is bundled but not actually used for device access.
            self._soapy_status.setText(
                "ℹ️  "
                + _(
                    "SoapySDR is bundled but bypassed on Windows.\n"
                    "RTL-SDR and HackRF communicate directly via ctypes (hackrf.dll / rtlsdr.dll)."
                )
            )
            self._soapy_status.setStyleSheet("color: #3498db;")
        elif SOAPY_AVAILABLE:
            self._soapy_status.setText("✅  " + _("SoapySDR is installed and ready."))
            self._soapy_status.setStyleSheet("color: #2ecc71;")
        else:
            self._soapy_status.setText("❌  " + _("SoapySDR is NOT installed."))
            self._soapy_status.setStyleSheet("color: #e74c3c;")

        # Build install instructions and button visibility
        self._build_install_section(all_devices)

    def _build_install_section(self, devices: list[SdrDeviceInfo]) -> None:
        """Populate the install action area based on OS and detected devices."""
        os_name = platform.system()

        # Collect needed packages
        needed_modules = {d.soapy_module for d in devices if d.driver is None and d.soapy_module}
        if SOAPY_AVAILABLE and not needed_modules:
            self._action_label.setText(
                "✅  " + _("All detected devices have drivers installed. No action needed.")
            )
            self._install_btn.setVisible(False)
            return

        self._install_btn.setVisible(True)

        if os_name == "Linux":
            pkgs: list[str] = []
            if not SOAPY_AVAILABLE:
                pkgs += ["python3-soapysdr"]
            for mod in needed_modules:
                pkgs += _APT_PACKAGES.get(mod, [])
            unique_pkgs = list(dict.fromkeys(pkgs))  # deduplicate preserving order
            self._pending_cmd = ["pkexec", "apt-get", "install", "-y"] + unique_pkgs
            self._action_label.setText(
                _("The following packages will be installed:\n") + "  " + "  ".join(unique_pkgs)
            )
            self._install_btn.setText(_("Install via apt-get (requires password)"))

        elif os_name == "Darwin":
            pkgs = []
            if not SOAPY_AVAILABLE:
                pkgs += ["soapysdr"]
            for mod in needed_modules:
                pkgs += _BREW_PACKAGES.get(mod, [])
            unique_pkgs = list(dict.fromkeys(pkgs))
            if shutil.which("brew"):
                self._pending_cmd = ["brew", "install"] + unique_pkgs
                self._action_label.setText(
                    _("The following Homebrew packages will be installed:\n")
                    + "  "
                    + "  ".join(unique_pkgs)
                )
                self._install_btn.setText(_("Install via Homebrew"))
            else:
                self._pending_cmd = []
                self._action_label.setText(
                    _(
                        "Homebrew is not installed.\n"
                        "Install Homebrew first by running the following in Terminal:\n\n"
                        '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
                    )
                )
                self._install_btn.setVisible(False)

        elif os_name == "Windows":
            self._pending_cmd = []
            self._action_label.setText(
                _(
                    "Windows — Supported SDR Devices\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "✅  RTL-SDR      — Supported  (WinUSB driver required → Zadig)\n"
                    "✅  HackRF One   — Supported  (WinUSB driver required → Zadig)\n"
                    "❌  Airspy / Airspy HF+  — Not supported on Windows\n"
                    "❌  ADALM-Pluto  — Not supported on Windows\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "On Windows, RTL-SDR and HackRF bypass SoapySDR entirely and\n"
                    "communicate directly with the device DLL via ctypes.\n"
                    "SoapySDR on Windows is fundamentally incompatible with WinUSB\n"
                    "drivers and cannot be used reliably — other device types\n"
                    "(Airspy, Airspy HF+, ADALM-Pluto) are therefore not supported.\n\n"
                    "WinUSB Driver Setup — required once for BOTH RTL-SDR and HackRF:\n"
                    "  1. Plug in your device.\n"
                    "  2. Click 'Open Zadig Website' below, download and run Zadig.\n"
                    "  3. In Zadig: Options → List All Devices, select your device\n"
                    "     (RTL-SDR: Bulk-In Interface 0 / HackRF: Hackrf One)\n"
                    "     → set driver to WinUSB → click Install Driver.\n"
                    "  ⚠️  Do NOT select libusbK — it causes device detection failures.\n"
                    "  4. Restart FBSAT59."
                )
            )
            self._install_btn.setVisible(False)
            self._add_windows_buttons()
        else:
            self._pending_cmd = []
            self._action_label.setText(
                _(
                    "Automatic installation is not supported on this OS.\n"
                    "Please install SoapySDR and the appropriate driver module manually."
                )
            )
            self._install_btn.setVisible(False)

    def _add_windows_buttons(self) -> None:
        """Add Zadig website button for WinUSB driver installation (RTL-SDR and HackRF)."""
        zadig_btn = QPushButton(_("Open Zadig Website (WinUSB driver for RTL-SDR / HackRF)"))
        zadig_btn.clicked.connect(lambda: self._open_url(_ZADIG_URL))
        self._status_layout.addWidget(zadig_btn)

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def _on_install(self) -> None:
        cmd = getattr(self, "_pending_cmd", [])
        if not cmd:
            return
        self._install_btn.setEnabled(False)
        self._progress_bar.setVisible(True)
        self._log.clear()
        self._worker = _InstallWorker(cmd)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    @Slot(str)
    def _on_progress(self, line: str) -> None:
        self._log.append(line)

    @Slot(bool, str)
    def _on_finished(self, success: bool, msg: str) -> None:
        self._progress_bar.setVisible(False)
        self._install_btn.setEnabled(True)
        self._log.append(f"\n{'✅' if success else '❌'}  {msg}")
        if success:
            self._refresh()
            self._log.append(_("\n⚠️  Restart FBSAT59 to activate the installed drivers."))

    def _open_url(self, url: str) -> None:
        """Open a URL in the system browser."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))

    def _download_and_run(self, url: str) -> None:
        """Download a direct-link executable and run it (Windows helper)."""
        import tempfile
        import urllib.request
        from pathlib import Path

        try:
            fname = url.split("/")[-1]
            dest = Path(tempfile.gettempdir()) / fname
            self._log.append(f"Downloading {url}…")
            urllib.request.urlretrieve(url, dest)
            self._log.append(f"Launching {dest}…")
            subprocess.Popen([str(dest)], shell=True)
        except Exception as exc:
            self._log.append(f"Error: {exc}")
