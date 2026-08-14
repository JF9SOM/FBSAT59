"""
Rig settings dialog.

RigSettingsDialog — Dialog opened from Radio > Rig Settings.
Three tabs: Rig 1 / Rig 2 / SDR Settings.
Supports Hamlib direct connection and NET (rigctld) connection.

DB keys:
  'rig1_settings' — JSON dict for Rig 1 (always active)
  'rig2_settings' — JSON dict for Rig 2 (has an 'enabled' boolean field)
  'sdr_settings'  — JSON dict for SDR (device args, sample rate, gain, etc.)

Backward compatibility: if 'rig1_settings' is absent but the legacy
'rig_settings' key exists, it is migrated to 'rig1_settings' on first open.
"""

from __future__ import annotations

import contextlib
import glob
import json
import math
import shutil
import subprocess
import sys
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QHideEvent, QPalette, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from i18n import _
from rig.controller import CTCSS_PRESET_TEMPLATES, normalize_civ_addr
from sdr import SOAPY_AVAILABLE
from sdr.device import SdrDeviceInfo
from sdr.ppm_measure import PpmMeasureWorker

# ---------------------------------------------------------------------------
# Hamlib Python binding (imported lazily to avoid Qt TLS collision at startup)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fallback model list (actual Hamlib 4.x model numbers)
# ---------------------------------------------------------------------------
# Model IDs verified against Hamlib 4.7.2 riglist.h (RIG_MAKE_MODEL(backend, n)).
# Hamlib 4.x removed riglist dict, so _load_from_hamlib_api() returns [] and
# this list is always used as the fallback on 4.x installations.
_FALLBACK_MODELS: list[tuple[int, str, str]] = [
    # Hamlib internal
    (1, "Hamlib", "Dummy"),
    (2, "Hamlib", "NET rigctl"),
    (4, "FLRig", "FLRig"),
    # Yaesu  (RIG_YAESU=1, base=1000)
    (1001, "Yaesu", "FT-847"),
    (1010, "Yaesu", "FT-736R"),
    (1020, "Yaesu", "FT-817"),
    (1021, "Yaesu", "FT-100"),
    (1022, "Yaesu", "FT-857"),
    (1023, "Yaesu", "FT-897"),
    (1024, "Yaesu", "FT-1000MP"),
    (1027, "Yaesu", "FT-450"),
    (1028, "Yaesu", "FT-950"),
    (1029, "Yaesu", "FT-2000"),
    (1032, "Yaesu", "FTDX-5000"),
    (1034, "Yaesu", "FTDX-1200"),
    (1035, "Yaesu", "FT-991 / FT-991A / FT-991AM"),
    (1036, "Yaesu", "FT-891"),
    (1037, "Yaesu", "FTDX-3000"),
    (1040, "Yaesu", "FTDX-101D"),
    (1041, "Yaesu", "FT-818"),
    (1042, "Yaesu", "FTDX-10"),
    (1044, "Yaesu", "FTDX-101MP"),
    (1046, "Yaesu", "FT-450D"),
    (1051, "Yaesu", "FTX-1"),
    # Kenwood  (RIG_KENWOOD=2, base=2000)
    (2003, "Kenwood", "TS-450S"),
    (2004, "Kenwood", "TS-570D"),
    (2005, "Kenwood", "TS-690S"),
    (2006, "Kenwood", "TS-711A"),
    (2007, "Kenwood", "TS-790E"),
    (2009, "Kenwood", "TS-850S"),
    (2010, "Kenwood", "TS-870S"),
    (2013, "Kenwood", "TS-950SDX"),
    (2014, "Kenwood", "TS-2000"),
    (2016, "Kenwood", "TS-570S"),
    (2026, "Kenwood", "TM-D700"),
    (2027, "Kenwood", "TM-V7"),
    (2028, "Kenwood", "TS-480"),
    (2029, "Elecraft", "K3"),
    (2031, "Kenwood", "TS-590S"),
    (2034, "Kenwood", "TM-D710"),
    (2037, "Kenwood", "TS-590SG"),
    (2039, "Kenwood", "TS-990S"),
    (2041, "Kenwood", "TS-890S"),
    (2042, "Kenwood", "TH-D74"),
    (2043, "Elecraft", "K3S"),
    (2044, "Elecraft", "KX2"),
    (2045, "Elecraft", "KX3"),
    # Icom  (RIG_ICOM=3, base=3000)
    (3013, "Icom", "IC-718"),
    (3023, "Icom", "IC-746"),
    (3026, "Icom", "IC-756"),
    (3027, "Icom", "IC-756Pro"),
    (3029, "Icom", "IC-765"),
    (3030, "Icom", "IC-775"),
    (3031, "Icom", "IC-781"),
    (3032, "Icom", "IC-820H"),
    (3044, "Icom", "IC-910H"),
    (3046, "Icom", "IC-746Pro"),
    (3047, "Icom", "IC-756ProII"),
    (3055, "Icom", "IC-703"),
    (3056, "Icom", "IC-7800"),
    (3057, "Icom", "IC-756ProIII"),
    (3060, "Icom", "IC-7000"),
    (3061, "Icom", "IC-7200"),
    (3062, "Icom", "IC-7700"),
    (3063, "Icom", "IC-7600"),
    (3067, "Icom", "IC-7410"),
    (3068, "Icom", "IC-9100"),
    (3070, "Icom", "IC-7100"),
    (3073, "Icom", "IC-7300"),
    (3078, "Icom", "IC-7610"),
    (3081, "Icom", "IC-9700"),
    (3085, "Icom", "IC-705"),
    # Alinco  (RIG_ALINCO=17, base=17000)
    (17001, "Alinco", "DX-77"),
    # SDR
    (3000801, "HPSDR", "Apache Labs ANAN-7000DLE MKII"),
]


def _load_from_hamlib_api() -> list[tuple[int, str, str]]:
    """Fetch all supported models from the Hamlib Python binding.

    Uses the riglist dict (Hamlib 3.x API). Hamlib 4.x removed riglist and
    provides no efficient API to enumerate model names without creating a Rig
    instance per model — creating 384+ Rig instances exhausts pthread keys
    (PTHREAD_KEYS_MAX=1024) and crashes Qt via QThreadStorage hash collision.

    Returns:
        List of (model_id, manufacturer, model_name). Empty on failure.
    """
    try:
        import Hamlib as _hamlib_mod  # lazy — avoids Qt TLS collision at startup
    except ModuleNotFoundError:
        return []

    if not hasattr(_hamlib_mod, "riglist"):
        return []  # Hamlib 4.x: fall back to _FALLBACK_MODELS

    models: list[tuple[int, str, str]] = []
    try:
        for model_id, info in _hamlib_mod.riglist.items():
            name = str(getattr(info, "model_name", "") or "").strip()
            mfg = str(getattr(info, "mfg_name", "") or "").strip()
            if name:
                models.append((int(model_id), mfg, name))
    except (AttributeError, TypeError):
        pass
    return models


def _set_placeholder_color(widget: QLineEdit) -> None:
    """Set placeholder text to steel blue via QPalette (theme-safe, no stylesheet)."""
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6B9EC4"))
    widget.setPalette(palette)


def _load_hamlib_models() -> list[tuple[int, str, str]]:
    """Return all supported Hamlib models sorted by manufacturer and model name.

    Priority:
        1. ``riglist`` dictionary from the Hamlib Python binding
        2. Hard-coded fallback list

    Returns:
        List of (model_id, manufacturer, model_name).
    """
    models = _load_from_hamlib_api()
    if not models:
        models = list(_FALLBACK_MODELS)
    return sorted(models, key=lambda x: (x[1].lower(), x[2].lower()))


def _baud_test_type(model_id: int, all_models: list[tuple[int, str, str]]) -> str | None:
    """Return the baud-rate test method for a given Hamlib model ID.

    Returns:
        ``"if"``  — send ``IF;`` (Yaesu / Kenwood / Elecraft CAT protocol)
        ``"civ"`` — send CI-V frequency query (Icom)
        ``None``  — no known test; hide the Test button
    """
    mfg = ""
    for mid, m, _name in all_models:
        if mid == model_id:
            mfg = m.lower()
            break

    if mfg in ("yaesu",):
        return "if"
    if mfg in ("kenwood", "elecraft"):
        return "if"
    if mfg == "icom":
        return "civ"
    # Fallback: guess by model-ID range for rigs not in all_models
    if 1000 <= model_id < 3000:
        return "if"
    if 3000 <= model_id < 4000:
        return "civ"
    return None


_icom_civ_cache: dict[int, str] = {}


def _get_icom_default_civ(model_id: int) -> str:
    """Return the Hamlib default CI-V address for an Icom rig as a hex string.

    Creates a Rig instance without opening the port and reads the compile-time
    default via get_conf("civaddr").  Results are cached so repeated model
    switches don't re-query Hamlib.

    Returns e.g. "60" (without 0x prefix) or "" if unavailable.
    """
    if model_id in _icom_civ_cache:
        return _icom_civ_cache[model_id]
    result = ""
    try:
        import Hamlib as _H  # noqa: N812

        rig = _H.Rig(model_id)
        raw = rig.get_conf("civaddr")
        # Hamlib returns a decimal string (e.g. "96" for 0x60)
        if raw and raw.strip() not in ("", "0"):
            addr_int = int(raw.strip(), 0)
            result = format(addr_int, "X")  # "60", "A2" etc.
    except Exception:
        pass
    _icom_civ_cache[model_id] = result
    return result


