"""
Rotator settings dialog.

RotatorSettingsDialog — Dialog opened from Radio > Rotator Settings.
Supports Hamlib direct connection and NET (rotctld) connection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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

# Models for which the stop point and auto flip make sense: Yaesu GS-232 family
# controllers (G-5500 and similar) and the ST-2, which take true bearings and
# swing the elevation axis to 180 degrees. Hamlib's own elevation limit is no
# guide here — telescope mounts report 180 too but cannot turn an antenna
# over. Extend this set when another rotator is confirmed to work.
_FLIP_CAPABLE_ROT_MODELS: frozenset[int] = frozenset({601, 602, 603, 607, 608})


def _probe_rotator(model_id: int, port: str, baud: int) -> bool:
    """Open the rotator via Hamlib at *baud* and return True iff it responds.

    Talks to Hamlib directly rather than via HamlibRotatorController because
    we need ``rot.error_status``: on a wrong baud the SkyWatcher (and other
    fixed-rate backends) still return ``None`` from ``rot.open()`` but set
    ``error_status`` to ``-RIG_ETIMEOUT`` after the handshake read times out,
    so ``connect()``'s boolean cannot tell a live link from a dead one.
    A matching baud leaves ``error_status == 0``.

    Runs on a background thread — Hamlib rotator I/O must never touch the
    UI thread.
    """
    try:
        import Hamlib  # bundled; resolved lazily on the worker thread
    except Exception:
        return False
    rot = Hamlib.Rot(model_id)
    rot.set_conf("rot_pathname", port)
    rot.set_conf("serial_speed", str(baud))
    try:
        rot.open()
        ok = bool(rot.error_status == 0)
    except Exception:
        logger.exception("Rotator probe: rot.open() raised")
        ok = False
    finally:
        with contextlib.suppress(Exception):
            rot.close()
    return ok


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

        # --- Antenna pose and park position ---
        self._pose_group = QGroupBox(_("Stop Point / Flip / Park"))
        pose_form = QFormLayout(self._pose_group)

        self._stop_combo = QComboBox()
        self._stop_combo.addItem(_("North"), "north")
        self._stop_combo.addItem(_("South"), "south")
        self._stop_combo.setToolTip(
            _(
                "Where the rotator's mechanical stop is. Choose South for a rotator that "
                "turns through north without unwinding (north in the middle of its range)."
            )
        )
        pose_form.addRow(_("Rotator stop point:"), self._stop_combo)

        self._flip_cb = QCheckBox(_("Auto flip for passes crossing the stop point"))
        self._flip_cb.setToolTip(
            _(
                "For a pass that crosses the stop point, add 180 degrees to the azimuth and "
                "send the elevation as 180 - elevation, so the antenna is turned over instead "
                "of the rotator unwinding. Needs an elevation rotator that can swing to 180 "
                "degrees. South Init is ignored while this or a South stop point is used."
            )
        )
        pose_form.addRow(self._flip_cb)

        self._flip_note = QLabel("")
        self._flip_note.setWordWrap(True)
        self._flip_note.setStyleSheet("color: gray;")
        # A word-wrapped label in a form row is laid out for a single line,
        # so a longer note overlapped the rows below; reserve three lines.
        self._flip_note.setMinimumHeight(self._flip_note.fontMetrics().lineSpacing() * 3)
        self._flip_note.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        pose_form.addRow(self._flip_note)

        self._park_cb = QCheckBox(_("Use park position"))
        self._park_cb.setToolTip(
            _("Used by the Park button instead of the rotator's own park command.")
        )
        pose_form.addRow(self._park_cb)

        park_row = QWidget()
        park_layout = QHBoxLayout(park_row)
        park_layout.setContentsMargins(0, 0, 0, 0)
        self._park_az_spin = QDoubleSpinBox()
        self._park_az_spin.setRange(0.0, 360.0)
        self._park_az_spin.setDecimals(1)
        self._park_el_spin = QDoubleSpinBox()
        self._park_el_spin.setRange(0.0, 90.0)
        self._park_el_spin.setDecimals(1)
        park_layout.addWidget(QLabel(_("AZ:")))
        park_layout.addWidget(self._park_az_spin)
        park_layout.addWidget(QLabel(_("EL:")))
        park_layout.addWidget(self._park_el_spin)
        pose_form.addRow(_("Park position:"), park_row)

        self._after_pass_combo = QComboBox()
        self._after_pass_combo.addItem(_("Move to the next AOS"), "next_aos")
        self._after_pass_combo.addItem(_("Wait at the park position"), "park")
        pose_form.addRow(_("After a pass:"), self._after_pass_combo)

        self._park_on_disconnect_cb = QCheckBox(_("Move to the park position on disconnect"))
        pose_form.addRow(self._park_on_disconnect_cb)

        self._park_cb.toggled.connect(self._on_park_toggled)
        self._on_park_toggled(False)
        layout.addWidget(self._pose_group)
        self._update_flip_availability()

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

    def _on_park_toggled(self, checked: bool) -> None:
        """Park position fields only matter while the park position is in use."""
        for w in (
            self._park_az_spin,
            self._park_el_spin,
            self._after_pass_combo,
            self._park_on_disconnect_cb,
        ):
            w.setEnabled(checked)

    def _on_mode_toggled(self, _checked: bool) -> None:
        is_direct = self._radio_direct.isChecked()
        self._direct_group.setVisible(is_direct)
        self._net_group.setVisible(not is_direct)
        self._update_baud_test_visibility()
        self._update_flip_availability()

    def _update_flip_availability(self) -> None:
        """Enable the stop point / auto flip only where they can work.

        Direct mode: only for the Yaesu GS-232 family and ST-2. NET mode: the
        rotator behind rotctld is unknown here, so the options stay available
        and the operator is told what rotctld must be driving. Anything else
        is greyed out and reset, so a stale choice cannot be saved.
        """
        if not hasattr(self, "_flip_note"):
            return  # signal fired during _setup_ui before the widgets exist
        if self._radio_net.isChecked():
            enabled = True
            note = _(
                "NET mode: works only if rotctld is driving a Yaesu GS-232 family "
                "controller or ST-2 (elevation up to 180 degrees)."
            )
        else:
            enabled = self._model_combo.currentData() in _FLIP_CAPABLE_ROT_MODELS
            note = (
                ""
                if enabled
                else _("Available only for Yaesu GS-232 family controllers and the ST-2.")
            )
        self._stop_combo.setEnabled(enabled)
        self._flip_cb.setEnabled(enabled)
        self._flip_note.setText(note)
        self._flip_note.setVisible(bool(note))
        if not enabled:
            self._stop_combo.setCurrentIndex(max(self._stop_combo.findData("north"), 0))
            self._flip_cb.setChecked(False)

    # ------------------------------------------------------------------ #
    # Direct-mode connection test (Baud Rate row)
    # ------------------------------------------------------------------ #

    def _on_model_changed(self, *_args: object) -> None:
        """Reset the Test button and refresh its visibility when the model changes."""
        self._reset_baud_test_btn()
        self._update_baud_test_visibility()
        self._update_flip_availability()

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
        """Probe the rotator at the chosen baud and colour the button by the result.

        The probe (``_probe_rotator``) opens the rotator via Hamlib and checks
        ``rot.error_status``: a matching baud completes the model's serial
        handshake (e.g. SkyWatcher :F1/:F2) and leaves it 0; a wrong baud
        times out with -RIG_ETIMEOUT. Runs on a background thread.
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
                ok = _probe_rotator(model_id, port, baud)
            except Exception:
                logger.exception("Rotator baud test failed")
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

        idx = self._stop_combo.findData(str(s.get("stop_point", "north")))
        self._stop_combo.setCurrentIndex(max(idx, 0))
        self._flip_cb.setChecked(bool(s.get("auto_flip", False)))
        self._park_cb.setChecked(bool(s.get("park_enabled", False)))
        with contextlib.suppress(TypeError, ValueError):
            self._park_az_spin.setValue(float(s.get("park_az", 0.0)))
            self._park_el_spin.setValue(float(s.get("park_el", 0.0)))
        idx = self._after_pass_combo.findData(str(s.get("after_pass", "next_aos")))
        self._after_pass_combo.setCurrentIndex(max(idx, 0))
        self._park_on_disconnect_cb.setChecked(bool(s.get("park_on_disconnect", False)))
        # Saved values for a model that cannot use them are dropped here too.
        self._update_flip_availability()

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
            "stop_point": self._stop_combo.currentData() or "north",
            "auto_flip": self._flip_cb.isChecked(),
            "park_enabled": self._park_cb.isChecked(),
            "park_az": self._park_az_spin.value(),
            "park_el": self._park_el_spin.value(),
            "after_pass": self._after_pass_combo.currentData() or "next_aos",
            "park_on_disconnect": self._park_on_disconnect_cb.isChecked(),
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
