"""
Manual TLE addition dialog

ManualTLEDialog — dialog opened from Satellite > Add Manual TLE...
Automatically fetches TLE from CelesTrak using a NORAD catalog number,
or allows the user to paste three TLE lines manually and save them to the DB.
"""

from __future__ import annotations

import asyncio

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from data.omm import parse_tle_norad
from data.tle_manager import TLEManager
from i18n import _


class ManualTLEDialog(QDialog):
    """Satellite > Add Manual TLE... dialog.

    Fetches TLE automatically from CelesTrak using a NORAD catalog number,
    or allows pasting three TLE lines manually to add them to the DB.
    """

    def __init__(self, tle_manager: TLEManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tle_manager = tle_manager
        self._added_norad: int | None = None
        self.setWindowTitle(_("Add Manual TLE"))
        self.resize(480, 300)
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    @property
    def added_norad(self) -> int | None:
        """NORAD catalog number of the added satellite. None if the dialog was cancelled."""
        return self._added_norad

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()

        # Tab 1: NORAD ID -> CelesTrak auto-fetch
        fetch_tab = QWidget()
        fetch_layout = QVBoxLayout(fetch_tab)
        fetch_form = QFormLayout()

        self._norad_spin = QSpinBox()
        self._norad_spin.setRange(1, 999999)
        self._norad_spin.setValue(25544)
        fetch_form.addRow(_("NORAD Cat ID:"), self._norad_spin)
        fetch_layout.addLayout(fetch_form)

        self._fetch_btn = QPushButton(_("Fetch from CelesTrak"))
        self._fetch_btn.clicked.connect(self._on_fetch)
        fetch_layout.addWidget(self._fetch_btn)

        self._fetch_status = QLabel("")
        self._fetch_status.setWordWrap(True)
        fetch_layout.addWidget(self._fetch_status)
        fetch_layout.addStretch()

        self._tabs.addTab(fetch_tab, _("Fetch by NORAD ID"))

        # Tab 2: manual TLE paste
        manual_tab = QWidget()
        manual_layout = QVBoxLayout(manual_tab)
        manual_layout.addWidget(QLabel(_("Paste TLE (3 lines: Name, Line 1, Line 2):")))

        self._tle_edit = QPlainTextEdit()
        self._tle_edit.setPlaceholderText(
            "SATELLITE NAME\n1 NNNNNC NNNNNAAA.AAAAAAAA ...\n2 NNNNN ..."
        )
        manual_layout.addWidget(self._tle_edit)

        self._tabs.addTab(manual_tab, _("Manual TLE"))

        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #

    def _on_fetch(self) -> None:
        """Fetch button: retrieve TLE from CelesTrak and add it to the DB."""
        norad = self._norad_spin.value()
        self._fetch_status.setText(_("Fetching..."))
        self._fetch_btn.setEnabled(False)
        QApplication.processEvents()

        try:
            success = asyncio.run(self._tle_manager.fetch_single(norad))
            if success:
                self._fetch_status.setText(
                    _("Successfully fetched TLE for NORAD {n}").format(n=norad)
                )
                self._added_norad = norad
            else:
                self._fetch_status.setText(
                    _("Failed to fetch TLE. Check NORAD ID and network connection.")
                )
        except Exception as exc:  # noqa: BLE001
            self._fetch_status.setText(f"Error: {exc}")
        finally:
            self._fetch_btn.setEnabled(True)

    def _on_accept(self) -> None:
        """OK button handler."""
        if self._tabs.currentIndex() == 0:
            if self._added_norad is None:
                QMessageBox.warning(
                    self,
                    _("No TLE"),
                    _("Please click 'Fetch from CelesTrak' first."),
                )
                return
            self.accept()
        else:
            self._accept_manual()

    def _accept_manual(self) -> None:
        """Manual tab: parse the TLE text and add it to the DB."""
        text = self._tle_edit.toPlainText().strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 3:  # noqa: PLR2004
            QMessageBox.warning(
                self,
                _("Invalid TLE"),
                _("Please enter 3 lines: Name, Line 1, Line 2."),
            )
            return
        name, line1, line2 = lines[0], lines[1], lines[2]
        if not line1.startswith("1 ") or not line2.startswith("2 "):
            QMessageBox.warning(
                self,
                _("Invalid TLE"),
                _("Line 1 must start with '1 ' and Line 2 with '2 '."),
            )
            return
        try:
            norad = parse_tle_norad(line1)
        except ValueError:
            QMessageBox.warning(
                self,
                _("Invalid TLE"),
                _("Cannot parse NORAD ID from Line 1."),
            )
            return
        if self._tle_manager.add_manual_tle(norad, name, line1, line2):
            self._added_norad = norad
            self.accept()
        else:
            QMessageBox.warning(
                self,
                _("Invalid TLE"),
                _("TLE validation failed. Please check the format."),
            )
