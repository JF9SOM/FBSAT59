"""Remove Manual TLE dialog

RemoveManualTLEDialog — opened from Satellite > Remove Manual TLE...
Lists the manually entered TLEs with their age and lets the user remove one, so the
satellite goes back to automatic TLE updates. A manual TLE is never overwritten by an
automatic fetch, so without this it would stay in place (and age) for good.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from data.tle_manager import MANUAL_TLE_STALE_DAYS, TLEManager
from i18n import _

_STALE_COLOR = QColor("#e67e22")


def format_age(age_days: float) -> str:
    """Human-readable age of a TLE epoch: "5.3 days", or "?" when unknown."""
    if age_days == float("inf"):
        return "?"
    return _("{n:.1f} days").format(n=age_days)


class RemoveManualTLEDialog(QDialog):
    """Satellite > Remove Manual TLE... dialog."""

    def __init__(self, tle_manager: TLEManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tle_manager = tle_manager
        self._removed: list[int] = []
        self.setWindowTitle(_("Remove Manual TLE"))
        self.resize(560, 320)
        self._setup_ui()
        self._reload()

    @property
    def removed_norads(self) -> list[int]:
        """NORAD ids whose manual TLE was removed while the dialog was open."""
        return list(self._removed)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(
            _(
                "A manually entered TLE is never replaced by an automatic update, so it "
                "ages. Removing it makes the satellite use automatic updates again. "
                "Remove it only once the automatic TLE is correct."
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self._info = QLabel("")
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            [_("Satellite"), _("NORAD"), _("TLE epoch (UTC)"), _("Age")]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self._table)

        row = QHBoxLayout()
        self._remove_btn = QPushButton(_("Remove Selected"))
        self._remove_btn.clicked.connect(self._on_remove)
        row.addWidget(self._remove_btn)
        row.addStretch()
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _reload(self) -> None:
        entries = self._tle_manager.list_manual_tles()
        self._table.setRowCount(len(entries))
        stale = 0
        for r, e in enumerate(entries):
            epoch = e["epoch"]
            cells = [
                e["name"],
                str(e["norad_cat_id"]),
                epoch.strftime("%Y-%m-%d %H:%M") if epoch is not None else "?",
                format_age(e["age_days"]) + (" ⚠" if e["stale"] else ""),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, e["norad_cat_id"])
                if e["stale"]:
                    item.setForeground(QBrush(_STALE_COLOR))
                self._table.setItem(r, c, item)
            stale += bool(e["stale"])
        if not entries:
            self._info.setText(_("There is no manually entered TLE."))
        elif stale:
            self._info.setText(
                _("⚠ {n} older than {d} days (orange).").format(n=stale, d=MANUAL_TLE_STALE_DAYS)
            )
        else:
            self._info.setText("")
        self._update_buttons()

    def _selected_norad(self) -> int | None:
        items = self._table.selectedItems()
        if not items:
            return None
        value = items[0].data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def _update_buttons(self) -> None:
        self._remove_btn.setEnabled(self._selected_norad() is not None)

    def _on_remove(self) -> None:
        norad = self._selected_norad()
        if norad is None:
            return
        name = self._table.item(self._table.currentRow(), 0)
        label = f"{name.text() if name else norad} (NORAD {norad})"
        answer = QMessageBox.question(
            self,
            _("Remove Manual TLE"),
            _(
                "Remove the manual TLE of {label}?\n\nThe satellite will use the automatic TLE "
                "again. Check that the automatic TLE is correct first."
            ).format(label=label),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._tle_manager.remove_manual_tle(norad):
            self._removed.append(norad)
        self._reload()
