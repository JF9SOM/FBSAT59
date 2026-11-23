"""Dialog of the Telemetry tab's "Export…" button: date range and file format."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from comms.telemetry.frame_export import FORMAT_CSV, FORMAT_KISS, FORMAT_SATNOGS
from i18n import _


class TelemetryExportDialog(QDialog):
    """Asks for the UTC date range and the format of the frames to export.

    The range starts one day back (the frames of a recent pass); both days are included.
    Use :meth:`range_utc` and :meth:`export_format` after ``exec()`` returns accepted.
    """

    def __init__(self, satellite_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Export frames"))
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(satellite_label))

        form = QFormLayout()
        today = datetime.now(UTC).date()
        self._from_edit = self._date_edit(today - timedelta(days=1))
        self._to_edit = self._date_edit(today)
        form.addRow(_("Start date (UTC):"), self._from_edit)
        form.addRow(_("End date (UTC):"), self._to_edit)

        self._format_combo = QComboBox()
        self._format_combo.addItem(_("CSV (time, callsign, hex)"), FORMAT_CSV)
        self._format_combo.addItem(_("KISS hex text (C0 00 … C0)"), FORMAT_KISS)
        self._format_combo.addItem(_("SatNOGS frame list (time|hex||)"), FORMAT_SATNOGS)
        form.addRow(_("Export format:"), self._format_combo)
        layout.addLayout(form)

        note = QLabel(
            _(
                "The KISS and SatNOGS formats need the whole received frame; frames saved\n"
                "with the payload only are left out of them (the CSV keeps them)."
            )
        )
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _date_edit(day: date) -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd")
        edit.setDate(QDate(day.year, day.month, day.day))
        return edit

    def range_utc(self) -> tuple[datetime, datetime]:
        """``(start, end)``: midnight UTC of the start day to midnight after the end day."""
        first = self._from_edit.date()
        last = self._to_edit.date()
        start = datetime(first.year(), first.month(), first.day(), tzinfo=UTC)
        end = datetime(last.year(), last.month(), last.day(), tzinfo=UTC) + timedelta(days=1)
        return start, end

    def export_format(self) -> str:
        """The chosen format: ``csv``, ``kiss`` or ``satnogs``."""
        return str(self._format_combo.currentData())
