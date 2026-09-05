"""Reusable satellite picker dialog with a live text-search filter.

Originally written for the Autotrack "Add Satellite" flow
(ui/autotrack_record_dialog.py); extracted here so other satellite
pickers (e.g. the Telemetry tab's Direwolf/gr-satellites combos) can
reuse the same search UI instead of duplicating it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from i18n import _


class SatSearchDialog(QDialog):
    """Satellite picker dialog with a live text-search filter."""

    def __init__(
        self,
        satellites: list[tuple[int, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Select Satellite"))
        self.resize(420, 480)
        self.selected_norad: int | None = None
        self._all: list[tuple[int, str]] = satellites

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText(_("Search…"))
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(btns)

        self._populate("")
        self._search.textChanged.connect(self._populate)
        self._list.itemDoubleClicked.connect(self._accept_item)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)

        self._search.setFocus()

    def _populate(self, text: str) -> None:
        self._list.clear()
        needle = text.strip().lower()
        for norad, name in self._all:
            label = f"{name} ({norad})"
            if needle and needle not in label.lower():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, norad)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _accept_item(self, _item: QListWidgetItem) -> None:
        self._on_ok()

    def _on_ok(self) -> None:
        current = self._list.currentItem()
        if current is None:
            return
        self.selected_norad = int(current.data(Qt.ItemDataRole.UserRole))
        self.accept()
