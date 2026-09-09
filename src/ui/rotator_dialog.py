"""
Rotator settings dialog.

RotatorSettingsDialog — Dialog opened from Radio > Rotator Settings.
Supports Hamlib direct connection and NET (rotctld) connection.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.hamlib_info import get_hamlib_version
from i18n import _
from rig.controller import HAMLIB_AVAILABLE
from ui.rig_dialog import _BaudTestNotifier, _scan_serial_ports  # reuse test infra

logger = logging.getLogger(__name__)

# Pseudo rotator models with no real serial link — the connection Test button
# is meaningless (Dummy always "succeeds"; NET rotctl is not a serial device).
_NON_TESTABLE_ROT_MODELS: frozenset[int] = frozenset({1, 2})

# ---------------------------------------------------------------------------
# Fallback rotator model list.
# Hamlib 4.x removed the rotlist dict, so models are hard-coded here.
# ---------------------------------------------------------------------------
_FALLBACK_ROT_MODELS: list[tuple[int, str, str]] = [
    (1, "Hamlib", "Dummy"),
    (2, "Hamlib", "NET rotctl"),
    (201, "AMSAT", "EasyComm I"),
    (202, "AMSAT", "EasyComm II"),
    (204, "AMSAT", "EasyComm III"),
    (301, "AEA", "Fodtrack"),
    (401, "Rotor-EZ", "Rotor-EZ"),
    (601, "Yaesu", "GS-232A"),
    (602, "Yaesu", "GS-232 Generic"),
    (603, "Yaesu", "GS-232B"),
    (607, "Yaesu", "LVB Tracker"),
    (608, "Yaesu", "ST-2"),
    (901, "SPID", "ROT2PROG"),
    (902, "SPID", "ROT1PROG"),
    (903, "SPID", "MD-01/02 ROT2PROG"),
    (1001, "M2", "RC2800"),
    (1101, "ARS", "RCI AZEL"),
    (1102, "ARS", "RCI AZ"),
    (1401, "Celestron", "NexStar"),
    (1801, "Meade", "LX200"),
    (1901, "iOptron", "iOptron"),
    (2401, "GRBL", "GRBLtrk Serial"),
    (2402, "GRBL", "GRBLtrk NET"),
    (2801, "SkyWatcher", "SkyWatcher"),
]


def _load_from_hamlib_rot_api() -> list[tuple[int, str, str]]:
    """Fetch rotator models from the Hamlib Python binding (Hamlib 3.x rotlist only).

    Hamlib 4.x removed rotlist; returns an empty list in that case.
    """
    try:
        import Hamlib as _H  # lazy — avoids Qt TLS collision at startup
    except ModuleNotFoundError:
        return []
    if not hasattr(_H, "rotlist"):
        return []
    models: list[tuple[int, str, str]] = []
    try:
        for model_id, info in _H.rotlist.items():
            name = str(getattr(info, "model_name", "") or "").strip()
            mfg = str(getattr(info, "mfg_name", "") or "").strip()
            if name:
                models.append((int(model_id), mfg, name))
    except (AttributeError, TypeError):
        pass
    return models


def _load_rot_models() -> list[tuple[int, str, str]]:
    """Return all Hamlib rotator models sorted by manufacturer and model name."""
    models = _load_from_hamlib_rot_api()
    if not models:
        models = list(_FALLBACK_ROT_MODELS)
    return sorted(models, key=lambda x: (x[1].lower(), x[2].lower()))


class RotatorSettingsDialog(QDialog):
    """Radio > Rotator Settings dialog.

    Displays common Hamlib rotator models with a search filter.
    Settings are persisted as a JSON blob in app_settings under 'rotator_settings'.
    """

    def __init__(self, conn: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._all_models: list[tuple[int, str, str]] = []
        self.setWindowTitle(_("Rotator Settings"))
        self.resize(480, 380)
        self._setup_ui()
        self._load_models()
        self._load_settings()
        self._on_scan_ports()
        self._update_baud_test_visibility()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Connection mode ---
        mode_group = QGroupBox(_("Connection Mode"))
        mode_layout = QVBoxLayout(mode_group)
        self._radio_direct = QRadioButton(_("Direct (Hamlib built-in)"))
        self._radio_net = QRadioButton(_("NET (rotctld compatible)"))
        self._radio_direct.setChecked(True)
        self._radio_direct.toggled.connect(self._on_mode_toggled)
        mode_layout.addWidget(self._radio_direct)
        mode_layout.addWidget(self._radio_net)
        layout.addWidget(mode_group)

        # --- Direct connection settings ---
        self._direct_group = QGroupBox(_("Direct Connection Settings"))
        direct_form = QFormLayout(self._direct_group)

        port_row = QWidget()
        port_layout = QHBoxLayout(port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(160)
        self._port_combo.currentTextChanged.connect(self._reset_baud_test_btn)
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
        for b in ["4800", "9600", "19200", "38400"]:
            self._baud_combo.addItem(b)
        self._baud_combo.setCurrentText("9600")
        self._baud_combo.currentTextChanged.connect(self._reset_baud_test_btn)
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
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        direct_form.addRow(_("Rot Model:"), self._model_combo)

        layout.addWidget(self._direct_group)

        # --- NET connection settings ---
        self._net_group = QGroupBox(_("NET Connection Settings"))
        net_form = QFormLayout(self._net_group)
        self._host_edit = QLineEdit("localhost")
        net_form.addRow(_("Host:"), self._host_edit)
        self._net_port_spin = QSpinBox()
        self._net_port_spin.setRange(1, 65535)
        self._net_port_spin.setValue(4533)
        net_form.addRow(_("Port:"), self._net_port_spin)
        layout.addWidget(self._net_group)
        self._net_group.setVisible(False)

        # --- Status + Hamlib version / update link ---
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status_label)

        hamlib_row = QHBoxLayout()
        hamlib_row.addWidget(QLabel(_("Hamlib {ver}").format(ver=get_hamlib_version())))
        hamlib_row.addStretch()
        hamlib_update_btn = QPushButton(_("Hamlib Update…"))
        hamlib_update_btn.setFlat(True)
        hamlib_update_btn.setStyleSheet("color: #3498db; text-decoration: underline;")
        hamlib_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hamlib_update_btn.clicked.connect(self._on_hamlib_update)
        hamlib_row.addWidget(hamlib_update_btn)
        layout.addLayout(hamlib_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_settings)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # Model list
    # ------------------------------------------------------------------ #

    def _load_models(self) -> None:
        """Fetch the Hamlib rotator model list and initialise the combo box."""
        self._all_models = _load_rot_models()
        self._populate_model_combo(self._all_models)
        self._status_label.setText(
            _("{n} rotator models available").format(n=len(self._all_models))
        )

    def _populate_model_combo(self, models: list[tuple[int, str, str]]) -> None:
        """Update the model combo box with the given list, preserving the current selection."""
        current_id: int | None = self._model_combo.currentData()
        self._model_combo.clear()
        for mid, mfg, name in models:
            label = f"{mfg} — {name} (#{mid})" if mfg else f"{name} (#{mid})"
            self._model_combo.addItem(label, mid)
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == current_id:
                self._model_combo.setCurrentIndex(i)
                break

    def _on_model_search(self, text: str) -> None:
        """Filter the model list in real time as the user types."""
        query = text.lower().strip()
        if not query:
            self._populate_model_combo(self._all_models)
            self._status_label.setText(
                _("{n} rotator models available").format(n=len(self._all_models))
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
    # Port scan / mode toggle
    # ------------------------------------------------------------------ #

    def _on_mode_toggled(self, _checked: bool) -> None:
        is_direct = self._radio_direct.isChecked()
        self._direct_group.setVisible(is_direct)
        self._net_group.setVisible(not is_direct)
        self._update_baud_test_visibility()

    # ------------------------------------------------------------------ #
    # Direct-mode connection test (Baud Rate row)
    # ------------------------------------------------------------------ #

    def _on_model_changed(self, *_args: object) -> None:
        """Reset the Test button and refresh its visibility when the model changes."""
        self._reset_baud_test_btn()
        self._update_baud_test_visibility()

    def _reset_baud_test_btn(self, *_args: object) -> None:
        """Return the Test button to its neutral state (port/baud/model changed)."""
        if not hasattr(self, "_baud_test_btn"):
            return  # signal fired during _setup_ui before the button exists
        self._baud_test_btn.setText(_("Test"))
        self._baud_test_btn.setStyleSheet("")
        self._baud_test_btn.setEnabled(True)

    def _update_baud_test_visibility(self) -> None:
        """Show the Test button only when a real serial rotator can actually be probed."""
        if not hasattr(self, "_baud_test_btn"):
            return
        model_id = self._model_combo.currentData()
        visible = (
            HAMLIB_AVAILABLE
            and self._radio_direct.isChecked()
            and isinstance(model_id, int)
            and model_id not in _NON_TESTABLE_ROT_MODELS
        )
        self._baud_test_btn.setVisible(visible)

    def _on_baud_test(self) -> None:
        """Open the rotator via Hamlib at the chosen baud and report success/failure.

        A successful Hamlib rot.open() performs the model's serial handshake
        with the unit (e.g. SkyWatcher :F1/:F2), so it is the rotator analogue
        of the rig dialog's "did the radio answer" baud test. Runs on a
        background thread — Hamlib rotator I/O must never touch the UI thread.
        """
        port = self._port_combo.currentText().strip()
        baud = int(self._baud_combo.currentText())
        model_id = self._model_combo.currentData()
        if not isinstance(model_id, int):
            return
        if not port:
            self._baud_test_btn.setText(_("No port"))
            self._baud_test_btn.setStyleSheet("color: orange;")
            return

        self._baud_test_btn.setText(_("Testing…"))
        self._baud_test_btn.setEnabled(False)
        self._baud_test_btn.setStyleSheet("")

        # Keep the notifier on self so it survives until the worker fires it.
        self._baud_notifier = _BaudTestNotifier()

        def _apply(ok: bool) -> None:
            self._baud_test_btn.setEnabled(True)
            if ok:
                self._baud_test_btn.setText("✓ OK")
                self._baud_test_btn.setStyleSheet(
                    "background-color: #27ae60; color: white; font-weight: bold;"
                )
            else:
                self._baud_test_btn.setText("✗ Failed")
                self._baud_test_btn.setStyleSheet(
                    "background-color: #c0392b; color: white; font-weight: bold;"
                )

        self._baud_notifier.done.connect(_apply)

        def _test() -> None:
            ok = False
            try:
                from rig.controller import HamlibRotatorController

                ctrl = HamlibRotatorController(model_id=model_id, port=port, baud_rate=baud)
                try:
                    ok = bool(ctrl.connect())
                finally:
                    ctrl.disconnect()
            except Exception:
                logger.exception("Rotator baud test failed")
                ok = False
            self._baud_notifier.done.emit(ok)

        threading.Thread(target=_test, daemon=True).start()

    def _on_scan_ports(self) -> None:
        """Scan serial ports and update the combo box."""
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

    # ------------------------------------------------------------------ #
    def _on_hamlib_update(self) -> None:
        from ui.hamlib_update_dialog import HamlibUpdateDialog

        HamlibUpdateDialog(self).exec()

    # ------------------------------------------------------------------ #
    # Settings load / save
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> None:
        """Load saved rotator settings from the database."""
        if not hasattr(self._conn, "execute"):
            return
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'rotator_settings'"
        ).fetchone()
        if not row or not row["value"]:
            return
        try:
            s: dict[str, Any] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return

        if s.get("mode") == "net":
            self._radio_net.setChecked(True)

        port = str(s.get("port", ""))
        if port:
            idx = self._port_combo.findText(port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
            else:
                self._port_combo.setEditText(port)

        baud = str(s.get("baud_rate", 9600))
        idx = self._baud_combo.findText(baud)
        if idx >= 0:
            self._baud_combo.setCurrentIndex(idx)

        model_id = int(s.get("model_id", 1))
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == model_id:
                self._model_combo.setCurrentIndex(i)
                break

        self._host_edit.setText(str(s.get("host", "localhost")))
        self._net_port_spin.setValue(int(s.get("net_port", 4533)))

    def _save_settings(self) -> None:
        """Save rotator settings to the database."""
        model_id: int = self._model_combo.currentData() or 1
        s = {
            "mode": "direct" if self._radio_direct.isChecked() else "net",
            "port": self._port_combo.currentText(),
            "baud_rate": int(self._baud_combo.currentText()),
            "model_id": model_id,
            "host": self._host_edit.text(),
            "net_port": self._net_port_spin.value(),
        }
        if hasattr(self._conn, "execute"):
            self._conn.execute(
                """
                INSERT OR REPLACE INTO app_settings (key, value, updated_at)
                VALUES ('rotator_settings', ?, CURRENT_TIMESTAMP)
                """,
                (json.dumps(s),),
            )
            self._conn.commit()
