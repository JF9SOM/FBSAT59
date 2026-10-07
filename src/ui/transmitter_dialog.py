"""
Manual transmitter add/edit dialog.

Accepts NORAD ID, frequency, mode, and CTCSS tone and saves to DB (manual_override=True).
Pass existing to enter edit mode.
"""

from __future__ import annotations

import asyncio
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from data.transmitter_manager import TransmitterManager
from i18n import _

_MODES: list[str] = [
    "FM",
    "USB",
    "LSB",
    "USB-D",
    "LSB-D",
    "FM-D",
    "CW",
    "CW-R",
    "DIGITALVOICE",
    "BPSK",
    "AFSK",
    "Other",
]
_TYPES: list[str] = ["Transmitter", "Transponder", "Transceiver", "Beacon"]
_CTCSS_TYPES: list[str] = ["", "CTCSS", "DCS"]

# Same range/step as the Radio Control Offset spinboxes.
_OFFSET_RANGE_HZ = 10000
_OFFSET_STEP_HZ = 10


class TransmitterDialog(QDialog):
    """Manual transmitter add/edit dialog."""

    def __init__(
        self,
        transmitter_manager: TransmitterManager,
        norad_cat_id: int | None = None,
        existing: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            transmitter_manager: Transmitter manager instance
            norad_cat_id:        Initial NORAD ID (defaults to 25544=ISS when None)
            existing:            Record to edit (add mode when None)
            parent:              Parent widget
        """
        super().__init__(parent)
        self._tm = transmitter_manager
        self._existing = existing
        self._edit_mode = existing is not None
        title = _("Edit Transmitter") if self._edit_mode else _("Add Transmitter")
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        self._build_ui()
        if self._edit_mode:
            self._prefill(existing)  # type: ignore[arg-type]
        elif norad_cat_id is not None:
            self._norad_spin.setValue(norad_cat_id)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Satellite
        sat_group = QGroupBox(_("Satellite"))
        sat_form = QFormLayout(sat_group)
        self._norad_spin = QSpinBox()
        self._norad_spin.setRange(1, 999999)
        self._norad_spin.setValue(25544)
        sat_form.addRow(_("NORAD ID:"), self._norad_spin)
        self._satnogs_norad_spin = QSpinBox()
        self._satnogs_norad_spin.setRange(0, 999999)
        self._satnogs_norad_spin.setValue(0)
        self._satnogs_norad_spin.setSpecialValueText(_("(same as above)"))
        sat_form.addRow(_("SatNOGS NORAD ID:"), self._satnogs_norad_spin)
        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText(_("e.g. ISS FM downlink"))
        sat_form.addRow(_("Description:"), self._desc_edit)
        layout.addWidget(sat_group)

        # Frequency (MHz input, converted to Hz internally)
        freq_group = QGroupBox(_("Frequency"))
        freq_form = QFormLayout(freq_group)

        self._dl_spin = QDoubleSpinBox()
        self._dl_spin.setRange(0.001, 10000.0)
        self._dl_spin.setDecimals(3)
        self._dl_spin.setSuffix(" MHz")
        self._dl_spin.setValue(145.800)
        freq_form.addRow(_("Downlink (MHz):"), self._dl_spin)

        self._dl_high_spin = QDoubleSpinBox()
        self._dl_high_spin.setRange(0.0, 10000.0)
        self._dl_high_spin.setDecimals(3)
        self._dl_high_spin.setSuffix(" MHz")
        self._dl_high_spin.setSpecialValueText(_("(none)"))
        freq_form.addRow(_("Downlink High (MHz):"), self._dl_high_spin)

        self._ul_spin = QDoubleSpinBox()
        self._ul_spin.setRange(0.0, 10000.0)
        self._ul_spin.setDecimals(3)
        self._ul_spin.setSuffix(" MHz")
        self._ul_spin.setSpecialValueText(_("(none)"))
        freq_form.addRow(_("Uplink (MHz):"), self._ul_spin)

        self._ul_high_spin = QDoubleSpinBox()
        self._ul_high_spin.setRange(0.0, 10000.0)
        self._ul_high_spin.setDecimals(3)
        self._ul_high_spin.setSuffix(" MHz")
        self._ul_high_spin.setSpecialValueText(_("(none)"))
        freq_form.addRow(_("Uplink High (MHz):"), self._ul_high_spin)

        # Persistent per-transponder offsets (Hz), applied by the app before
        # Doppler correction. See docs/doppler-tuning.md.
        self._dl_offset_spin = QSpinBox()
        self._dl_offset_spin.setRange(-_OFFSET_RANGE_HZ, _OFFSET_RANGE_HZ)
        self._dl_offset_spin.setSingleStep(_OFFSET_STEP_HZ)
        self._dl_offset_spin.setSuffix(" Hz")
        freq_form.addRow(_("Downlink Offset:"), self._dl_offset_spin)

        self._ul_offset_spin = QSpinBox()
        self._ul_offset_spin.setRange(-_OFFSET_RANGE_HZ, _OFFSET_RANGE_HZ)
        self._ul_offset_spin.setSingleStep(_OFFSET_STEP_HZ)
        self._ul_offset_spin.setSuffix(" Hz")
        freq_form.addRow(_("Uplink Offset:"), self._ul_offset_spin)

        self._eff_dl_label = QLabel()
        freq_form.addRow(_("Effective Downlink:"), self._eff_dl_label)
        self._eff_ul_label = QLabel()
        freq_form.addRow(_("Effective Uplink:"), self._eff_ul_label)

        for spin in (
            self._dl_spin,
            self._dl_high_spin,
            self._ul_spin,
            self._ul_high_spin,
        ):
            spin.valueChanged.connect(self._update_effective_labels)
        self._dl_offset_spin.valueChanged.connect(self._update_effective_labels)
        self._ul_offset_spin.valueChanged.connect(self._update_effective_labels)
        self._update_effective_labels()

        layout.addWidget(freq_group)

        # Mode and type
        mode_group = QGroupBox(_("Mode"))
        mode_form = QFormLayout(mode_group)

        self._type_combo = QComboBox()
        self._type_combo.addItems(_TYPES)
        mode_form.addRow(_("Type:"), self._type_combo)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(_MODES)
        mode_form.addRow(_("Mode (Downlink):"), self._mode_combo)

        self._invert_check = QCheckBox(_("Inverting transponder"))
        mode_form.addRow("", self._invert_check)

        layout.addWidget(mode_group)

        # CTCSS tone
        ctcss_group = QGroupBox(_("CTCSS / DCS Tone"))
        ctcss_form = QFormLayout(ctcss_group)

        self._ctcss_type_combo = QComboBox()
        self._ctcss_type_combo.addItems(_CTCSS_TYPES)
        ctcss_form.addRow(_("Tone type:"), self._ctcss_type_combo)

        self._ctcss_spin = QDoubleSpinBox()
        self._ctcss_spin.setRange(0.0, 9999.9)
        self._ctcss_spin.setDecimals(1)
        self._ctcss_spin.setSuffix(" Hz")
        self._ctcss_spin.setSpecialValueText(_("(none)"))
        ctcss_form.addRow(_("Tone (Hz):"), self._ctcss_spin)

        layout.addWidget(ctcss_group)

        # Notes
        notes_form = QFormLayout()
        self._notes_edit = QLineEdit()
        self._notes_edit.setPlaceholderText(_("Optional notes"))
        notes_form.addRow(_("Notes:"), self._notes_edit)
        layout.addLayout(notes_form)

        # Reset to SatNOGS official value: only meaningful when editing a row
        # that originated from SatNOGS. source is never changed by editing
        # (update_transmitter()'s allowed-columns set excludes uuid/source),
        # so source == "satnogs" stays a reliable marker of "there is a real
        # SatNOGS UUID to re-fetch" even after manual_override=1 protected a
        # bad edit from ever being auto-corrected (GitHub Issue #20 follow-up).
        if self._edit_mode and self._existing and self._existing.get("source") == "satnogs":
            self._reset_satnogs_btn = QPushButton(_("Reset to SatNOGS Official Value"))
            self._reset_satnogs_btn.clicked.connect(self._on_reset_to_satnogs)
            layout.addWidget(self._reset_satnogs_btn)

        # Overwrite protection
        self._overwrite_check = QCheckBox(
            _("Overwrite protection (prevent SATNOGS sync from overwriting)")
        )
        self._overwrite_check.setChecked(True)
        layout.addWidget(self._overwrite_check)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # Internal utilities
    # ------------------------------------------------------------------ #

    def _mhz_to_hz(self, mhz: float) -> int | None:
        """Convert MHz to Hz. Returns None for 0.0 (specialValue)."""
        if mhz <= 0.0:
            return None
        return int(round(mhz * 1_000_000))

    @staticmethod
    def _hz_to_mhz(hz: int | None) -> float:
        """Convert Hz to MHz. Returns 0.0 (specialValue) when hz is None."""
        if hz is None:
            return 0.0
        return hz / 1_000_000

    def _update_effective_labels(self) -> None:
        """Show the offset-corrected nominal frequencies (band centre when a
        high edge is set, matching what the app tunes to before Doppler)."""
        for label, low, high, offset in (
            (self._eff_dl_label, self._dl_spin, self._dl_high_spin, self._dl_offset_spin),
            (self._eff_ul_label, self._ul_spin, self._ul_high_spin, self._ul_offset_spin),
        ):
            low_hz = self._mhz_to_hz(low.value())
            if low_hz is None:
                label.setText("-")
                continue
            high_hz = self._mhz_to_hz(high.value())
            nominal = (low_hz + high_hz) / 2 if high_hz is not None else float(low_hz)
            label.setText(f"{(nominal + offset.value()) / 1_000_000:.6f} MHz")

    def _prefill(self, rec: dict[str, Any]) -> None:
        """Populate widgets with values from an existing record (edit mode)."""
        self._norad_spin.setValue(rec.get("norad_cat_id", 25544))
        self._norad_spin.setEnabled(False)
        self._satnogs_norad_spin.setEnabled(False)
        self._apply_satnogs_fields(rec)
        # Offsets are the operator's own saved corrections, not SatNOGS data,
        # so they are populated here and not by _apply_satnogs_fields().
        self._dl_offset_spin.setValue(int(rec.get("rx_offset_hz") or 0))
        self._ul_offset_spin.setValue(int(rec.get("ul_offset_hz") or 0))
        self._notes_edit.setText(rec.get("notes") or "")
        self._overwrite_check.setChecked(bool(rec.get("manual_override", 1)))

    def _apply_satnogs_fields(self, rec: dict[str, Any]) -> None:
        """Populate only the fields SatNOGS actually has data for: description,
        frequencies, type, mode, invert, CTCSS. Shared by _prefill() (edit
        mode) and _on_reset_to_satnogs() (the "Reset to SatNOGS Official
        Value" button) -- deliberately leaves NORAD ID, Notes, and the
        Overwrite-protection checkbox untouched, since NORAD/Notes aren't
        SatNOGS data and the checkbox is the caller's own responsibility."""
        self._desc_edit.setText(rec.get("description", ""))
        self._dl_spin.setValue(self._hz_to_mhz(rec.get("downlink_low")))
        self._dl_high_spin.setValue(self._hz_to_mhz(rec.get("downlink_high")))
        self._ul_spin.setValue(self._hz_to_mhz(rec.get("uplink_low")))
        self._ul_high_spin.setValue(self._hz_to_mhz(rec.get("uplink_high")))

        xtype = rec.get("type", "Transponder")
        idx = self._type_combo.findText(xtype)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)

        mode = rec.get("mode") or "FM"
        midx = self._mode_combo.findText(mode)
        if midx >= 0:
            self._mode_combo.setCurrentIndex(midx)

        self._invert_check.setChecked(bool(rec.get("invert", False)))

        ctcss_type = rec.get("ctcss_tone_type") or ""
        cidx = self._ctcss_type_combo.findText(ctcss_type)
        if cidx >= 0:
            self._ctcss_type_combo.setCurrentIndex(cidx)

        ctcss = rec.get("ctcss_tone")
        self._ctcss_spin.setValue(ctcss if ctcss and ctcss > 0 else 0.0)

    # ------------------------------------------------------------------ #
    # Signal handlers
    # ------------------------------------------------------------------ #

    def _on_accept(self) -> None:
        """Handle OK button press."""
        norad = self._norad_spin.value()
        satnogs_norad = self._satnogs_norad_spin.value()

        # SatNOGS import mode (when a SatNOGS NORAD ID is specified)
        if not self._edit_mode and satnogs_norad != 0:
            self._do_satnogs_import(norad, satnogs_norad)
            return

        desc = self._desc_edit.text().strip()
        if not desc:
            QMessageBox.warning(self, _("Error"), _("Description is required."))
            return

        dl_low = self._mhz_to_hz(self._dl_spin.value())
        if dl_low is None:
            QMessageBox.warning(self, _("Error"), _("Downlink frequency is required."))
            return

        dl_high = self._mhz_to_hz(self._dl_high_spin.value())
        ul_low = self._mhz_to_hz(self._ul_spin.value())
        ul_high = self._mhz_to_hz(self._ul_high_spin.value())
        mode = self._mode_combo.currentText()
        xpdr_type = self._type_combo.currentText()
        invert = self._invert_check.isChecked()

        ctcss_type_str: str | None = self._ctcss_type_combo.currentText() or None
        ctcss_tone: float | None = (
            self._ctcss_spin.value() if self._ctcss_spin.value() > 0.0 else None
        )
        notes = self._notes_edit.text().strip()
        manual_override = int(self._overwrite_check.isChecked())

        try:
            if self._edit_mode and self._existing is not None:
                self._tm.update_transmitter(
                    self._existing["uuid"],
                    description=desc,
                    type=xpdr_type,
                    downlink_low=dl_low,
                    downlink_high=dl_high,
                    uplink_low=ul_low,
                    uplink_high=ul_high,
                    mode=mode,
                    invert=int(invert),
                    ctcss_tone=ctcss_tone,
                    ctcss_tone_type=ctcss_type_str,
                    notes=notes,
                    manual_override=manual_override,
                    rx_offset_hz=float(self._dl_offset_spin.value()),
                    ul_offset_hz=float(self._ul_offset_spin.value()),
                )
            else:
                self._tm.add_manual_transmitter(
                    norad_cat_id=norad,
                    description=desc,
                    downlink_low=dl_low,
                    mode=mode,
                    uplink_low=ul_low,
                    uplink_high=ul_high,
                    downlink_high=dl_high,
                    invert=invert,
                    ctcss_tone=ctcss_tone,
                    ctcss_tone_type=ctcss_type_str,
                    notes=notes,
                    xpdr_type=xpdr_type,
                    manual_override=bool(manual_override),
                    rx_offset_hz=float(self._dl_offset_spin.value()),
                    ul_offset_hz=float(self._ul_offset_spin.value()),
                )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, _("Error"), str(exc))

    def _do_satnogs_import(self, primary_norad: int, satnogs_norad: int) -> None:
        """Fetch frequency data by SatNOGS NORAD ID and save it mapped to the primary NORAD."""
        try:
            result = asyncio.run(
                self._tm.sync_from_satnogs(
                    norad_cat_id=satnogs_norad,
                    target_norad_cat_id=primary_norad,
                )
            )
            n = result["inserted"] + result["updated"]
            QMessageBox.information(
                self,
                _("SatNOGS Import"),
                _("Imported {n} transmitter(s) for NORAD {norad} from SatNOGS ID {src}.").format(
                    n=n, norad=primary_norad, src=satnogs_norad
                ),
            )
            self.accept()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, _("Error"), str(exc))

    def _on_reset_to_satnogs(self) -> None:
        """Re-fetch this transmitter's current data directly from SatNOGS and
        populate the form. Does NOT touch the DB until OK is pressed -- the
        user reviews (and can still tweak) the fetched values before saving,
        same as any other field. Also unchecks Overwrite protection, since
        the whole point is to resume being kept in sync automatically."""
        if self._existing is None:
            return
        try:
            rec = asyncio.run(self._tm.fetch_satnogs_transmitter(self._existing["uuid"]))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, _("Error"), str(exc))
            return
        if rec is None:
            QMessageBox.warning(
                self,
                _("Reset to SatNOGS"),
                _("This transmitter is no longer present in the SatNOGS database."),
            )
            return
        self._apply_satnogs_fields(rec)
        self._overwrite_check.setChecked(False)