def _scan_serial_ports() -> list[str]:
    """Scan for available serial ports and return them. No extra dependencies needed."""
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore[import]

            ports: list[str] = []
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DEVICEMAP\SERIALCOMM",
            )
            i = 0
            while True:
                try:
                    _, port, _ = winreg.EnumValue(key, i)
                    ports.append(str(port))
                    i += 1
                except OSError:
                    break
            return sorted(ports)
        except OSError:
            return []
    else:
        patterns = [
            "/dev/FTX*",
            "/dev/ttyUSB*",
            "/dev/ttyACM*",
            "/dev/ttyS*",
            "/dev/cu.*",
        ]
        found: list[str] = []
        for pattern in patterns:
            found.extend(glob.glob(pattern))
        return sorted(set(found))


# ---------------------------------------------------------------------------
# _RigPanel — reusable settings form for one rig
# ---------------------------------------------------------------------------


class _BaudTestNotifier(QObject):
    """Single-use signal carrier for baud-rate test results.

    Defined at module level so PySide6's meta-object system registers the
    Signal once, avoiding the instability of per-call dynamic QObject classes.
    """

    done = Signal(bool)


class _RigPanel(QWidget):
    """Configuration panel for a single rig.

    Used as a tab page inside RigSettingsDialog.
    Rig 1 is always active; Rig 2 has an "Enable Rig 2" checkbox that
    enables or disables the form below it.
    """

    # Emitted when the SDR radio button is toggled: value is True (SDR selected)
    # or False (Hamlib Direct/NET selected).
    sdr_mode_changed = Signal(bool)

    def __init__(
        self,
        rig_index: int,
        all_models: list[tuple[int, str, str]],
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            rig_index:  1 or 2.  Rig 2 renders an enable checkbox.
            all_models: pre-loaded Hamlib model list shared between both panels.
            parent:     parent widget.
        """
        super().__init__(parent)
        self._rig_index = rig_index
        self._all_models = all_models
        self._enable_cb: QCheckBox | None = None
        self._form_widget: QWidget
        # Rig 1 only: manual Radio Type selector
        self._radio_type_combo: QComboBox | None = None
        # Rig 2 only: split-mode selector (determines radio_type for both rigs)
        self._split_mode_combo: QComboBox | None = None
        self._setup_ui()
        self._on_scan_ports()
        self._on_ctcss_method_changed()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Rig 2 only: enable checkbox lives above the scrollable form
        if self._rig_index == 2:
            self._enable_cb = QCheckBox(_("Enable Rig 2"))
            self._enable_cb.toggled.connect(self._on_enable_toggled)
            outer.addWidget(self._enable_cb)

        # Scroll area so the form remains accessible even in a small dialog
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        # Form container placed inside the scroll area
        self._form_widget = QWidget()
        scroll.setWidget(self._form_widget)
        form = QVBoxLayout(self._form_widget)

        # --- Connection mode ---
        mode_group = QGroupBox(_("Connection Mode"))
        mode_outer = QHBoxLayout(mode_group)
        # Left column: Direct / NET
        left_col = QVBoxLayout()
        self._radio_direct = QRadioButton(_("Direct (Hamlib built-in)"))
        self._radio_net = QRadioButton(_("NET (rigctld compatible)"))
        left_col.addWidget(self._radio_direct)
        left_col.addWidget(self._radio_net)
        mode_outer.addLayout(left_col)
        # Right: SDR button (vertically centred)
        self._radio_sdr = QRadioButton(_("SDR"))
        mode_outer.addStretch()
        mode_outer.addWidget(self._radio_sdr, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._radio_direct.setChecked(True)
        self._radio_direct.toggled.connect(self._on_mode_toggled)
        self._radio_net.toggled.connect(self._on_mode_toggled)
        self._radio_sdr.toggled.connect(self._on_mode_toggled)
        form.addWidget(mode_group)

        # --- Direct connection settings ---
        self._direct_group = QGroupBox(_("Direct Connection Settings"))
        direct_form = QFormLayout(self._direct_group)

        port_row = QWidget()
        port_layout = QHBoxLayout(port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(160)
        self._scan_btn = QPushButton(_("Scan"))
        self._scan_btn.setMaximumWidth(80)
        self._scan_btn.clicked.connect(self._on_scan_ports)
        port_layout.addWidget(self._port_combo)
        port_layout.addWidget(self._scan_btn)
        direct_form.addRow(_("COM Port:"), port_row)

        baud_row = QWidget()
        baud_layout = QHBoxLayout(baud_row)
        baud_layout.setContentsMargins(0, 0, 0, 0)
        self._baud_combo = QComboBox()
        for b in ["4800", "9600", "19200", "38400", "57600", "115200"]:
            self._baud_combo.addItem(b)
        self._baud_combo.setCurrentText("9600")
        self._baud_test_btn = QPushButton(_("Test"))
        self._baud_test_btn.setMaximumWidth(80)
        self._baud_test_btn.clicked.connect(self._on_baud_test)
        baud_layout.addWidget(self._baud_combo)
        baud_layout.addWidget(self._baud_test_btn)
        direct_form.addRow(_("Baud Rate:"), baud_row)

        self._model_search = QLineEdit()
        self._model_search.setPlaceholderText(_("Search by manufacturer or model name..."))
        self._model_search.textChanged.connect(self._on_model_search)
        direct_form.addRow(_("Search:"), self._model_search)

        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(280)
        self._populate_model_combo(self._all_models)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        direct_form.addRow(_("Rig Model:"), self._model_combo)

        self._civ_addr_edit = QLineEdit()
        self._civ_addr_edit.setPlaceholderText(
            _("e.g. 65  (hex as shown on rig menu, blank = default)")
        )
        _set_placeholder_color(self._civ_addr_edit)
        self._civ_addr_edit.setMaximumWidth(160)
        direct_form.addRow(_("CI-V Address (Icom):"), self._civ_addr_edit)

        form.addWidget(self._direct_group)

        # --- NET connection settings ---
        self._net_group = QGroupBox(_("NET Connection Settings"))
        net_form = QFormLayout(self._net_group)
        self._host_edit = QLineEdit("localhost")
        net_form.addRow(_("Host:"), self._host_edit)
        self._net_port_spin = QSpinBox()
        self._net_port_spin.setRange(1, 65535)
        # Rig 1 defaults to rigctld port 4532; Rig 2 defaults to 4533
        self._net_port_spin.setValue(4532 if self._rig_index == 1 else 4533)
        net_form.addRow(_("Port:"), self._net_port_spin)
        form.addWidget(self._net_group)
        self._net_group.setVisible(False)

        # --- Radio Type (Rig 1) / Split Mode (Rig 2) ---
        if self._rig_index == 1:
            # Rig 1 running alone: choose full-duplex / RX-only / TX-only
            type_group = QGroupBox(_("Radio Type"))
            type_form = QFormLayout(type_group)
            self._radio_type_combo = QComboBox()
            self._radio_type_combo.addItem(
                _("Duplex — Main: Downlink (RX) / Sub: Uplink (TX)"), "full_duplex"
            )
            self._radio_type_combo.addItem(_("Simplex — Downlink (RX) only"), "rx_only")
            self._radio_type_combo.addItem(_("Simplex — Uplink (TX) only"), "tx_only")
            type_form.addRow(_("Radio Type:"), self._radio_type_combo)
            form.addWidget(type_group)
        else:
            # Rig 2 enabled: describe how the two rigs share DL/UL duties.
            # The selection automatically sets radio_type for both rigs when saving.
            split_group = QGroupBox(_("Split Mode"))
            split_form = QFormLayout(split_group)
            self._split_mode_combo = QComboBox()
            self._split_mode_combo.addItem(
                _("Rig 1: Downlink (RX only) / Rig 2: Uplink (TX only)"),
                "rig1_dl_rig2_ul",
            )
            self._split_mode_combo.addItem(
                _("Rig 1: Uplink (TX only) / Rig 2: Downlink (RX only)"),
                "rig1_ul_rig2_dl",
            )
            split_form.addRow(_("Split Mode:"), self._split_mode_combo)
            form.addWidget(split_group)

        # --- CTCSS Tone Settings (NET mode only) ---
        ctcss_group = QGroupBox(_("CTCSS Tone Settings (NET Mode)"))
        self._ctcss_group = ctcss_group
        self._ctcss_form = QFormLayout(ctcss_group)
        ctcss_form = self._ctcss_form
        self._icom_satmode_cb = QCheckBox(_("Icom SAT mode rig (IC-9100/9700/910H/821H)"))
        self._icom_satmode_cb.toggled.connect(self._on_icom_satmode_toggled)
        ctcss_form.addRow("", self._icom_satmode_cb)
        self._ctcss_method_combo = QComboBox()
        self._ctcss_method_combo.addItem(_("Hamlib standard"), "hamlib")
        self._ctcss_method_combo.addItem(_("FTX-1 (Custom CAT)"), "ftx1")
        self._ctcss_method_combo.addItem(_("FT-991 (Custom CAT)"), "ft991")
        self._ctcss_method_combo.addItem(_("Custom CAT command"), "custom_cat")
        self._ctcss_method_combo.currentIndexChanged.connect(self._on_ctcss_method_changed)
        ctcss_form.addRow(_("CTCSS Method:"), self._ctcss_method_combo)
        self._ctcss_cat_on_edit = QLineEdit()
        self._ctcss_cat_on_edit.setPlaceholderText(_("e.g. CN1{tone:03d};CT11;"))
        ctcss_form.addRow(_("CAT ON command:"), self._ctcss_cat_on_edit)
        self._ctcss_cat_off_edit = QLineEdit()
        self._ctcss_cat_off_edit.setPlaceholderText(_("e.g. CT10;"))
        ctcss_form.addRow(_("CAT OFF command:"), self._ctcss_cat_off_edit)
        form.addWidget(ctcss_group)
        ctcss_group.setEnabled(False)  # disabled by default; enabled when NET mode is selected

        # Status label (port-scan / model-search results)
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addWidget(self._status_label)
        form.addStretch()

        # Rig 2 starts disabled until the checkbox is checked
        if self._rig_index == 2:
            self._form_widget.setEnabled(False)

    # ------------------------------------------------------------------ #
    # Slot handlers
    # ------------------------------------------------------------------ #

    def _on_enable_toggled(self, checked: bool) -> None:
        """Enable or disable the entire form based on the Rig 2 checkbox."""
        self._form_widget.setEnabled(checked)

    def _on_mode_toggled(self, _checked: bool = False) -> None:
        is_sdr = self._radio_sdr.isChecked()
        is_direct = self._radio_direct.isChecked()
        self._direct_group.setVisible(is_direct and not is_sdr)
        self._net_group.setVisible(not is_direct and not is_sdr)
        # Gray out Hamlib-specific groups when SDR is selected
        for grp in (self._direct_group, self._net_group):
            grp.setEnabled(not is_sdr)
        # CTCSS Method only applies in NET mode; also grey out for SDR.
        # Must be set after the loop above — it previously overwrote this
        # with setEnabled(not is_sdr), silently re-enabling the group
        # whenever SDR wasn't selected (i.e. always enabled in Direct mode).
        self._ctcss_group.setEnabled(not is_direct and not is_sdr)
        self.sdr_mode_changed.emit(is_sdr)

    def _on_scan_ports(self) -> None:
        """Scan serial ports and update the COM port combo box."""
        current = self._port_combo.currentText()
        ports = _scan_serial_ports()
        self._port_combo.clear()
        if ports:
            self._port_combo.addItems(ports)
            self._status_label.setText(_("{n} port(s) found").format(n=len(ports)))
        else:
            self._status_label.setText(_("No serial ports found"))
        if current:
            idx = self._port_combo.findText(current)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
            else:
                self._port_combo.setEditText(current)

    def _on_baud_test(self) -> None:
        """Test the Hamlib Direct baud rate using the appropriate command for the rig."""
        port = self._port_combo.currentText()
        baud = int(self._baud_combo.currentText())
        model_id: int = self._model_combo.currentData() or 0
        test_type = _baud_test_type(model_id, self._all_models) or "if"
        # For CI-V, use address from the CI-V Address field (or 0x00 as broadcast fallback).
        civ_text = self._civ_addr_edit.text().strip()
        try:
            civ_addr = int(normalize_civ_addr(civ_text), 16) if civ_text else 0
        except ValueError:
            self._baud_test_btn.setText(_("Bad address"))
            self._baud_test_btn.setStyleSheet("color: orange;")
            return
        self._run_baud_test(port, baud, self._baud_test_btn, test_type, civ_addr)

    def _run_baud_test(
        self,
        port: str,
        baud: int,
        btn: QPushButton,
        test_type: str = "if",
        civ_addr: int = 0,
    ) -> None:
        """Open *port* at *baud* and verify the rig responds.

        test_type ``"if"``  — send ``IF;`` (Yaesu / Kenwood CAT)
        test_type ``"civ"`` — send CI-V frequency read to *civ_addr*
                              (0x00 = broadcast, responds regardless of address)

        Updates *btn* to green "✓ OK" on success or red "✗ Failed" on timeout.
        "Testing…" state while in-flight prevents double-clicks.
        """
        import threading

        if not port:
            btn.setText(_("No port"))
            btn.setStyleSheet("color: orange;")
            return

        btn.setText(_("Testing…"))
        btn.setEnabled(False)
        btn.setStyleSheet("")

        # Keep notifier on self so it isn't garbage-collected before the
        # background thread fires the signal.
        self._baud_notifier = _BaudTestNotifier()

        def _apply(ok: bool) -> None:
            btn.setEnabled(True)
            if ok:
                btn.setText("✓ OK")
                btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
            else:
                btn.setText("✗ Failed")
                btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold;")

        self._baud_notifier.done.connect(_apply)

        def _test() -> None:
            ok = False
            try:
                import serial  # pyserial

                with serial.Serial(port, baud, timeout=0.4) as ser:
                    ser.reset_input_buffer()
                    if test_type == "civ":
                        # CI-V read frequency command; civ_addr=0x00 acts as broadcast
                        addr = civ_addr & 0xFF
                        frame = bytes([0xFE, 0xFE, addr, 0xE0, 0x03, 0xFD])
                        ser.write(frame)
                        # Expect FE FE prefix in response
                        response = ser.read(20)
                        ok = response[:2] == b"\xfe\xfe"
                    else:
                        ser.write(b"IF;")
                        response = ser.read(50)
                        ok = len(response) > 0
            except Exception:
                ok = False
            self._baud_notifier.done.emit(ok)

        threading.Thread(target=_test, daemon=True).start()

    def _on_icom_satmode_toggled(self, checked: bool) -> None:
        """Lock CTCSS method to Hamlib standard when Icom SAT mode is selected."""
        if checked:
            # Force Hamlib standard and grey out everything below the checkbox
            for i in range(self._ctcss_method_combo.count()):
                if self._ctcss_method_combo.itemData(i) == "hamlib":
                    self._ctcss_method_combo.setCurrentIndex(i)
                    break
        self._ctcss_method_combo.setEnabled(not checked)
        self._ctcss_cat_on_edit.setEnabled(False)
        self._ctcss_cat_off_edit.setEnabled(False)
        if not checked:
            self._on_ctcss_method_changed()

    def _on_ctcss_method_changed(self) -> None:
        """Show/hide CAT fields based on the selected CTCSS method."""
        method = self._ctcss_method_combo.currentData()

        self._ctcss_form.setRowVisible(self._ctcss_cat_on_edit, True)
        self._ctcss_form.setRowVisible(self._ctcss_cat_off_edit, True)

        if method in CTCSS_PRESET_TEMPLATES:
            on_cmd, off_cmd = CTCSS_PRESET_TEMPLATES[method]
            self._ctcss_cat_on_edit.setText(on_cmd)
            self._ctcss_cat_off_edit.setText(off_cmd)
            self._ctcss_cat_on_edit.setEnabled(False)
            self._ctcss_cat_off_edit.setEnabled(False)
        elif method == "custom_cat":
            self._ctcss_cat_on_edit.setEnabled(True)
            self._ctcss_cat_off_edit.setEnabled(True)
        else:  # "hamlib"
            self._ctcss_cat_on_edit.setText("")
            self._ctcss_cat_off_edit.setText("")
            self._ctcss_cat_on_edit.setEnabled(False)
            self._ctcss_cat_off_edit.setEnabled(False)

    def _on_model_changed(self, _index: int) -> None:
        """Enable CI-V Address field only for Icom rigs; show/hide baud Test button."""
        model_id: int = self._model_combo.currentData() or 0
        is_icom = any(
            mid == model_id and mfg.lower() == "icom" for mid, mfg, _name in self._all_models
        )
        self._civ_addr_edit.setEnabled(is_icom)
        if is_icom:
            default = _get_icom_default_civ(model_id)
            if default:
                self._civ_addr_edit.setPlaceholderText(
                    _("default: {addr}  (blank = use default)").format(addr=default)
                )
            else:
                self._civ_addr_edit.setPlaceholderText(
                    _("e.g. 65  (hex as shown on rig menu, blank = default)")
                )
        else:
            self._civ_addr_edit.setPlaceholderText(_("N/A"))

        # Show Test button only for rigs with a known CAT/CI-V test command.
        test_type = _baud_test_type(model_id, self._all_models)
        self._baud_test_btn.setVisible(test_type is not None)
        # Reset button appearance when the model changes.
        self._baud_test_btn.setText(_("Test"))
        self._baud_test_btn.setStyleSheet("")
        self._baud_test_btn.setEnabled(True)

    def _on_model_search(self, text: str) -> None:
        """Filter the Hamlib model list as the user types."""
        query = text.lower().strip()
        if not query:
            self._populate_model_combo(self._all_models)
            self._status_label.setText(
                _("{n} rig models available").format(n=len(self._all_models))
            )
        else:
            filtered = [
                (mid, mfg, name)
                for mid, mfg, name in self._all_models
                if query in mfg.lower() or query in name.lower() or query in str(mid)
            ]
            self._populate_model_combo(filtered)
            if len(filtered) == 1:
                self._model_combo.setCurrentIndex(0)
            self._status_label.setText(
                _("Showing {n} / {total} models").format(
                    n=len(filtered), total=len(self._all_models)
                )
            )

    # ------------------------------------------------------------------ #
    # Model combo helpers
    # ------------------------------------------------------------------ #

    def _populate_model_combo(self, models: list[tuple[int, str, str]]) -> None:
        """Populate the model combo box, preserving the current selection if possible."""
        current_id: int | None = self._model_combo.currentData()
        self._model_combo.clear()
        for mid, mfg, name in models:
            label = f"{mfg} — {name} (#{mid})" if mfg else f"{name} (#{mid})"
            self._model_combo.addItem(label, mid)
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == current_id:
                self._model_combo.setCurrentIndex(i)
                break

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def is_enabled(self) -> bool:
        """Return True when this rig should be activated.

        Rig 1 is always enabled.  Rig 2 is enabled only when its checkbox
        is checked.
        """
        if self._enable_cb is None:
            return True
        return self._enable_cb.isChecked()

    def load(self, s: dict[str, Any]) -> None:
        """Restore form fields from a saved settings dictionary.

        Args:
            s: dict produced by :meth:`save` (may be a legacy ``rig_settings`` dict).
        """
        # Enable checkbox (Rig 2 only)
        if self._enable_cb is not None:
            checked = bool(s.get("enabled", False))
            self._enable_cb.blockSignals(True)
            self._enable_cb.setChecked(checked)
            self._enable_cb.blockSignals(False)
            self._form_widget.setEnabled(checked)

        # Connection mode
        if s.get("mode") == "sdr":
            self._radio_sdr.setChecked(True)
        elif s.get("mode") == "net":
            self._radio_net.setChecked(True)
        else:
            self._radio_direct.setChecked(True)

        # COM port
        port = str(s.get("port", ""))
        if port:
            idx = self._port_combo.findText(port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
            else:
                self._port_combo.setEditText(port)

        # Baud rate
        baud = str(s.get("baud_rate", 9600))
        idx = self._baud_combo.findText(baud)
        if idx >= 0:
            self._baud_combo.setCurrentIndex(idx)

        # Rig model
        model_id = int(s.get("model_id", 1))
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == model_id:
                self._model_combo.setCurrentIndex(i)
                break

        # NET settings
        self._host_edit.setText(str(s.get("host", "localhost")))
        self._net_port_spin.setValue(int(s.get("net_port", 4532 if self._rig_index == 1 else 4533)))

        # Radio Type (Rig 1) or Split Mode (Rig 2)
        if self._rig_index == 1 and self._radio_type_combo is not None:
            radio_type = str(s.get("radio_type", "full_duplex"))
            for i in range(self._radio_type_combo.count()):
                if self._radio_type_combo.itemData(i) == radio_type:
                    self._radio_type_combo.setCurrentIndex(i)
                    break
        elif self._rig_index == 2 and self._split_mode_combo is not None:
            split_mode = str(s.get("split_mode", "rig1_dl_rig2_ul"))
            for i in range(self._split_mode_combo.count()):
                if self._split_mode_combo.itemData(i) == split_mode:
                    self._split_mode_combo.setCurrentIndex(i)
                    break

        # CTCSS — migrate legacy "icom_civ" method to "hamlib" + satmode checkbox
        ctcss_method = str(s.get("ctcss_method", "hamlib"))
        if ctcss_method == "icom_civ":
            ctcss_method = "hamlib"
            self._icom_satmode_cb.setChecked(True)
        for i in range(self._ctcss_method_combo.count()):
            if self._ctcss_method_combo.itemData(i) == ctcss_method:
                self._ctcss_method_combo.setCurrentIndex(i)
                break
        self._ctcss_cat_on_edit.setText(str(s.get("ctcss_cat_on", "")))
        self._ctcss_cat_off_edit.setText(str(s.get("ctcss_cat_off", "")))
        self._icom_satmode_cb.setChecked(bool(s.get("icom_satmode_rig", False)))

        self._civ_addr_edit.setText(str(s.get("civ_addr", "")))
        self._on_ctcss_method_changed()
        self._on_model_changed(0)

    def save(self) -> dict[str, Any]:
        """Return the current form state as a settings dictionary.

        Returns:
            dict with all rig parameters.  Rig 2 dicts include an ``'enabled'``
            key set to the checkbox state.
        """
        model_id: int = self._model_combo.currentData() or 1
        if self._radio_sdr.isChecked():
            _mode = "sdr"
        elif self._radio_net.isChecked():
            _mode = "net"
        else:
            _mode = "direct"
        s: dict[str, Any] = {
            "mode": _mode,
            "port": self._port_combo.currentText(),
            "baud_rate": int(self._baud_combo.currentText()),
            "model_id": model_id,
            "host": self._host_edit.text(),
            "net_port": self._net_port_spin.value(),
            "civ_addr": self._civ_addr_edit.text().strip(),
            "ctcss_method": self._ctcss_method_combo.currentData() or "hamlib",
            "ctcss_cat_on": self._ctcss_cat_on_edit.text(),
            "ctcss_cat_off": self._ctcss_cat_off_edit.text(),
            "icom_satmode_rig": self._icom_satmode_cb.isChecked(),
        }
        # Rig 1: store its own radio_type (used when Rig 2 is disabled)
        if self._rig_index == 1 and self._radio_type_combo is not None:
            s["radio_type"] = self._radio_type_combo.currentData() or "full_duplex"
        # Rig 2: store split_mode; radio_type is derived by RigSettingsDialog._save_settings()
        if self._rig_index == 2 and self._split_mode_combo is not None:
            s["split_mode"] = self._split_mode_combo.currentData() or "rig1_dl_rig2_ul"
        if self._enable_cb is not None:
            s["enabled"] = self._enable_cb.isChecked()
        return s


class _AddRemoteHostDialog(QDialog):
    """Dialog for connecting to a SoapyRemote server ("Add Remote Host…").

    Lets the user manually specify a SoapySDRServer host/port when LAN
    broadcast auto-discovery doesn't reach it (e.g. a receiver on a separate
    network segment or reachable only over VPN).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Add Remote Host"))
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText(_("e.g. 192.168.1.50 or shed.local"))
        form.addRow(_("Host:"), self._host_edit)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(55132)  # SoapySDRServer default
        form.addRow(_("Port:"), self._port_spin)

        self._driver_edit = QLineEdit()
        self._driver_edit.setPlaceholderText(_("optional, e.g. rtlsdr — auto-detect if blank"))
        form.addRow(_("Remote driver:"), self._driver_edit)

        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText(_("optional display name"))
        form.addRow(_("Label:"), self._label_edit)

        layout.addLayout(form)

        info = QLabel(
            _(
                "The remote machine must be running SoapySDRServer (SoapyRemote)\n"
                "with the SDR's own SoapySDR module installed."
            )
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _on_accept(self) -> None:
        if not self._host_edit.text().strip():
            self._host_edit.setFocus()
            return
        self.accept()

    def result_entry(self) -> dict[str, str]:
        """Return the entered host as a plain dict suitable for persistence."""
        return {
            "host": self._host_edit.text().strip(),
            "port": str(self._port_spin.value()),
            "driver_hint": self._driver_edit.text().strip(),
            "label": self._label_edit.text().strip(),
        }


# ---------------------------------------------------------------------------
# RigSettingsDialog
# ---------------------------------------------------------------------------


class _SdrSettingsPanel(QWidget):
    """SDR Settings tab panel.

    Allows the user to enumerate SoapySDR devices, configure the selected
    device, and assign it to Rig 1 or Rig 2.

    When SoapySDR is not installed, the panel shows an install prompt instead.
    """

    # Emitted when the assigned rig slot changes: value is 1, 2, or None.
    assigned_rig_changed = Signal(object)
    # Emitted from background thread when enumerate() completes
    _enumerate_done = Signal(object)

    # Sample rates offered in the dropdown (Hz)
    _SAMPLE_RATES: list[tuple[str, float]] = [
        ("250 kHz", 250_000),
        ("1.0 MHz", 1_000_000),
        ("1.4 MHz", 1_400_000),
        ("1.8 MHz", 1_800_000),
        ("2.0 MHz", 2_000_000),
        ("2.4 MHz", 2_400_000),
        ("3.2 MHz", 3_200_000),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._devices: list[SdrDeviceInfo] = []
        # Devices returned by SdrDevice.enumerate() (real hardware / LAN-
        # broadcast-discovered SoapyRemote servers only). self._devices is
        # rebuilt from this plus self._remote_hosts on every change.
        self._hw_devices: list[SdrDeviceInfo] = []
        self._enum_running: bool = False
        # Manually-added SoapyRemote hosts (see "Add Remote Host…"), persisted
        # as part of sdr_settings so they survive restarts even when the
        # remote server isn't reachable via LAN broadcast discovery.
        self._remote_hosts: list[dict[str, str]] = []
        self._enumerate_done.connect(self._on_enumerate)
        self._ppm_worker: PpmMeasureWorker | None = None
        self._ppm_progress: QProgressDialog | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        if not SOAPY_AVAILABLE:
            msg = QLabel(
                _(  # noqa: F823
                    "SoapySDR is not installed.\n"
                    "Use Help > SDR Device Installation to set up your device."
                )
            )
            msg.setWordWrap(True)
            msg.setStyleSheet("color: orange; font-weight: bold;")
            layout.addWidget(msg)
            layout.addStretch()
            return

        # -- Device selection row --
        dev_group = QGroupBox(_("SDR Device"))
        dev_form = self._dev_form = QFormLayout(dev_group)

        dev_row = QHBoxLayout()
        self._dev_combo = QComboBox()
        self._dev_combo.setMinimumWidth(260)
        # Connected once, for the lifetime of the panel. _rebuild_combo() uses
        # blockSignals() around clear()/repopulate instead of disconnecting and
        # reconnecting this on every call — disconnecting a bound method is
        # unreliable in PySide6 (silently fails, see _rebuild_combo), and
        # repeated failed-disconnect + reconnect cycles pile up duplicate
        # connections that can crash the interpreter at shutdown.
        self._dev_combo.currentIndexChanged.connect(self._on_device_selected)
        self._enum_btn = QPushButton(_("Enumerate"))
        self._enum_btn.clicked.connect(lambda: self._start_enumerate(force=True))
        dev_row.addWidget(self._dev_combo)
        dev_row.addWidget(self._enum_btn)
        dev_form.addRow(_("Device:"), dev_row)

        remote_row = QHBoxLayout()
        self._add_remote_btn = QPushButton(_("Add Remote Host…"))
        self._add_remote_btn.setToolTip(
            _(
                "Connect to an SDR on another machine running SoapySDRServer\n"
                "(SoapyRemote), e.g. a receiver in a separate location."
            )
        )
        self._add_remote_btn.clicked.connect(self._on_add_remote_host)
        self._remove_remote_btn = QPushButton(_("Remove"))
        self._remove_remote_btn.setEnabled(False)
        self._remove_remote_btn.clicked.connect(self._on_remove_remote_host)
        remote_row.addWidget(self._add_remote_btn)
        remote_row.addWidget(self._remove_remote_btn)
        remote_row.addStretch()
        dev_form.addRow("", remote_row)

        self._driver_label = QLabel("—")
        dev_form.addRow(_("Driver:"), self._driver_label)

        self._serial_label = QLabel("—")
        dev_form.addRow(_("Serial:"), self._serial_label)
        # On Windows the row starts hidden and is revealed only for devices
        # that actually report a serial — see _update_serial_row().
        self._update_serial_row("")

        layout.addWidget(dev_group)

        # -- Configuration --
        cfg_group = QGroupBox(_("Configuration"))
        cfg_form = QFormLayout(cfg_group)

        self._rate_combo = QComboBox()
        for label, _hz in self._SAMPLE_RATES:
            self._rate_combo.addItem(label)
        self._rate_combo.setCurrentIndex(5)  # default 2.4 MHz
        cfg_form.addRow(_("Sample Rate:"), self._rate_combo)

        ppm_row = QHBoxLayout()
        self._ppm_spin = QSpinBox()
        self._ppm_spin.setRange(-200, 200)
        self._ppm_spin.setValue(0)
        self._ppm_spin.setSuffix(" ppm")
        ppm_row.addWidget(self._ppm_spin)
        self._ppm_measure_btn = QPushButton(_("Measure…"))
        self._ppm_measure_btn.setToolTip(
            _(
                "Automatically estimate the device's clock drift by comparing\n"
                "actual samples received against the configured sample rate over\n"
                "~35 seconds. No reference signal needed — same principle as the\n"
                "standard rtl_test -p tool, but built in and works for any device."
            )
        )
        self._ppm_measure_btn.clicked.connect(self._on_measure_ppm)
        ppm_row.addWidget(self._ppm_measure_btn)
        cfg_form.addRow(_("PPM Correction:"), ppm_row)

        gain_row = QHBoxLayout()
        self._gain_auto_rb = QRadioButton(_("Auto"))
        self._gain_manual_rb = QRadioButton(_("Manual"))
        self._gain_auto_rb.setChecked(True)
        self._gain_spin = QSpinBox()
        self._gain_spin.setRange(0, 80)
        self._gain_spin.setValue(40)
        self._gain_spin.setSuffix(" dB")
        self._gain_spin.setEnabled(False)
        self._gain_auto_rb.toggled.connect(lambda on: self._gain_spin.setDisabled(on))
        gain_row.addWidget(self._gain_auto_rb)
        gain_row.addWidget(self._gain_manual_rb)
        gain_row.addWidget(self._gain_spin)
        cfg_form.addRow(_("RF Gain:"), gain_row)

        self._bias_tee_chk = QCheckBox(_("Enable Bias-T (powers external LNA via antenna port)"))
        cfg_form.addRow("", self._bias_tee_chk)

        layout.addWidget(cfg_group)

        # -- Rig slot assignment --
        assign_group = QGroupBox(_("Assign as"))
        assign_layout = QHBoxLayout(assign_group)
        self._rig1_rb = QRadioButton(_("Rig 1"))
        self._rig2_rb = QRadioButton(_("Rig 2"))
        self._rig_none_rb = QRadioButton(_("Not assigned"))
        self._rig_none_rb.setChecked(True)
        self._rig1_rb.toggled.connect(self._on_assignment_changed)
        self._rig2_rb.toggled.connect(self._on_assignment_changed)
        self._rig_none_rb.toggled.connect(self._on_assignment_changed)
        assign_layout.addWidget(self._rig1_rb)
        assign_layout.addWidget(self._rig2_rb)
        assign_layout.addWidget(self._rig_none_rb)
        layout.addWidget(assign_group)

        # -- IQ save directory --
        iq_group = QGroupBox(_("IQ Recording"))
        iq_form = QFormLayout(iq_group)
        iq_row = QHBoxLayout()
        self._iq_dir_edit = QLineEdit()
        self._iq_dir_edit.setPlaceholderText(str(QWidget().fontMetrics()))  # overwritten below
        self._iq_dir_edit.setText(str(__import__("pathlib").Path.home() / "iq_recordings"))
        iq_browse_btn = QPushButton(_("Browse…"))
        iq_browse_btn.clicked.connect(self._on_browse_iq_dir)
        iq_row.addWidget(self._iq_dir_edit)
        iq_row.addWidget(iq_browse_btn)
        iq_form.addRow(_("Save directory:"), iq_row)
        layout.addWidget(iq_group)

        layout.addStretch()

        # Enumerate on first show (in background to avoid UI freeze on Windows)
        self._start_enumerate()

    # ------------------------------------------------------------------ #

    def _start_enumerate(self, force: bool = False) -> None:
        """Run SdrDevice.enumerate() in a background thread to avoid UI freeze.

        force=True bypasses the process-level cache (used when the user
        explicitly clicks the Enumerate button after plugging in a new device).
        """
        if not SOAPY_AVAILABLE:
            return
        if self._enum_running:
            return
        self._enum_running = True
        if hasattr(self, "_enum_btn"):
            self._enum_btn.setEnabled(False)

        import threading

        def _run() -> None:
            try:
                from sdr.device import SdrDevice

                devices = SdrDevice.enumerate(force=force)
            except Exception:
                devices = []
            # Signal delivers result back to the UI thread via Qt event loop
            self._enumerate_done.emit(devices)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _on_enumerate(self, devices: list[SdrDeviceInfo] | None = None) -> None:
        self._enum_running = False
        if hasattr(self, "_enum_btn"):
            self._enum_btn.setEnabled(True)

        if devices is not None:
            self._hw_devices = devices
        if not SOAPY_AVAILABLE and devices is None:
            return
        self._rebuild_combo()

    def _rebuild_combo(self) -> None:
        """Recompute self._devices (hardware + saved remote hosts) and repopulate the combo."""
        self._devices = list(self._hw_devices) + [
            self._remote_host_info(h) for h in self._remote_hosts
        ]

        if not hasattr(self, "_dev_combo"):
            return

        # Block signals instead of disconnect/reconnect around clear()+repopulate:
        # disconnecting a bound method is unreliable in PySide6 (see the
        # currentIndexChanged.connect() call in _setup_ui()), and clear() would
        # otherwise fire spurious currentIndexChanged events mid-rebuild.
        self._dev_combo.blockSignals(True)
        self._dev_combo.clear()
        if not self._devices:
            self._dev_combo.addItem(_("(no devices found)"))
        else:
            for d in self._devices:
                self._dev_combo.addItem(d.display_name)
        self._dev_combo.blockSignals(False)

        if not self._devices:
            self._driver_label.setText("—")
            self._serial_label.setText("—")
            self._update_serial_row("")
            if hasattr(self, "_remove_remote_btn"):
                self._remove_remote_btn.setEnabled(False)
        else:
            self._on_device_selected(0)

    def _update_serial_row(self, serial: str) -> None:
        """Show or hide the Serial row for the selected device (Windows only).

        Windows RTL-SDR and HackRF bypass SoapySDR::Device::make() and drive
        the DLL through ctypes, and the patched findRTLSDR behind the WinUSB
        fix returns a bare device_index=0 entry without querying the dongle
        (see SdrDevice._win_filter_rtlsdr_by_count), so no serial ever
        reaches SdrDeviceInfo.  The resulting permanent "Serial: —" reads as
        a malfunction and has been reported as one.  A SoapyRemote device
        does carry the serial forwarded from the server, though, so key the
        row on whether a value actually arrived rather than on the driver.

        Other platforms always show the row, blank or not, as before.
        """
        if sys.platform != "win32":
            return
        self._dev_form.setRowVisible(self._serial_label, bool(serial))

    def _on_device_selected(self, idx: int) -> None:
        if not self._devices or idx < 0 or idx >= len(self._devices):
            return
        d = self._devices[idx]
        self._driver_label.setText(d.driver or "—")
        self._serial_label.setText(d.serial or "—")
        self._update_serial_row(d.serial or "")
        if hasattr(self, "_remove_remote_btn"):
            is_saved_remote = any(
                self._remote_host_info(h).args == d.args for h in self._remote_hosts
            )
            self._remove_remote_btn.setEnabled(is_saved_remote)

    # ------------------------------------------------------------------ #
    # SoapyRemote (remote SDR host) management
    # ------------------------------------------------------------------ #

    @staticmethod
    def _remote_host_info(entry: dict[str, str]) -> SdrDeviceInfo:
        """Build an SdrDeviceInfo for a manually-saved SoapyRemote host entry."""
        host = entry.get("host", "")
        port = entry.get("port", "") or "55132"
        driver_hint = entry.get("driver_hint", "")
        label = entry.get("label") or _("Remote: {host}").format(host=host)
        args: dict[str, str] = {"driver": "remote", "remote": f"{host}:{port}"}
        if driver_hint:
            args["remote:driver"] = driver_hint
        return SdrDeviceInfo(driver="remote", label=label, serial="", hardware="", args=args)

    def _on_add_remote_host(self) -> None:
        dlg = _AddRemoteHostDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._remote_hosts.append(dlg.result_entry())
        self._rebuild_combo()
        if hasattr(self, "_dev_combo") and self._dev_combo.count() > 0:
            self._dev_combo.setCurrentIndex(self._dev_combo.count() - 1)

    def _on_remove_remote_host(self) -> None:
        idx = self._dev_combo.currentIndex()
        if not (0 <= idx < len(self._devices)):
            return
        d = self._devices[idx]
        if d.driver != "remote":
            return
        self._remote_hosts = [
            h for h in self._remote_hosts if self._remote_host_info(h).args != d.args
        ]
        self._rebuild_combo()

    def _on_assignment_changed(self, _checked: bool = False) -> None:
        """Emit assigned_rig_changed whenever the rig-slot radio buttons change."""
        if not hasattr(self, "_rig1_rb"):
            return
        if self._rig1_rb.isChecked():
            self.assigned_rig_changed.emit(1)
        elif self._rig2_rb.isChecked():
            self.assigned_rig_changed.emit(2)
        else:
            self.assigned_rig_changed.emit(None)

    def set_assigned_rig(self, rig: int | None) -> None:
        """Set the assigned rig slot programmatically (without triggering loops).

        Called by RigSettingsDialog when the user toggles the SDR button on a
        Rig tab so that this panel stays in sync.
        """
        if not hasattr(self, "_rig1_rb"):
            return
        for rb in (self._rig1_rb, self._rig2_rb, self._rig_none_rb):
            rb.blockSignals(True)
        if rig == 1:
            self._rig1_rb.setChecked(True)
        elif rig == 2:
            self._rig2_rb.setChecked(True)
        else:
            self._rig_none_rb.setChecked(True)
        for rb in (self._rig1_rb, self._rig2_rb, self._rig_none_rb):
            rb.blockSignals(False)

    def _on_measure_ppm(self) -> None:
        """Run PpmMeasureWorker against the currently selected device/rate."""
        idx = self._dev_combo.currentIndex()
        if not self._devices or not (0 <= idx < len(self._devices)):
            QMessageBox.warning(self, _("Measure PPM"), _("Select an SDR device first."))
            return

        info = self._devices[idx]
        rate_idx = self._rate_combo.currentIndex()
        rate_hz = (
            self._SAMPLE_RATES[rate_idx][1]
            if 0 <= rate_idx < len(self._SAMPLE_RATES)
            else 2_400_000
        )

        duration_s = 30.0
        self._ppm_measure_btn.setEnabled(False)
        progress = QProgressDialog(
            _("Measuring clock drift ({sec:.0f}s)…").format(sec=duration_s),
            _("Cancel"),
            0,
            100,
            self,
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.canceled.connect(self._on_cancel_measure_ppm)
        self._ppm_progress = progress

        worker = PpmMeasureWorker(info, rate_hz, duration_s=duration_s, parent=self)
        worker.progress.connect(self._on_ppm_measure_progress)
        worker.finished_ok.connect(self._on_ppm_measure_ok)
        worker.finished_err.connect(self._on_ppm_measure_err)
        self._ppm_worker = worker
        worker.start()

    def _on_cancel_measure_ppm(self) -> None:
        if self._ppm_worker is not None:
            self._ppm_worker.requestInterruption()

    def _on_ppm_measure_progress(self, fraction: float) -> None:
        if self._ppm_progress is not None:
            self._ppm_progress.setValue(int(fraction * 100))

    def _on_ppm_measure_ok(self, ppm: float) -> None:
        self._ppm_measure_btn.setEnabled(True)
        if self._ppm_progress is not None:
            self._ppm_progress.close()
            self._ppm_progress = None
        self._ppm_spin.setValue(round(ppm))
        QMessageBox.information(
            self,
            _("Measure PPM"),
            _("Measured clock drift: {ppm:.1f} ppm (set to {rounded}).").format(
                ppm=ppm, rounded=round(ppm)
            ),
        )

    def _on_ppm_measure_err(self, message: str) -> None:
        self._ppm_measure_btn.setEnabled(True)
        if self._ppm_progress is not None:
            self._ppm_progress.close()
            self._ppm_progress = None
        QMessageBox.warning(self, _("Measure PPM"), message)

    def _on_browse_iq_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(
            self, _("Select IQ Recording Directory"), self._iq_dir_edit.text()
        )
        if path:
            self._iq_dir_edit.setText(path)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def save(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of current settings."""
        if not SOAPY_AVAILABLE or not hasattr(self, "_dev_combo"):
            return {"enabled": False}

        idx = self._dev_combo.currentIndex()
        device_args: dict[str, str] = {}
        if self._devices and 0 <= idx < len(self._devices):
            device_args = dict(self._devices[idx].args)

        rate_idx = self._rate_combo.currentIndex() if hasattr(self, "_rate_combo") else 5
        rate_hz = (
            self._SAMPLE_RATES[rate_idx][1]
            if 0 <= rate_idx < len(self._SAMPLE_RATES)
            else 2_400_000
        )

        assigned: int | None = None
        if hasattr(self, "_rig1_rb") and self._rig1_rb.isChecked():
            assigned = 1
        elif hasattr(self, "_rig2_rb") and self._rig2_rb.isChecked():
            assigned = 2

        return {
            "enabled": assigned is not None,
            "assigned_rig": assigned,
            "device_args": device_args,
            "device_label": self._dev_combo.currentText(),
            "sample_rate_hz": rate_hz,
            "ppm": self._ppm_spin.value() if hasattr(self, "_ppm_spin") else 0,
            "gain_auto": self._gain_auto_rb.isChecked() if hasattr(self, "_gain_auto_rb") else True,
            "gain_db": self._gain_spin.value() if hasattr(self, "_gain_spin") else 40,
            "bias_tee": self._bias_tee_chk.isChecked() if hasattr(self, "_bias_tee_chk") else False,
            "iq_save_dir": self._iq_dir_edit.text() if hasattr(self, "_iq_dir_edit") else "",
            "remote_hosts": self._remote_hosts,
        }

    def load(self, data: dict[str, object]) -> None:
        """Restore settings from a previously saved dict."""
        if not SOAPY_AVAILABLE or not hasattr(self, "_dev_combo"):
            return

        rate_hz = float(data.get("sample_rate_hz") or 2_400_000)  # type: ignore[arg-type]
        for i, (_lbl, r) in enumerate(self._SAMPLE_RATES):
            if abs(r - rate_hz) < 1:
                self._rate_combo.setCurrentIndex(i)
                break

        self._ppm_spin.setValue(int(data.get("ppm") or 0))  # type: ignore[call-overload]

        gain_auto = bool(data.get("gain_auto", True))
        self._gain_auto_rb.setChecked(gain_auto)
        self._gain_spin.setValue(int(data.get("gain_db") or 40))  # type: ignore[call-overload]
        self._bias_tee_chk.setChecked(bool(data.get("bias_tee", False)))

        assigned = data.get("assigned_rig")
        if assigned == 1:
            self._rig1_rb.setChecked(True)
        elif assigned == 2:
            self._rig2_rb.setChecked(True)
        else:
            self._rig_none_rb.setChecked(True)

        iq_dir = str(data.get("iq_save_dir", ""))
        if iq_dir:
            self._iq_dir_edit.setText(iq_dir)

        raw_remote_hosts = data.get("remote_hosts") or []
        if isinstance(raw_remote_hosts, list):
            self._remote_hosts = [dict(h) for h in raw_remote_hosts if isinstance(h, dict)]
            self._rebuild_combo()


def _list_pactl_targets(kind: str) -> list[tuple[str, str]]:
    """Enumerate PipeWire/PulseAudio sinks or sources as (name, description).

    `kind` is "sinks" or "sources". Returns [] on non-Linux, when `pactl`
    is unavailable, or on any error. Monitor sources (".monitor" — a mirror
    of what a sink is playing, not a real capture device) are excluded.
    """
    if sys.platform != "linux" or shutil.which("pactl") is None:
        return []
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", kind],
            capture_output=True,
            text=True,
            timeout=3,
        )
        items = json.loads(result.stdout)
        out: list[tuple[str, str]] = []
        for item in items:
            name = item.get("name", "")
            if not name or name.endswith(".monitor"):
                continue
            out.append((name, item.get("description", name)))
        return out
    except Exception:
        return []


class _SoundCardPanel(QWidget):
    """Sound Card tab panel (4th tab in Rig Settings).

    Configures audio input/output devices for Communications features
    (APRS, Telemetry, future FT4/SSTV).  Uses :mod:`sounddevice` to
    enumerate host audio devices; falls back gracefully when the library
    is not installed.

    On Linux, also offers an explicit PipeWire sink/source pin (see
    ``src/comms/audio_device_manager.py`` module docstring) since the
    generic ``pipewire`` ALSA device otherwise silently follows whatever
    PipeWire currently considers the default — which changes when other USB
    audio devices are plugged in or removed.

    DB key written on OK: ``soundcard_settings`` — JSON dict.
    """

    #: Emitted from the shared-input audio callback thread with the current
    #: chunk's peak level in dBFS; Qt auto-queues this to the UI thread.
    level_updated = Signal(float)

    _METER_OWNER = "RigSettings/Meter"
    _METER_SAMPLE_RATE = 8_000  # plenty for a level meter, keeps resampling cheap
    _METER_MIN_INTERVAL_S = 0.05  # ~20fps UI update cap

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._meter_active = False
        self._last_meter_emit = 0.0
        self._setup_ui()
        self.level_updated.connect(self._on_level_updated)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        try:
            import sounddevice as sd

            self._sd = sd
            self._sd_available = True
        except ImportError:
            self._sd_available = False

        if not self._sd_available:
            msg = QLabel(
                _("sounddevice is not installed.\nInstall it with:  pip install sounddevice")
            )
            msg.setWordWrap(True)
            msg.setStyleSheet("color: orange; font-weight: bold;")
            layout.addWidget(msg)
            layout.addStretch()
            return

        # -- Device selection group --
        dev_group = QGroupBox(_("Audio Devices"))
        dev_form = QFormLayout(dev_group)

        # Input device row
        in_row = QHBoxLayout()
        self._in_combo = QComboBox()
        self._in_combo.setMinimumWidth(280)
        in_row.addWidget(self._in_combo)
        dev_form.addRow(_("Input device:"), in_row)
        self._in_combo.currentIndexChanged.connect(self._on_input_device_changed)

        # RX level meter — live while this tab/dialog is visible
        level_row = QHBoxLayout()
        self._level_bar = QProgressBar()
        self._level_bar.setRange(0, 100)
        self._level_bar.setValue(0)
        self._level_bar.setTextVisible(False)
        self._level_bar.setFixedHeight(14)
        level_row.addWidget(self._level_bar)
        self._level_label = QLabel(_("-- dBFS"))
        self._level_label.setMinimumWidth(70)
        level_row.addWidget(self._level_label)
        dev_form.addRow(_("RX Level:"), level_row)

        # Output device row
        out_row = QHBoxLayout()
        self._out_combo = QComboBox()
        self._out_combo.setMinimumWidth(280)
        out_row.addWidget(self._out_combo)
        dev_form.addRow(_("Output device:"), out_row)

        # Enumerate / Test row
        btn_row = QHBoxLayout()
        self._enum_btn = QPushButton(_("Refresh Devices"))
        self._enum_btn.clicked.connect(self._on_enumerate)
        btn_row.addWidget(self._enum_btn)
        self._test_btn = QPushButton(_("Test (loopback)"))
        self._test_btn.clicked.connect(self._on_test)
        btn_row.addWidget(self._test_btn)
        btn_row.addStretch()
        dev_form.addRow("", btn_row)

        layout.addWidget(dev_group)

        # -- Linux/PipeWire explicit pin group (hidden elsewhere / when pactl
        #    is unavailable) --
        self._pactl_ok = sys.platform == "linux" and shutil.which("pactl") is not None
        self._pin_in_combo: QComboBox | None = None
        self._pin_out_combo: QComboBox | None = None
        if self._pactl_ok:
            pin_group = QGroupBox(_("Pin to Device (Linux)"))
            pin_form = QFormLayout(pin_group)

            pin_note = QLabel(
                _(
                    "The devices above follow PipeWire's current default, which can "
                    "silently change when other USB audio devices are plugged in. "
                    "Pick a specific device below to always route to it."
                )
            )
            pin_note.setWordWrap(True)
            pin_note.setStyleSheet("color: #aaa;")
            pin_form.addRow(pin_note)

            self._pin_in_combo = QComboBox()
            self._pin_in_combo.setMinimumWidth(280)
            pin_form.addRow(_("Pin input to:"), self._pin_in_combo)

            self._pin_out_combo = QComboBox()
            self._pin_out_combo.setMinimumWidth(280)
            pin_form.addRow(_("Pin output to:"), self._pin_out_combo)

            layout.addWidget(pin_group)

        # -- Sample rate (fixed at 48000 for Direwolf compatibility) --
        rate_group = QGroupBox(_("Sample Rate"))
        rate_form = QFormLayout(rate_group)
        rate_label = QLabel("48000 Hz  " + _("(fixed — required by Direwolf)"))
        rate_label.setStyleSheet("color: #aaa;")
        rate_form.addRow(_("Rate:"), rate_label)
        layout.addWidget(rate_group)

        # -- Status row --
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        layout.addStretch()

        # Populate on first open
        self._on_enumerate()

    # ------------------------------------------------------------------ #
    # Slots
    # ------------------------------------------------------------------ #

    def _on_enumerate(self) -> None:
        """Refresh input/output device lists from sounddevice."""
        if not self._sd_available:
            return

        try:
            devices = self._sd.query_devices()
        except Exception as exc:  # noqa: BLE001
            self._status_label.setText(_("Failed to query devices: {e}").format(e=exc))
            self._status_label.setStyleSheet("color: orange;")
            return

        default_in = self._sd.default.device[0]
        default_out = self._sd.default.device[1]

        self._in_combo.clear()
        self._out_combo.clear()

        in_default_idx = 0
        out_default_idx = 0

        for idx, dev in enumerate(devices):
            name = f"{idx}: {dev['name']}"
            if dev["max_input_channels"] > 0:
                self._in_combo.addItem(name, idx)
                if idx == default_in:
                    in_default_idx = self._in_combo.count() - 1
            if dev["max_output_channels"] > 0:
                self._out_combo.addItem(name, idx)
                if idx == default_out:
                    out_default_idx = self._out_combo.count() - 1

        self._in_combo.setCurrentIndex(in_default_idx)
        self._out_combo.setCurrentIndex(out_default_idx)
        self._status_label.setText(_("{n} devices found.").format(n=len(devices)))
        self._status_label.setStyleSheet("color: #7bed9f;")

        if self._pactl_ok:
            self._populate_pin_combo(self._pin_in_combo, "sources")
            self._populate_pin_combo(self._pin_out_combo, "sinks")

    @staticmethod
    def _populate_pin_combo(combo: QComboBox | None, kind: str) -> None:
        if combo is None:
            return
        current = combo.currentData()
        combo.clear()
        combo.addItem(_("Auto (follow PipeWire default)"), None)
        for name, description in _list_pactl_targets(kind):
            combo.addItem(description, name)
        if current is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == current:
                    combo.setCurrentIndex(i)
                    break

    # ------------------------------------------------------------------ #
    # RX level meter
    # ------------------------------------------------------------------ #

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._start_meter()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._stop_meter()

    def _on_input_device_changed(self) -> None:
        if self._meter_active:
            self._stop_meter()
            self._start_meter()

    def _start_meter(self) -> None:
        if not self._sd_available or self._meter_active:
            return
        try:
            from comms.audio_device_manager import get_audio_device_manager

            device = self._in_combo.currentData()
            get_audio_device_manager().acquire_input(
                self._METER_OWNER, device, self._METER_SAMPLE_RATE, self._on_audio_chunk
            )
            self._meter_active = True
        except Exception:  # noqa: BLE001
            pass

    def _stop_meter(self) -> None:
        if not self._meter_active:
            return
        from comms.audio_device_manager import get_audio_device_manager

        device = self._in_combo.currentData()
        get_audio_device_manager().release_input(self._METER_OWNER, device)
        self._meter_active = False
        self._level_bar.setValue(0)
        self._level_label.setText(_("-- dBFS"))

    def _on_audio_chunk(self, chunk: NDArray[np.float32]) -> None:
        """Runs on the shared audio callback thread — keep this fast."""
        if len(chunk) == 0:
            return
        now = time.monotonic()
        if now - self._last_meter_emit < self._METER_MIN_INTERVAL_S:
            return
        self._last_meter_emit = now
        peak = float(np.max(np.abs(chunk)))
        dbfs = 20.0 * math.log10(max(peak, 1e-6))
        self.level_updated.emit(dbfs)

    def _on_level_updated(self, dbfs: float) -> None:
        pct = max(0.0, min(100.0, (dbfs + 60.0) / 60.0 * 100.0))
        self._level_bar.setValue(int(pct))
        color = "#2ecc71" if dbfs < -12.0 else ("#f1c40f" if dbfs < -3.0 else "#e74c3c")
        self._level_bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")
        self._level_label.setText(_("{db:.0f} dBFS").format(db=dbfs))

    def _on_test(self) -> None:
        """Play a short 1 kHz tone through the selected output device."""
        if not self._sd_available:
            return

        out_idx = self._out_combo.currentData()
        if out_idx is None:
            return

        pin_target = self._pin_out_combo.currentData() if self._pin_out_combo is not None else None

        try:
            import numpy as np

            from comms.audio_device_manager import pin_output_stream, snapshot_output_streams

            sr = 48000
            t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
            tone = (0.3 * np.sin(2 * math.pi * 1000 * t)).astype(np.float32)
            before = snapshot_output_streams() if pin_target else None
            self._sd.play(tone, samplerate=sr, device=out_idx, blocking=False)
            if pin_target and before is not None:
                pin_output_stream(pin_target, before)
            self._status_label.setText(_("Playing 1 kHz test tone…"))
            self._status_label.setStyleSheet("color: #4a9eff;")
        except Exception as exc:  # noqa: BLE001
            self._status_label.setText(_("Test failed: {e}").format(e=exc))
            self._status_label.setStyleSheet("color: orange;")

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def save(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of current settings."""
        if not self._sd_available or not hasattr(self, "_in_combo"):
            return {"configured": False}

        return {
            "configured": True,
            "input_device_index": self._in_combo.currentData(),
            "input_device_label": self._in_combo.currentText(),
            "output_device_index": self._out_combo.currentData(),
            "output_device_label": self._out_combo.currentText(),
            "sample_rate_hz": 48000,
            "input_source_name": (
                self._pin_in_combo.currentData() if self._pin_in_combo is not None else None
            ),
            "output_sink_name": (
                self._pin_out_combo.currentData() if self._pin_out_combo is not None else None
            ),
        }

    def load(self, data: dict[str, object]) -> None:
        """Restore settings from a previously saved dict."""
        if not self._sd_available or not hasattr(self, "_in_combo"):
            return

        in_idx = data.get("input_device_index")
        out_idx = data.get("output_device_index")

        if in_idx is not None:
            for i in range(self._in_combo.count()):
                if self._in_combo.itemData(i) == in_idx:
                    self._in_combo.setCurrentIndex(i)
                    break

        if out_idx is not None:
            for i in range(self._out_combo.count()):
                if self._out_combo.itemData(i) == out_idx:
                    self._out_combo.setCurrentIndex(i)
                    break

        in_source = data.get("input_source_name")
        if in_source is not None and self._pin_in_combo is not None:
            for i in range(self._pin_in_combo.count()):
                if self._pin_in_combo.itemData(i) == in_source:
                    self._pin_in_combo.setCurrentIndex(i)
                    break

        out_sink = data.get("output_sink_name")
        if out_sink is not None and self._pin_out_combo is not None:
            for i in range(self._pin_out_combo.count()):
                if self._pin_out_combo.itemData(i) == out_sink:
                    self._pin_out_combo.setCurrentIndex(i)
                    break


class RigSettingsDialog(QDialog):
    """Radio > Rig Settings dialog.

    Four tabs — Rig 1, Rig 2, SDR Settings, and Sound Card — each backed
    by its panel.  Hamlib models are loaded once and shared between both
    rig panels.

    DB keys written on OK:
        ``rig1_settings``      — Rig 1 JSON dict
        ``rig2_settings``      — Rig 2 JSON dict (includes ``"enabled": bool``)
        ``sdr_settings``       — SDR JSON dict
        ``soundcard_settings`` — Sound Card JSON dict
    """

    def __init__(self, conn: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self.setWindowTitle(_("Rig Settings"))

        # Load models once; share between both panels to avoid double Hamlib scan
        self._all_models = _load_hamlib_models()
        self._panel1 = _RigPanel(1, self._all_models)
        self._panel2 = _RigPanel(2, self._all_models)
        self._sdr_panel = _SdrSettingsPanel()
        self._soundcard_panel = _SoundCardPanel()

        self._setup_ui()
        self._load_settings()

        # Size from actual content instead of a stale fixed guess: a static
        # resize() call here drifts out of date as translations/combo
        # options grow (reported on macOS — the SDR-mode "Rig Type" combo's
        # longest option and the SDR radio's own label were both clipped at
        # an old fixed 560px width, with essentially zero margin once
        # measured against this dialog's real sizeHint()). The margin added
        # on top of sizeHint() covers native widget metrics (fonts, combo
        # box chrome) varying enough across platforms that the bare
        # sizeHint sometimes still clips by a few pixels on macOS
        # specifically. The floor keeps the previous 560x620 as a lower
        # bound so the dialog never gets smaller than before.
        hint = self.sizeHint()
        self.resize(max(hint.width() + 40, 560), max(hint.height() + 20, 620))

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._panel1, _("Rig 1"))
        self._tabs.addTab(self._panel2, _("Rig 2"))
        self._tabs.addTab(self._sdr_panel, _("SDR Settings"))
        self._tabs.addTab(self._soundcard_panel, _("Sound Card"))
        layout.addWidget(self._tabs)

        # Bidirectional sync: SDR tab ↔ Rig tabs
        self._sdr_panel.assigned_rig_changed.connect(self._on_sdr_assignment_changed)
        self._panel1.sdr_mode_changed.connect(lambda on: self._on_rig_sdr_toggled(1, on))
        self._panel2.sdr_mode_changed.connect(lambda on: self._on_rig_sdr_toggled(2, on))

        # Hamlib info row: shown only on Rig 1 / Rig 2 tabs, hidden on SDR tab
        from PySide6.QtWidgets import QWidget as _QWidget

        from core.hamlib_info import get_hamlib_version

        n = len(self._all_models)
        hamlib_ver = get_hamlib_version()
        self._hamlib_info_widget = _QWidget()
        info_row = QHBoxLayout(self._hamlib_info_widget)
        info_row.setContentsMargins(0, 0, 0, 0)
        self._status_label = QLabel(
            _("{n} rig models available  |  Hamlib {ver}").format(n=n, ver=hamlib_ver)
        )
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        info_row.addWidget(self._status_label)
        info_row.addStretch()
        hamlib_update_btn = QPushButton(_("Hamlib Update…"))
        hamlib_update_btn.setFlat(True)
        hamlib_update_btn.setStyleSheet("color: #3498db; text-decoration: underline;")
        hamlib_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hamlib_update_btn.clicked.connect(self._on_hamlib_update)
        info_row.addWidget(hamlib_update_btn)
        layout.addWidget(self._hamlib_info_widget)

        # Hide Hamlib info row when the SDR tab (index 2) is active
        self._tabs.currentChanged.connect(self._on_tab_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_settings)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # Settings persistence
    # ------------------------------------------------------------------ #

    def _on_hamlib_update(self) -> None:
        from ui.hamlib_update_dialog import HamlibUpdateDialog

        HamlibUpdateDialog(self).exec()

    def _on_tab_changed(self, index: int) -> None:
        """Show Hamlib info row only on Rig 1 / Rig 2 tabs (not SDR or Sound Card tab)."""
        # Tab 0=Rig1, 1=Rig2, 2=SDR Settings, 3=Sound Card
        self._hamlib_info_widget.setVisible(index < 2)

    def _on_sdr_assignment_changed(self, assigned_rig: object) -> None:
        """Sync Rig tab SDR radio buttons when the SDR panel assignment changes."""
        # Update Rig 1 tab without re-triggering the loop
        panel1_is_sdr = assigned_rig == 1
        if self._panel1._radio_sdr.isChecked() != panel1_is_sdr:
            self._panel1._radio_sdr.blockSignals(True)
            if panel1_is_sdr:
                self._panel1._radio_sdr.setChecked(True)
            elif self._panel1._radio_sdr.isChecked():
                self._panel1._radio_direct.setChecked(True)
            self._panel1._radio_sdr.blockSignals(False)
            self._panel1._on_mode_toggled()

        panel2_is_sdr = assigned_rig == 2
        if self._panel2._radio_sdr.isChecked() != panel2_is_sdr:
            self._panel2._radio_sdr.blockSignals(True)
            if panel2_is_sdr:
                self._panel2._radio_sdr.setChecked(True)
            elif self._panel2._radio_sdr.isChecked():
                self._panel2._radio_direct.setChecked(True)
            self._panel2._radio_sdr.blockSignals(False)
            self._panel2._on_mode_toggled()

    def _on_rig_sdr_toggled(self, rig_index: int, sdr_on: bool) -> None:
        """Sync the SDR panel assignment when a Rig tab SDR button is toggled."""
        if sdr_on:
            self._sdr_panel.set_assigned_rig(rig_index)
        else:
            # Only clear if this rig was the one assigned
            current = None
            if hasattr(self._sdr_panel, "_rig1_rb") and self._sdr_panel._rig1_rb.isChecked():
                current = 1
            elif hasattr(self._sdr_panel, "_rig2_rb") and self._sdr_panel._rig2_rb.isChecked():
                current = 2
            if current == rig_index:
                self._sdr_panel.set_assigned_rig(None)

    def _load_settings(self) -> None:
        """Load Rig 1 and Rig 2 settings from the DB.

        Migrates the legacy ``rig_settings`` key to ``rig1_settings`` on first
        open so existing configurations are not lost.
        """
        if not hasattr(self._conn, "execute"):
            return

        # --- Rig 1: migrate legacy 'rig_settings' → 'rig1_settings' ---
        row1 = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'rig1_settings'"
        ).fetchone()
        if row1 is None:
            row_old = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'rig_settings'"
            ).fetchone()
            if row_old and row_old["value"]:
                self._conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                    "VALUES ('rig1_settings', ?, CURRENT_TIMESTAMP)",
                    (row_old["value"],),
                )
                self._conn.commit()
                row1 = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'rig1_settings'"
                ).fetchone()

        if row1 and row1["value"]:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                self._panel1.load(json.loads(row1["value"]))

        # --- Rig 2 ---
        row2 = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'rig2_settings'"
        ).fetchone()
        if row2 and row2["value"]:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                self._panel2.load(json.loads(row2["value"]))

        # --- SDR ---
        row_sdr = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'sdr_settings'"
        ).fetchone()
        if row_sdr and row_sdr["value"]:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                self._sdr_panel.load(json.loads(row_sdr["value"]))

        # Sync initial state: fire assignment signal so Rig tabs reflect loaded SDR setting
        self._sdr_panel._on_assignment_changed()
        # Also restore SDR radio button state on Rig panels from their own saved mode
        self._panel1._on_mode_toggled()
        self._panel2._on_mode_toggled()

        # --- Sound Card ---
        row_sc = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if row_sc and row_sc["value"]:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                self._soundcard_panel.load(json.loads(row_sc["value"]))

    def _save_settings(self) -> None:
        """Save Rig 1 and Rig 2 settings to the DB.

        When Rig 2 is enabled, ``radio_type`` for both rigs is derived
        automatically from Rig 2's split_mode selection so the caller
        never has to set both manually:

        * ``rig1_dl_rig2_ul`` → Rig 1 = rx_only, Rig 2 = tx_only
        * ``rig1_ul_rig2_dl`` → Rig 1 = tx_only, Rig 2 = rx_only
        """
        if not hasattr(self._conn, "execute"):
            return

        s1 = self._panel1.save()
        s2 = self._panel2.save()

        # Derive radio_type for both rigs from the split-mode combo when Rig 2 is active
        if s2.get("enabled", False):
            split_mode = str(s2.get("split_mode", "rig1_dl_rig2_ul"))
            if split_mode == "rig1_dl_rig2_ul":
                s1["radio_type"] = "rx_only"
                s2["radio_type"] = "tx_only"
            else:  # rig1_ul_rig2_dl
                s1["radio_type"] = "tx_only"
                s2["radio_type"] = "rx_only"
        # When Rig 2 is disabled, s1["radio_type"] comes from the Rig 1 panel as-is

        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('rig1_settings', ?, CURRENT_TIMESTAMP)",
            (json.dumps(s1),),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('rig2_settings', ?, CURRENT_TIMESTAMP)",
            (json.dumps(s2),),
        )
        s_sdr = self._sdr_panel.save()
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('sdr_settings', ?, CURRENT_TIMESTAMP)",
            (json.dumps(s_sdr),),
        )
        s_sc = self._soundcard_panel.save()
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('soundcard_settings', ?, CURRENT_TIMESTAMP)",
            (json.dumps(s_sc),),
        )
        self._conn.commit()
