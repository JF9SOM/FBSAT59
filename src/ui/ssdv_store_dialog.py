"""Stored SSDV pictures: every picture the SSTV / SSDV tab has collected packets for.

Packets from all passes and all SSDV modes are kept (comms.sstv.packet_store); a picture can be
built from whatever has been collected so far, and a later pass adds to it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.sstv.packet_store import KIND_DSLWP, GroupInfo, SsdvPacketStore
from i18n import _


def format_stamp(iso: str) -> str:
    """A stored ISO timestamp as local ``YYYY-MM-DD HH:MM``."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


class _SortItem(QTableWidgetItem):
    """A cell that sorts by a key (a number for Image ID / Packets) instead of its text."""

    def __init__(self, text: str, key: Any) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _SortItem):
            return bool(self._key < other._key)
        return super().__lt__(other)


class SsdvStoreDialog(QDialog):
    """List of stored pictures with Build Image / Delete."""

    build_requested: Signal = Signal(int)  # group id

    def __init__(
        self,
        store: SsdvPacketStore,
        name_of: Callable[[int], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._name_of = name_of
        self.setWindowTitle(_("Stored SSDV Images"))
        self.resize(720, 360)

        root = QVBoxLayout(self)
        hint = QLabel(
            _(
                "Packets received in every SSDV mode are kept, so a picture can be built from "
                "several passes: select it and press Build Image. New packets of the same "
                "picture are added automatically."
            )
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        root.addWidget(hint)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            [
                _("Satellite"),
                _("Format"),
                _("Image ID"),
                _("Size"),
                _("Packets"),
                _("Last received"),
            ]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        # Click a column header to sort by it (the list starts newest first, unsorted).
        self._table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._table.setSortingEnabled(True)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        self._table.itemDoubleClicked.connect(lambda _i: self._on_build())
        root.addWidget(self._table, stretch=1)

        buttons = QHBoxLayout()
        self._build_btn = QPushButton(_("🖼 Build Image"))
        self._build_btn.clicked.connect(self._on_build)
        buttons.addWidget(self._build_btn)
        self._delete_btn = QPushButton(_("🗑 Delete"))
        self._delete_btn.clicked.connect(self._on_delete)
        buttons.addWidget(self._delete_btn)
        buttons.addStretch()
        close = QPushButton(_("Close"))
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        """Reload the list from the store."""
        groups = self._store.groups()
        self._table.setSortingEnabled(False)  # rows must not move while they are filled
        self._table.setRowCount(len(groups))
        for row, g in enumerate(groups):
            keys = (None, None, g.image_id, g.width * g.height, g.packets, g.updated_at)
            for col, text in enumerate(self._cells(g)):
                item = _SortItem(text, text if keys[col] is None else keys[col])
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, g.id)
                if col in (2, 4):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self._table.setItem(row, col, item)
        self._table.setSortingEnabled(True)  # re-applies the column the user chose
        self._table.resizeColumnsToContents()
        self._update_buttons()

    def _cells(self, g: GroupInfo) -> list[str]:
        fmt = _("CCSDS (DSLWP)") if g.kind == KIND_DSLWP else _("Standard SSDV")
        packets = f"{g.packets}" + (f" / {g.total}" if g.total else "")
        return [
            self._name_of(g.norad),
            fmt,
            str(g.image_id),
            f"{g.width * 16}x{g.height * 16}",
            packets,
            format_stamp(g.updated_at),
        ]

    def _selected_id(self) -> int | None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._table.item(rows[0].row(), 0)
        return int(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _update_buttons(self) -> None:
        have = self._selected_id() is not None
        self._build_btn.setEnabled(have)
        self._delete_btn.setEnabled(have)

    def _on_build(self) -> None:
        gid = self._selected_id()
        if gid is not None:
            self.build_requested.emit(gid)

    def _on_delete(self) -> None:
        gid = self._selected_id()
        if gid is None:
            return
        reply = QMessageBox.question(
            self,
            _("Delete"),
            _("Delete the stored packets of this picture?"),
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._store.delete(gid)
            self.refresh()
