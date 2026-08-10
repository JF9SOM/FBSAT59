"""
Radio Control widget.

RadioControlWidget — Rig and rotator control panel for the selected satellite.
Supports two independent rig controllers (Rig 1 and Rig 2) with separate
connect/disconnect buttons and status rows.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager
from comms.mode_detection import (
    is_aprs_transmitter,
    is_cw_transmitter,
    is_ft4_transmitter,
    is_sstv_transmitter,
)
from i18n import _
from rig.controller import (
    HamlibDirectController,
    RigController,
    RigState,
    RotatorController,
    RotatorState,
)
from sdr import LAMEENC_AVAILABLE, AudioRecorder

# Transponder combo box item background colors, shown only while the dropdown
# is open (Qt does not apply BackgroundRole to the closed combo box face).
# Chosen to be visually distinct from colors already used elsewhere in the app
# for a different meaning (e.g. the satellite list's AMSAT-status yellow/red,
# and the FT4 own-call highlight's plain Qt.GlobalColor.yellow).
_XPDR_INACTIVE_BG = QColor("#b8860b")  # dark goldenrod — SATNOGS status=inactive
_XPDR_INVALID_BG = QColor("#8b0000")  # dark red — SATNOGS status=invalid

# Persistent per-transponder RX offset (GitHub Issue #18). Range/step are
# sized for TCXO-aging-style drift (typically sub-kHz to a few kHz), not for
# recovering from picking the wrong transponder entirely.
_RX_OFFSET_RANGE_HZ = 5000
_RX_OFFSET_STEP_HZ = 10

logger = logging.getLogger(__name__)


class RadioControlWidget(QWidget):
    """
    Rig and rotator control panel.

    Displays Doppler-corrected frequency, mode, rotator position, and
    rig/rotator connection status for the selected satellite.

    Supports two independent rigs (Rig 1 / Rig 2) with separate connect
    buttons and status indicators.
    """

    transmitter_changed: Signal = Signal(object)
    cycle_changed: Signal = Signal(int)  # ms
    tune_requested: Signal = Signal()
    lock_changed: Signal = Signal(bool)
    # Emitted when the user edits the persistent per-transponder RX offset
    # spinbox (GitHub Issue #18) — not emitted for programmatic display updates.
    rx_offset_changed: Signal = Signal(float)
    rig_connected: Signal = Signal()
    rig_disconnected: Signal = Signal()
    rig2_connected: Signal = Signal()
    rig2_disconnected: Signal = Signal()
    rotator_connected: Signal = Signal()
    south_init_changed: Signal = Signal(bool)
    ctcss_send_requested: Signal = Signal(float)
    ctcss_activate_requested: Signal = Signal()  # activation-tone button pressed
    cw_mode_requested: Signal = Signal(str, str)  # dl_cw_mode, ul_cw_mode
    dmode_requested: Signal = Signal(str, str)  # dl_data_mode, ul_data_mode
    _rig1_connect_done: Signal = Signal(bool)  # internal: True = connected successfully
    # Emitted when a transponder whose description implies SSTV/SSDV or APRS
    # is selected — MainWindow uses these to auto-open the matching tab.
    sstv_transponder_selected: Signal = Signal()
    aprs_transponder_selected: Signal = Signal()
    ft4_transponder_selected: Signal = Signal()
    meteor_transponder_selected: Signal = Signal(int, int)  # norad, downlink_hz
    cw_transponder_selected: Signal = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rig1: RigController | None = None
        self._rig2: RigController | None = None
        self._rotator: RotatorController | None = None
        self._transmitters: list[dict[str, Any]] = []
        self._current_ctcss_hz: float | None = None
        self._ctcss_activation_hz: float | None = None
        # Pending comms tab to open after rig connects ("sstv", "aprs", "ft4")
        self._pending_comms_tab: str | None = None
        # CW toggle button state
        self._cw_mode_active: bool = False
        self._orig_dl_mode: str = ""
        self._orig_ul_mode: str = ""
        self._cw_dl_mode: str = ""
        self._cw_ul_mode: str = ""
        # DATA (-D) toggle button state (GitHub Issue #16)
        self._dmode_active: bool = False
        self._dmode_orig_dl_mode: str = ""
        self._dmode_orig_ul_mode: str = ""
        self._dmode_dl_mode: str = ""
        self._dmode_ul_mode: str = ""
        # SdrControlWidget instance, set via set_sdr_control() once MainWindow
        # constructs it — see that method's docstring for why this exists.
        self._sdr_control: Any = None
        # DB connection, set via set_db_connection() once MainWindow constructs
        # it — needed to look up the Sound Card input device for rig-audio
        # recording (see _start_audio_recording()).
        self._db_conn: sqlite3.Connection | None = None
        # Rig-audio (MP3) recording — shares the AudioRecorder used by SDR
        # Control, but the PCM source here is the Sound Card input device
        # (rig AF output) via AudioDeviceManager, not an SDR pipeline.
        self._audio_save_dir: Path = Path.home() / "audio_recordings"
        self._audio_recorder: AudioRecorder = AudioRecorder(self._audio_save_dir)
        self._rec_in_device: int | None = None
        self._rec_audio_active: bool = False
        self._audio_rec_timer = QTimer(self)
        self._audio_rec_timer.setInterval(1_000)
        self._audio_rec_timer.timeout.connect(self._update_audio_rec_status)
        self._setup_ui()
        self._rig1_connect_done.connect(self._finish_rig1_connect)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── Satellite ──────────────────────────────────────────────────
        sat_group = QGroupBox(_("Satellite"))
        sat_form = QFormLayout(sat_group)
        sat_form.setContentsMargins(4, 2, 4, 2)
        sat_form.setSpacing(3)
        sat_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        # Name + NORAD on one row
        self._sat_name_label = QLabel("—")
        self._sat_name_label.setMinimumWidth(120)
        self._norad_label = QLabel("—")
        name_norad_row = QHBoxLayout()
        name_norad_row.setSpacing(4)
        name_norad_row.addWidget(self._sat_name_label)
        name_norad_row.addSpacing(20)
        name_norad_row.addWidget(QLabel("NORAD:"))
        name_norad_row.addWidget(self._norad_label)
        name_norad_row.addStretch()
        name_norad_row.addWidget(QLabel(_("Offset:")))
        self._offset_spin = QSpinBox()
        self._offset_spin.setRange(-_RX_OFFSET_RANGE_HZ, _RX_OFFSET_RANGE_HZ)
        self._offset_spin.setSingleStep(_RX_OFFSET_STEP_HZ)
        self._offset_spin.setSuffix(_(" Hz"))
        self._offset_spin.setToolTip(
            _(
                "Persistent per-transponder RX (downlink) correction, saved with this "
                "transponder. Added to the nominal downlink before Doppler correction, "
                "so it carries over to every future pass and survives Tune (T). "
                "Positive shifts the corrected frequency higher. Use this for a "
                "satellite's known, fixed frequency error (e.g. TCXO aging); use "
                "Lock (L) for real-time manual retuning within a pass."
            )
        )
        self._offset_spin.setEnabled(False)
        self._offset_spin.valueChanged.connect(self._on_offset_spin_changed)
        name_norad_row.addWidget(self._offset_spin)
        sat_form.addRow(_("Name:"), name_norad_row)

        self._xpdr_combo = QComboBox()
        self._xpdr_combo.setEnabled(False)
        self._xpdr_combo.currentIndexChanged.connect(self._on_xpdr_changed)

        # Transponder combo + T/L buttons on one row
        xpdr_tl_row = QHBoxLayout()
        xpdr_tl_row.setSpacing(4)
        xpdr_tl_row.addWidget(self._xpdr_combo, stretch=1)
        self._tune_btn = QPushButton(_("T"))
        self._tune_btn.setFixedWidth(56)
        self._tune_btn.setToolTip(_("Tune: reset downlink/uplink to center of transmitter band"))
        self._tune_btn.setStyleSheet(
            "QPushButton:pressed { background-color: #e67e22; color: #fff; font-weight: bold; }"
        )
        self._tune_btn.clicked.connect(self.tune_requested.emit)
        self._lock_btn = QPushButton(_("L"))
        self._lock_btn.setFixedWidth(56)
        self._lock_btn.setToolTip(
            _(
                "Lock (Ctrl+L): DL is yours to tune manually; UL follows "
                "(inverting transponder aware). After each manual retune, "
                "allow a few seconds for it to register before relying on it — "
                "the rig is only polled once per Doppler cycle."
            )
        )
        self._lock_btn.setCheckable(True)
        self._lock_btn.setStyleSheet(
            "QPushButton:checked { background-color: #f1c40f; color: #000; font-weight: bold; }"
        )
        self._lock_btn.toggled.connect(self.lock_changed.emit)
        xpdr_tl_row.addWidget(self._tune_btn)
        xpdr_tl_row.addWidget(self._lock_btn)
        sat_form.addRow(_("Transmitter:"), xpdr_tl_row)

        layout.addWidget(sat_group)

        # ── Frequency ──────────────────────────────────────────────────
        freq_group = QGroupBox(_("Frequency"))
        freq_form = QFormLayout(freq_group)
        freq_form.setContentsMargins(4, 2, 4, 2)
        freq_form.setSpacing(3)
        freq_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._downlink_label = QLabel("—")
        self._downlink_doppler_label = QLabel("—")
        self._uplink_label = QLabel("—")
        self._uplink_doppler_label = QLabel("—")
        self._mode_label = QLabel("—")
        self._ctcss_label = QLabel("—")

        # DL freq + Doppler on one row
        dl_row = QHBoxLayout()
        dl_row.setSpacing(4)
        self._downlink_label.setMinimumWidth(110)
        dl_row.addWidget(self._downlink_label)
        dl_row.addSpacing(20)
        dl_row.addWidget(QLabel("Doppler:"))
        dl_row.addWidget(self._downlink_doppler_label)
        dl_row.addStretch()
        freq_form.addRow(_("Downlink:"), dl_row)

        # UL freq + Doppler on one row
        ul_row = QHBoxLayout()
        ul_row.setSpacing(4)
        self._uplink_label.setMinimumWidth(110)
        ul_row.addWidget(self._uplink_label)
        ul_row.addSpacing(20)
        ul_row.addWidget(QLabel("Doppler:"))
        ul_row.addWidget(self._uplink_doppler_label)
        ul_row.addStretch()
        freq_form.addRow(_("Uplink:"), ul_row)

        # Mode + CTCSS on one row
        mode_ctcss_row = QWidget()
        mode_ctcss_layout = QHBoxLayout(mode_ctcss_row)
        mode_ctcss_layout.setContentsMargins(0, 0, 0, 0)
        mode_ctcss_layout.setSpacing(4)
        self._mode_label.setMinimumWidth(40)
        mode_ctcss_layout.addWidget(self._mode_label)
        self._cw_btn = QPushButton()
        self._cw_btn.setToolTip(_("Toggle CW mode on both VFOs"))
        self._cw_btn.setVisible(False)
        self._cw_btn.setMaximumWidth(52)
        self._cw_btn.clicked.connect(self._on_cw_toggle)
        mode_ctcss_layout.addWidget(self._cw_btn)
        self._dmode_btn = QPushButton()
        self._dmode_btn.setToolTip(
            _(
                "Toggle Rig 1's TX VFO to DATA (-D) mode and back, without\n"
                "disconnecting. Some radios (e.g. Icom) require a -D mode\n"
                "(LSB-D/USB-D) to route computer audio to TX instead of the\n"
                "mic for digital modes (FT4 etc.) over a linear (SSB) transponder."
            )
        )
        self._dmode_btn.setVisible(False)
        self._dmode_btn.setMaximumWidth(66)
        self._dmode_btn.clicked.connect(self._on_dmode_toggle)
        mode_ctcss_layout.addWidget(self._dmode_btn)
        mode_ctcss_layout.addSpacing(12)
        mode_ctcss_layout.addWidget(QLabel("CTCSS:"))
        self._ctcss_send_btn = QPushButton("—")
        self._ctcss_send_btn.setToolTip(_("Send current CTCSS tone to rig"))
        self._ctcss_send_btn.clicked.connect(self._on_ctcss_send)
        self._ctcss_send_btn.setVisible(False)
        self._ctcss_activate_btn = QPushButton(_("74.4 Hz (Activation)"))
        self._ctcss_activate_btn.setToolTip(_("SO-50: activate 10-minute timer with 74.4 Hz tone"))
        self._ctcss_activate_btn.clicked.connect(self._on_ctcss_activate)
        self._ctcss_activate_btn.setVisible(False)
        mode_ctcss_layout.addWidget(self._ctcss_label)
        mode_ctcss_layout.addWidget(self._ctcss_send_btn)
        mode_ctcss_layout.addWidget(self._ctcss_activate_btn)
        mode_ctcss_layout.addStretch()
        freq_form.addRow(_("Mode:"), mode_ctcss_row)

        layout.addWidget(freq_group)

        # ── Rotator ────────────────────────────────────────────────────
        # A single-row QHBoxLayout (not QFormLayout) so Qt vertically centers
        # the AZ/EL row within the group box — QFormLayout instead anchors
        # its (single) row to the top and pads leftover height underneath,
        # which looked lopsided once Recording (below) forced this box to
        # match its sibling's taller height.
        rot_group = QGroupBox(_("Rotator"))
        rot_layout = QHBoxLayout(rot_group)
        rot_layout.setContentsMargins(4, 2, 4, 2)
        rot_layout.setSpacing(4)
        self._rot_az_label = QLabel("—")
        self._rot_el_label = QLabel("—")
        self._rot_az_label.setMinimumWidth(60)
        rot_layout.addWidget(QLabel(_("AZ:")))
        rot_layout.addWidget(self._rot_az_label)
        rot_layout.addSpacing(20)
        rot_layout.addWidget(QLabel("EL:"))
        rot_layout.addWidget(self._rot_el_label)
        rot_layout.addStretch()

        # Recording group sits beside Rotator (half width each) — rig audio
        # (FM/SSB voice from the Sound Card input) recorded to MP3.
        rec_group = self._build_recording_group()
        rot_rec_row = QHBoxLayout()
        rot_rec_row.setSpacing(4)
        rot_rec_row.addWidget(rot_group, 1)
        rot_rec_row.addWidget(rec_group, 1)
        layout.addLayout(rot_rec_row)

        # ── Status ─────────────────────────────────────────────────────
        status_group = QGroupBox(_("Status"))
        status_form = QFormLayout(status_group)
        status_form.setContentsMargins(4, 2, 4, 2)
        status_form.setSpacing(3)
        status_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._rig1_status_label = QLabel(_("Not configured"))
        self._rig2_status_label = QLabel(_("Not configured"))
        self._rot_status_label = QLabel(_("Not configured"))

        # Rig 1 status + Connect Rig 1 on same row
        rig1_row = QHBoxLayout()
        rig1_row.setSpacing(6)
        self._connect_rig1_btn = QPushButton(_("Connect Rig 1"))
        self._connect_rig1_btn.clicked.connect(self._on_connect_rig1)
        rig1_row.addWidget(self._rig1_status_label)
        rig1_row.addStretch()
        rig1_row.addWidget(self._connect_rig1_btn)
        status_form.addRow(_("Rig 1:"), rig1_row)

        # Rig 2 status + Connect Rig 2 on same row
        rig2_row = QHBoxLayout()
        rig2_row.setSpacing(6)
        self._connect_rig2_btn = QPushButton(_("Connect Rig 2"))
        self._connect_rig2_btn.clicked.connect(self._on_connect_rig2)
        rig2_row.addWidget(self._rig2_status_label)
        rig2_row.addStretch()
        rig2_row.addWidget(self._connect_rig2_btn)
        status_form.addRow(_("Rig 2:"), rig2_row)

        # Rotator status + Connect Rotator + South Init on same row
        rot_ctrl_row = QHBoxLayout()
        rot_ctrl_row.setSpacing(6)
        self._connect_rot_btn = QPushButton(_("Connect Rotator"))
        self._connect_rot_btn.clicked.connect(self._on_connect_rotator)
        self._south_init_cb = QCheckBox(_("South Init"))
        self._south_init_cb.setToolTip(
            _(
                "Rotator starts facing south (180°). "
                "AZ is offset by 180° so the 0/360° wrap is avoided."
            )
        )
        self._south_init_cb.toggled.connect(self.south_init_changed.emit)
        rot_ctrl_row.addWidget(self._rot_status_label)
        rot_ctrl_row.addStretch()
        rot_ctrl_row.addWidget(self._connect_rot_btn)
        rot_ctrl_row.addWidget(self._south_init_cb)
        status_form.addRow(_("Rotator:"), rot_ctrl_row)

        # Cycle
        cycle_row = QHBoxLayout()
        self._cycle_spin = QSpinBox()
        self._cycle_spin.setRange(10, 10000)
        self._cycle_spin.setSingleStep(10)
        self._cycle_spin.setValue(1000)
        self._cycle_spin.valueChanged.connect(lambda v: self.cycle_changed.emit(v))
        cycle_row.addWidget(self._cycle_spin)
        cycle_row.addWidget(QLabel(_("msec")))
        cycle_row.addStretch()
        status_form.addRow(_("Cycle:"), cycle_row)

        layout.addWidget(status_group)

        # ── Autotrack indicator (compact) ─────────────────────────────
        at_group = QGroupBox(_("Autotrack"))
        at_h = QHBoxLayout(at_group)
        at_h.setContentsMargins(6, 2, 6, 2)
        self._at_indicator = QLabel(_("OFF"))
        self._at_indicator.setStyleSheet("color: gray; font-weight: bold;")
        at_h.addWidget(self._at_indicator)
        at_h.addStretch()
        layout.addWidget(at_group)

        layout.addStretch()

        self._update_rig1_status()
        self._update_rig2_status()
        self._update_rot_status()

    def _build_recording_group(self) -> QGroupBox:
        """Build the rig-audio (MP3) recording panel shown beside Rotator.

        Records whatever PCM the Sound Card input device delivers (the rig's
        AF output — FM/SSB voice, SSTV tones, etc.), via the same shared
        AudioDeviceManager RX path used by CW Decoder/FT4/SSTV, and the same
        AudioRecorder (lameenc MP3) SDR Control uses for its own REC button.
        """
        grp = QGroupBox(_("Recording"))
        rec_layout = QHBoxLayout(grp)
        rec_layout.setContentsMargins(4, 2, 4, 2)
        rec_layout.setSpacing(4)

        self._audio_rec_btn = QPushButton(_("● REC"))
        self._audio_rec_btn.setStyleSheet("color: red; font-weight: bold;")
        self._audio_rec_btn.setEnabled(LAMEENC_AVAILABLE)
        if not LAMEENC_AVAILABLE:
            self._audio_rec_btn.setToolTip(_("lameenc not installed — pip install lameenc"))
        self._audio_rec_btn.clicked.connect(self._start_audio_recording)

        self._audio_stop_rec_btn = QPushButton(_("■ STOP"))
        self._audio_stop_rec_btn.setEnabled(False)
        self._audio_stop_rec_btn.clicked.connect(self._stop_audio_recording)

        self._audio_rec_status_label = QLabel("")
        self._audio_rec_status_label.setStyleSheet("color: gray; font-size: 10px;")
        self._audio_rec_status_label.setMinimumWidth(40)

        self._open_audio_folder_btn = QPushButton(_("📁"))
        self._open_audio_folder_btn.setToolTip(_("Open audio recordings folder in file manager"))
        self._open_audio_folder_btn.setFixedWidth(28)
        self._open_audio_folder_btn.clicked.connect(self._open_audio_folder)

        rec_layout.addWidget(self._audio_rec_btn)
        rec_layout.addWidget(self._audio_stop_rec_btn)
        rec_layout.addWidget(self._audio_rec_status_label)
        rec_layout.addStretch()
        rec_layout.addWidget(self._open_audio_folder_btn)
        return grp

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_satellite(self, norad: int, name: str) -> None:
        """Set the selected satellite."""
        self._sat_name_label.setText(name)
        self._norad_label.setText(str(norad))

    def clear_satellite(self) -> None:
        """Clear satellite info and frequency display."""
        self._sat_name_label.setText("—")
        self._norad_label.setText("—")
        self._xpdr_combo.blockSignals(True)
        self._xpdr_combo.clear()
        self._xpdr_combo.setEnabled(False)
        self._transmitters = []
        self._xpdr_combo.blockSignals(False)
        self._clear_frequency()
        self.update_ctcss(None, None)
        self._sync_offset_display(None)

    def current_transmitter(self) -> dict[str, Any] | None:
        """Return the currently selected transponder dict, or None if none selected."""
        idx = self._xpdr_combo.currentIndex()
        if 0 <= idx < len(self._transmitters):
            return self._transmitters[idx]
        return None

    def set_transmitters(
        self,
        transmitters: list[dict[str, Any]],
        default_index: int = 0,
    ) -> None:
        """Populate the transponder combo box and apply the default selection.

        The first item is the default (caller is expected to sort by priority beforehand).
        Emits transmitter_changed with the default selection.
        """
        self._transmitters = transmitters
        self._xpdr_combo.blockSignals(True)
        self._xpdr_combo.clear()
        for i, xpdr in enumerate(transmitters):
            self._xpdr_combo.addItem(self._xpdr_label(xpdr))
            bg = self._xpdr_status_bg(xpdr)
            if bg is not None:
                self._xpdr_combo.setItemData(i, bg, Qt.ItemDataRole.BackgroundRole)
        self._xpdr_combo.setEnabled(len(transmitters) > 0)
        if transmitters:
            idx = max(0, min(default_index, len(transmitters) - 1))
            self._xpdr_combo.setCurrentIndex(idx)
        self._xpdr_combo.blockSignals(False)
        selected = transmitters[default_index] if transmitters else None
        self._sync_offset_display(selected)
        self.transmitter_changed.emit(selected)

    def update_doppler(
        self,
        downlink_nominal_hz: float | None,
        downlink_corrected_hz: float | None,
        downlink_shift_hz: float | None,
        uplink_nominal_hz: float | None,
        uplink_corrected_hz: float | None,
        uplink_shift_hz: float | None,
        mode: str | None = None,
        ctcss_hz: float | None = None,
    ) -> None:
        """Update the Doppler-corrected frequency and status label."""
        if downlink_corrected_hz is not None:
            self._downlink_label.setText(f"{downlink_corrected_hz / 1e6:.6f} MHz")
            if downlink_shift_hz is not None:
                sign = "+" if downlink_shift_hz >= 0 else ""
                self._downlink_doppler_label.setText(f"{sign}{downlink_shift_hz:.0f} Hz")
            else:
                self._downlink_doppler_label.setText("—")
        else:
            self._downlink_label.setText("—")
            self._downlink_doppler_label.setText("—")

        if uplink_corrected_hz is not None:
            self._uplink_label.setText(f"{uplink_corrected_hz / 1e6:.6f} MHz")
            if uplink_shift_hz is not None:
                sign = "+" if uplink_shift_hz >= 0 else ""
                self._uplink_doppler_label.setText(f"{sign}{uplink_shift_hz:.0f} Hz")
            else:
                self._uplink_doppler_label.setText("—")
        else:
            self._uplink_label.setText("—")
            self._uplink_doppler_label.setText("—")

        self._mode_label.setText(mode if mode else "—")
        display_hz = ctcss_hz if (ctcss_hz and ctcss_hz > 0) else None
        self._ctcss_label.setText(f"{display_hz:.1f} Hz" if display_hz else "—")

    def update_rotator(self, state: RotatorState | None) -> None:
        """Update the current rotator position."""
        if state is None:
            self._rot_az_label.setText("—")
            self._rot_el_label.setText("—")
        else:
            self._rot_az_label.setText(f"{state.azimuth_deg:.1f}°")
            self._rot_el_label.setText(f"{state.elevation_deg:.1f}°")

    def set_rig(self, rig: RigController | None) -> None:
        """Set the Rig 1 controller (kept for backward compatibility)."""
        self.set_rig1(rig)

    def set_rig1(self, rig: RigController | None) -> None:
        """Set the Rig 1 controller and refresh the status display."""
        self._rig1 = rig
        self._update_rig1_status()

    def set_rig2(self, rig: RigController | None) -> None:
        """Set the Rig 2 controller and refresh the status display."""
        self._rig2 = rig
        self._update_rig2_status()

    def set_rotator(self, rotator: RotatorController | None) -> None:
        """Set the rotator controller."""
        self._rotator = rotator
        self._update_rot_status()

    def set_sdr_control(self, sdr_control: Any) -> None:
        """Attach the MainWindow-owned SdrControlWidget instance.

        CW/FT4/Q65/AX100 Digi look up the live SDR pipeline via
        getattr(self._radio_control, "_sdr_control", None) — this is what
        makes that attribute actually exist. Without a call to this
        setter, that lookup always silently returned None (RadioControlWidget
        never declared or set _sdr_control itself), so those tabs' SDR
        audio input never worked at all: _connect_sdr_audio() returned
        immediately at its "sdr_ctrl is None" check, before ever reaching
        pipeline.audio_ready.connect()/request_audio() (GitHub Issue #12
        follow-up — this is the real reason CW Decoder never decoded
        anything over SDR, independent of the two earlier, real but
        secondary bugs already fixed in v0.2.30/v0.2.31: SDRPipeline
        reference staleness across reconnects, and audio_ready being
        gated behind SDR Control's own Start Audio toggle).
        """
        self._sdr_control = sdr_control

    def set_db_connection(self, conn: sqlite3.Connection) -> None:
        """Attach the MainWindow-owned DB connection.

        Needed so the rig-audio REC button can look up the Sound Card input
        device index (``soundcard_settings``) at record time — see
        _start_audio_recording().
        """
        self._db_conn = conn

    # ------------------------------------------------------------------ #
    # Autotrack public API
    # ------------------------------------------------------------------ #

    def populate_autotrack_lists(self, lists: list[dict[str, int | str]]) -> None:
        """No-op kept for API compatibility; list management moved to Autotrack/Record dialog."""

    def set_autotrack_indicator(self, enabled: bool) -> None:
        """Update the compact Autotrack ON/OFF indicator in the Radio Control tab."""
        if enabled:
            self._at_indicator.setText(_("ON"))
            self._at_indicator.setStyleSheet("color: #2ecc71; font-weight: bold;")
        else:
            self._at_indicator.setText(_("OFF"))
            self._at_indicator.setStyleSheet("color: gray; font-weight: bold;")

    def toggle_lock(self) -> None:
        """Flip the Lock button's checked state, same as a real click (used
        by MainWindow's Ctrl+L hotkey -- see GitHub Issue #14)."""
        self._lock_btn.setChecked(not self._lock_btn.isChecked())

    def set_south_init(self, checked: bool) -> None:
        """Set the South Init checkbox without emitting south_init_changed."""
        self._south_init_cb.blockSignals(True)
        self._south_init_cb.setChecked(checked)
        self._south_init_cb.blockSignals(False)

    def set_cycle(self, ms: int) -> None:
        """Set the Cycle spin box value externally without emitting a signal."""
        self._cycle_spin.blockSignals(True)
        self._cycle_spin.setValue(max(10, min(10000, ms)))
        self._cycle_spin.blockSignals(False)

    def refresh_status(self) -> None:
        """Update all connection status displays (called by timer)."""
        self._update_rig1_status()
        self._update_rig2_status()
        self._update_rot_status()
        if self._rotator is not None and self._rotator.is_connected:
            self.update_rotator(self._rotator.get_position())

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _clear_frequency(self) -> None:
        for label in (
            self._downlink_label,
            self._downlink_doppler_label,
            self._uplink_label,
            self._uplink_doppler_label,
            self._mode_label,
            self._ctcss_label,
        ):
            label.setText("—")

    @staticmethod
    def _xpdr_label(xpdr: dict[str, Any]) -> str:
        """Generate a display label for the transponder combo box."""
        dl = xpdr.get("downlink_low")
        dl_str = f"{dl / 1e6:.3f} MHz" if dl else "—"
        xtype = xpdr.get("type", "")
        desc = xpdr.get("description", "?")
        return f"{desc}  [{dl_str}  {xtype}]"

    @staticmethod
    def _xpdr_status_bg(xpdr: dict[str, Any]) -> QColor | None:
        """Return the combo item background color for a non-active SATNOGS
        transmitter, or None for active/manual/community entries."""
        if xpdr.get("satnogs_status") == "invalid":
            return _XPDR_INVALID_BG
        if not xpdr.get("alive", True):
            return _XPDR_INACTIVE_BG
        return None

    def update_ctcss(self, tone_hz: float | None, activation_hz: float | None) -> None:
        """Update CTCSS button state from the current transponder and satellite DB info."""
        self._current_ctcss_hz = tone_hz if (tone_hz and tone_hz > 0) else None
        self._ctcss_activation_hz = activation_hz if (activation_hz and activation_hz > 0) else None
        self._update_ctcss_buttons()

    def _update_ctcss_buttons(self) -> None:
        """Show/hide and label CTCSS buttons based on current tone and activation tone."""
        has_tone = self._current_ctcss_hz is not None
        self._ctcss_send_btn.setVisible(has_tone)
        if has_tone:
            self._ctcss_send_btn.setText(f"{self._current_ctcss_hz:.1f} Hz")
        has_activation = self._ctcss_activation_hz is not None
        self._ctcss_activate_btn.setVisible(has_activation)
        if has_activation:
            self._ctcss_activate_btn.setText(f"{self._ctcss_activation_hz:.1f} Hz (Activation)")

    def _on_xpdr_changed(self, index: int) -> None:
        if 0 <= index < len(self._transmitters):
            xpdr = self._transmitters[index]
            self._sync_offset_display(xpdr)
            self.transmitter_changed.emit(xpdr)
            self._check_comms_auto_open(xpdr)

    def _sync_offset_display(self, xpdr: dict[str, Any] | None) -> None:
        """Reflect *xpdr*'s saved rx_offset_hz in the Offset spinbox without
        emitting rx_offset_changed (blockSignals) -- this is a display refresh
        driven by satellite/transponder selection, not a user edit."""
        self._offset_spin.blockSignals(True)
        self._offset_spin.setValue(int(xpdr.get("rx_offset_hz") or 0) if xpdr else 0)
        self._offset_spin.blockSignals(False)
        self._offset_spin.setEnabled(xpdr is not None)

    def _on_offset_spin_changed(self, value: int) -> None:
        self.rx_offset_changed.emit(float(value))

    def _check_comms_auto_open(self, xpdr: Any) -> None:
        """Store the comms tab to open once the rig connects (not immediately).

        Classification is delegated to comms.mode_detection so the same
        rules also drive the Comms Quick Panel's per-tab satellite filter.
        """
        mode = (xpdr.get("mode") or "").upper()
        if is_sstv_transmitter(xpdr):
            self._pending_comms_tab = "sstv"
        elif is_aprs_transmitter(xpdr):
            self._pending_comms_tab = "aprs"
        elif is_ft4_transmitter(xpdr):
            self._pending_comms_tab = "ft4"
        elif is_cw_transmitter(xpdr):
            # CW is RX-only — emit immediately (no rig connection required)
            self.cw_transponder_selected.emit()
            self._pending_comms_tab = None
        else:
            self._pending_comms_tab = None

        # METEOR LRPT/HRPT: emit immediately (no rig connection required)
        from comms.meteor.satdump import METEOR_NORAD_IDS

        norad = xpdr.get("norad_cat_id") or 0
        if norad in METEOR_NORAD_IDS and (mode == "LRPT" or mode == "HRPT"):
            dl_hz = xpdr.get("downlink_low") or 0
            self.meteor_transponder_selected.emit(int(norad), int(dl_hz))

    def _update_rig1_status(self) -> None:
        """Refresh the Rig 1 status row and button label."""
        if self._rig1 is None:
            self._rig1_status_label.setText(_("Not configured"))
            self._rig1_status_label.setStyleSheet("color: gray;")
            self._connect_rig1_btn.setEnabled(False)
            return
        self._connect_rig1_btn.setEnabled(True)
        state = self._rig1.state
        if state == RigState.CONNECTED:
            if getattr(self._rig1, "is_sdr", False):
                self._rig1_status_label.setText(_("SDR: Connected"))
                self._rig1_status_label.setStyleSheet("color: #00dcff; font-weight: bold;")
            else:
                info = self._rig1.get_rig_info()
                name = info.model_name if info else "—"
                self._rig1_status_label.setText(f"{_('Connected')}: {name}")
                self._rig1_status_label.setStyleSheet("color: green;")
            self._connect_rig1_btn.setText(_("Disconnect Rig 1"))
        elif state == RigState.CONNECTING:
            self._rig1_status_label.setText(_("Connecting..."))
            self._rig1_status_label.setStyleSheet("color: orange;")
            self._connect_rig1_btn.setText(_("Disconnect Rig 1"))
        elif state == RigState.ERROR:
            self._rig1_status_label.setText(_("Error"))
            self._rig1_status_label.setStyleSheet("color: red;")
            self._connect_rig1_btn.setText(_("Retry"))
        else:
            self._rig1_status_label.setText(_("Disconnected"))
            self._rig1_status_label.setStyleSheet("color: gray;")
            self._connect_rig1_btn.setText(_("Connect Rig 1"))

    def _update_rig2_status(self) -> None:
        """Refresh the Rig 2 status row and button label."""
        if self._rig2 is None:
            self._rig2_status_label.setText(_("Not configured"))
            self._rig2_status_label.setStyleSheet("color: gray;")
            self._connect_rig2_btn.setEnabled(False)
            return
        self._connect_rig2_btn.setEnabled(True)
        state = self._rig2.state
        if state == RigState.CONNECTED:
            if getattr(self._rig2, "is_sdr", False):
                self._rig2_status_label.setText(_("SDR: Connected"))
                self._rig2_status_label.setStyleSheet("color: #00dcff; font-weight: bold;")
            else:
                info = self._rig2.get_rig_info()
                name = info.model_name if info else "—"
                self._rig2_status_label.setText(f"{_('Connected')}: {name}")
                self._rig2_status_label.setStyleSheet("color: green;")
            self._connect_rig2_btn.setText(_("Disconnect Rig 2"))
        elif state == RigState.CONNECTING:
            self._rig2_status_label.setText(_("Connecting..."))
            self._rig2_status_label.setStyleSheet("color: orange;")
            self._connect_rig2_btn.setText(_("Disconnect Rig 2"))
        elif state == RigState.ERROR:
            self._rig2_status_label.setText(_("Error"))
            self._rig2_status_label.setStyleSheet("color: red;")
            self._connect_rig2_btn.setText(_("Retry"))
        else:
            self._rig2_status_label.setText(_("Disconnected"))
            self._rig2_status_label.setStyleSheet("color: gray;")
            self._connect_rig2_btn.setText(_("Connect Rig 2"))

    def _update_rot_status(self) -> None:
        if self._rotator is None:
            self._rot_status_label.setText(_("Not configured"))
            self._rot_status_label.setStyleSheet("color: gray;")
            self._connect_rot_btn.setEnabled(False)
            return
        self._connect_rot_btn.setEnabled(True)
        if self._rotator.is_connected:
            self._rot_status_label.setText(_("Connected"))
            self._rot_status_label.setStyleSheet("color: green;")
            self._connect_rot_btn.setText(_("Disconnect Rotator"))
        else:
            self._rot_status_label.setText(_("Disconnected"))
            self._rot_status_label.setStyleSheet("color: gray;")
            self._connect_rot_btn.setText(_("Connect Rotator"))

    @staticmethod
    def _rigctld_running() -> bool:
        """Return True if a rigctld process is currently running."""
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq rigctld.exe", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    # Without this, launching tasklist.exe from this
                    # windowed (console-less) PyInstaller build briefly
                    # flashes a console window on screen every time Connect
                    # is pressed in Direct mode.
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return "rigctld" in result.stdout.lower()
            else:
                result = subprocess.run(
                    ["pgrep", "-x", "rigctld"],
                    capture_output=True,
                    timeout=3,
                )
                return result.returncode == 0
        except Exception:
            return False

    def _warn_rigctld_if_direct(self, rig: RigController) -> bool:
        """Show a warning dialog if rigctld is running while connecting in Direct mode.

        Returns True when the caller should proceed with connect(), False when
        the user cancelled.  Handles the "stopped, retry" loop internally.
        """
        if not isinstance(rig, HamlibDirectController):
            return True
        while self._rigctld_running():
            msg = QMessageBox(self)
            msg.setWindowTitle(_("rigctld is running"))
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText(
                _(
                    "rigctld is running and sharing the same serial port.\n\n"
                    "Direct mode and rigctld cannot share the port reliably — "
                    "frequency and mode writes may be corrupted.\n\n"
                    "Stop rigctld before connecting:\n"
                    "  Normal launch:   pkill rigctld\n"
                    "  systemd service: sudo systemctl stop <service-name>"
                )
            )
            retry_btn = msg.addButton(_("I stopped it, retry"), QMessageBox.ButtonRole.AcceptRole)
            proceed_btn = msg.addButton(_("Connect anyway"), QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton(_("Cancel"), QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked is retry_btn:
                continue  # re-check
            return clicked is proceed_btn
        return True

    def _on_connect_rig1(self) -> None:
        # TEMP diagnostic logging for GitHub issue #10 (RTL-SDR Blog V4 on
        # Windows: Connect appears to do nothing, no [RTL-SDR direct] log
        # ever appears). Remove once the root cause is confirmed from a
        # user's log.
        logger.info(
            "[RigConnect diag] _on_connect_rig1 clicked: rig1=%s is_sdr=%s is_connected=%s",
            type(self._rig1).__name__ if self._rig1 is not None else None,
            getattr(self._rig1, "is_sdr", False),
            self._rig1.is_connected if self._rig1 is not None else None,
        )
        if self._rig1 is None:
            logger.info("[RigConnect diag] _on_connect_rig1: rig1 is None, returning")
            return
        if self._rig1.is_connected:
            logger.info("[RigConnect diag] _on_connect_rig1: already connected, disconnecting")
            self._rig1.disconnect()
            self.rig_disconnected.emit()
            self._update_rig1_status()
            return
        if not self._warn_rigctld_if_direct(self._rig1):
            logger.info(
                "[RigConnect diag] _on_connect_rig1: _warn_rigctld_if_direct blocked connect"
            )
            return
        # Disable button immediately to prevent queued double-clicks during connect.
        # Direct-mode rigs (IC-9100 etc.) can block for several seconds on _port_lock
        # while a concurrent CI-V sequence holds the serial port.
        self._connect_rig1_btn.setEnabled(False)
        self._rig1_status_label.setText(_("Connecting..."))
        self._rig1_status_label.setStyleSheet("color: orange;")

        rig = self._rig1

        def _do() -> None:
            logger.info("[RigConnect diag] connect thread started, calling rig.connect()")
            try:
                rig.connect()
            except Exception:
                logger.exception("[RigConnect diag] rig.connect() raised")
            logger.info(
                "[RigConnect diag] rig.connect() returned, is_connected=%s", rig.is_connected
            )
            self._rig1_connect_done.emit(rig.is_connected)

        logger.info("[RigConnect diag] starting connect thread")
        threading.Thread(target=_do, daemon=True).start()

    def _finish_rig1_connect(self, success: bool) -> None:
        """Called on the UI thread when the background connect() finishes."""
        self._connect_rig1_btn.setEnabled(True)
        if success:
            self.rig_connected.emit()
            if self._pending_comms_tab == "sstv":
                self.sstv_transponder_selected.emit()
            elif self._pending_comms_tab == "aprs":
                self.aprs_transponder_selected.emit()
            elif self._pending_comms_tab == "ft4":
                self.ft4_transponder_selected.emit()
            self._pending_comms_tab = None
        self._update_rig1_status()

    def _on_connect_rig2(self) -> None:
        if self._rig2 is None:
            return
        if self._rig2.is_connected:
            self._rig2.disconnect()
            self.rig2_disconnected.emit()
        else:
            if not self._warn_rigctld_if_direct(self._rig2):
                return
            self._rig2.connect()
            if self._rig2.is_connected:
                self.rig2_connected.emit()
        self._update_rig2_status()

    def update_cw_button(self, dl_mode: str, ul_mode: str) -> None:
        """Show/configure the CW toggle button when dl_mode is USB or LSB.

        dl_mode / ul_mode are the already-computed transponder modes (invert
        already applied by the caller).  The button is hidden for FM and other
        non-linear modes.

        CW mode mapping:
          USB/SSB DL → CW   (CW-Upper); LSB DL → CW-R (CW-Lower)
          ul_mode USB/SSB   → CW;       ul_mode LSB    → CW-R
        """
        self._cw_mode_active = False
        self._orig_dl_mode = dl_mode
        self._orig_ul_mode = ul_mode

        _CW_MAP: dict[str, str] = {"USB": "CW", "SSB": "CW", "LSB": "CW-R"}
        cw_dl = _CW_MAP.get(dl_mode, "")
        cw_ul = _CW_MAP.get(ul_mode, "")
        if not cw_dl:
            self._cw_btn.setVisible(False)
            return

        self._cw_dl_mode = cw_dl
        self._cw_ul_mode = cw_ul if cw_ul else cw_dl
        # Button label shows CW subtype matching DL VFO (what you receive on)
        btn_label = "CW-U" if cw_dl == "CW" else "CW-L"
        self._cw_btn.setText(btn_label)
        self._cw_btn.setStyleSheet("")
        self._cw_btn.setVisible(True)

    def _on_cw_toggle(self) -> None:
        """Toggle between CW mode and the original transponder mode."""
        if not self._cw_mode_active:
            self.cw_mode_requested.emit(self._cw_dl_mode, self._cw_ul_mode)
            self._cw_mode_active = True
            # Show original mode name so user can revert with next click
            revert_label = self._orig_dl_mode if self._orig_dl_mode else "SSB"
            self._cw_btn.setText(revert_label)
            self._cw_btn.setStyleSheet("font-weight: bold; color: #f39c12;")
        else:
            self.cw_mode_requested.emit(self._orig_dl_mode, self._orig_ul_mode)
            self._cw_mode_active = False
            cw_label = "CW-U" if self._cw_dl_mode == "CW" else "CW-L"
            self._cw_btn.setText(cw_label)
            self._cw_btn.setStyleSheet("")

    def update_dmode_button(self, dl_mode: str, ul_mode: str) -> None:
        """Show/configure the DATA (-D) toggle button when dl_mode is USB or LSB.

        dl_mode / ul_mode are the already-computed transponder modes (invert
        already applied by the caller), same convention as update_cw_button().
        The button is hidden for FM, CW, and modes already ending in "-D".

        Some radios (e.g. Icom) require a "-D" DATA mode (LSB-D/USB-D) to
        route computer/USB audio to the TX output instead of the mic when
        running a digital mode (FT4 etc.) over a linear SSB transponder
        (GitHub Issue #16).
        """
        self._dmode_active = False
        self._dmode_orig_dl_mode = dl_mode
        self._dmode_orig_ul_mode = ul_mode

        _D_MODE_MAP: dict[str, str] = {"USB": "USB-D", "SSB": "USB-D", "LSB": "LSB-D"}
        d_dl = _D_MODE_MAP.get(dl_mode, "")
        d_ul = _D_MODE_MAP.get(ul_mode, "")
        if not d_dl:
            self._dmode_btn.setVisible(False)
            return

        self._dmode_dl_mode = d_dl
        self._dmode_ul_mode = d_ul if d_ul else d_dl
        self._dmode_btn.setText(_("DATA"))
        self._dmode_btn.setStyleSheet("")
        self._dmode_btn.setVisible(True)

    def _on_dmode_toggle(self) -> None:
        """Toggle between DATA (-D) mode and the original transponder mode."""
        if not self._dmode_active:
            self.dmode_requested.emit(self._dmode_dl_mode, self._dmode_ul_mode)
            self._dmode_active = True
            # Show original mode name so user can revert with next click
            revert_label = self._dmode_orig_dl_mode if self._dmode_orig_dl_mode else "SSB"
            self._dmode_btn.setText(revert_label)
            self._dmode_btn.setStyleSheet("font-weight: bold; color: #f39c12;")
        else:
            self.dmode_requested.emit(self._dmode_orig_dl_mode, self._dmode_orig_ul_mode)
            self._dmode_active = False
            self._dmode_btn.setText(_("DATA"))
            self._dmode_btn.setStyleSheet("")

    def _on_ctcss_send(self) -> None:
        if self._current_ctcss_hz is not None:
            self._ctcss_label.setText(f"{self._current_ctcss_hz:.1f} Hz")
            self.ctcss_send_requested.emit(self._current_ctcss_hz)

    def _on_ctcss_activate(self) -> None:
        """Transmit the satellite's activation tone (e.g. SO-50: 74.4 Hz)."""
        if self._ctcss_activation_hz is not None:
            self._ctcss_label.setText(f"{self._ctcss_activation_hz:.1f} Hz")
        self.ctcss_activate_requested.emit()

    def _on_connect_rotator(self) -> None:
        if self._rotator is None:
            return
        if self._rotator.is_connected:
            self._rotator.disconnect()
        else:
            if self._rotator.connect():
                self.rotator_connected.emit()
        self._update_rot_status()

    # ------------------------------------------------------------------ #
    # Rig audio recording (MP3, via Sound Card input)
    # ------------------------------------------------------------------ #

    _AUDIO_OWNER = "Radio Control"

    def _load_sound_card_input_device(self) -> int | None:
        """Read the configured Sound Card input device index, fresh from the DB.

        Not cached — RadioControlWidget is a resident tab (built once at
        startup), so re-reading on every REC press means a Sound Card change
        made mid-session via Rig Settings takes effect on the next recording
        without needing to reload anything.
        """
        if self._db_conn is None:
            return None
        with contextlib.suppress(Exception):
            row = self._db_conn.execute(
                "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
            ).fetchone()
            if row:
                val = json.loads(row[0]).get("input_device_index")
                if val is not None:
                    return int(val)
        return None

    def _start_audio_recording(self) -> None:
        """Start recording the rig's audio (Sound Card input) to MP3."""
        if not LAMEENC_AVAILABLE or self._audio_recorder.is_active:
            return
        device = self._load_sound_card_input_device()
        if device is None:
            QMessageBox.warning(
                self,
                _("Recording"),
                _("Sound Card not configured — open Rig Settings > Sound Card"),
            )
            return
        try:
            get_audio_device_manager().acquire_input(
                self._AUDIO_OWNER, device, 48_000, self._audio_recorder.put_pcm
            )
        except Exception as exc:
            QMessageBox.warning(self, _("Recording"), _("Audio open error: {exc}").format(exc=exc))
            return
        self._rec_in_device = device
        self._rec_audio_active = True

        try:
            norad = int(self._norad_label.text())
        except ValueError:
            norad = 0
        sat_name = self._sat_name_label.text()
        if sat_name in ("", "—"):
            sat_name = "unknown"

        file_path = self._audio_recorder.start(norad=norad, sat_name=sat_name)
        self._audio_rec_btn.setEnabled(False)
        self._audio_stop_rec_btn.setEnabled(True)
        self._audio_rec_status_label.setText("00:00")
        self._audio_rec_status_label.setToolTip(str(file_path))
        self._audio_rec_timer.start()

    def _stop_audio_recording(self) -> None:
        """Stop MP3 recording and release the shared Sound Card input."""
        if not self._audio_recorder.is_active:
            return
        if self._rec_audio_active:
            with contextlib.suppress(Exception):
                get_audio_device_manager().release_input(self._AUDIO_OWNER, self._rec_in_device)
            self._rec_audio_active = False
        self._audio_recorder.stop()
        self._audio_rec_timer.stop()
        self._audio_rec_btn.setEnabled(LAMEENC_AVAILABLE)
        self._audio_stop_rec_btn.setEnabled(False)
        self._audio_rec_status_label.setText("")
        self._audio_rec_status_label.setToolTip("")

    def _update_audio_rec_status(self) -> None:
        """Update the elapsed-time label while recording (mm:ss)."""
        if not self._audio_recorder.is_active:
            return
        elapsed = int(self._audio_recorder.elapsed_seconds)
        self._audio_rec_status_label.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

    def _open_audio_folder(self) -> None:
        """Open the audio recordings save directory in the OS file manager."""
        self._audio_save_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._audio_save_dir)))

    def closeEvent(self, event: Any) -> None:
        """Stop recording and release the shared audio device on app exit.

        MainWindow.closeEvent() calls .close() on every tab generically
        (including resident ones like this), so this fires reliably even
        though RadioControlWidget is never individually closed by the user.
        """
        self._stop_audio_recording()
        super().closeEvent(event)
