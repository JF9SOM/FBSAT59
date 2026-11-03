"""Telemetry tab page that reassembles files downlinked as numbered packets.

OrigamiSat-2 sends JPEG images and AVI videos as ID68 blocks (see
comms.telemetry.file_assembler). The page reads the logged frames of the selected
satellite from ``telemetry_log``, collects the blocks by number across all passes and
lets the user preview or save each run of consecutive blocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from comms.telemetry.decoder import load_format
from comms.telemetry.file_assembler import FileAssembler, FileTransferDef, detect_kind
from i18n import _


def file_transfer_def(norad: int | None) -> FileTransferDef | None:
    """The satellite's ``file_transfer`` definition, or None if it has no file downlink."""
    if norad is None:
        return None
    fmt = load_format(norad)
    return FileTransferDef.from_json(fmt.get("file_transfer")) if fmt else None


class TelemetryFilesPage(QWidget):
    """List of reassembled files (runs of consecutive blocks) with preview and save."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._asm: FileAssembler | None = None
        # (first block, last block, kind) of each list row
        self._entries: list[tuple[int, int, str]] = []
        layout = QVBoxLayout(self)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        body = QHBoxLayout()
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        body.addWidget(self._list, 1)
        self._preview = QLabel(_("Select an entry to preview it."))
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(240, 180)
        body.addWidget(self._preview, 1)
        layout.addLayout(body, 1)
        buttons = QHBoxLayout()
        self._refresh_btn = QPushButton(_("Refresh"))
        self._save_btn = QPushButton(_("Save…"))
        self._save_btn.setEnabled(False)
        self._fill = QCheckBox(_("Fill missing blocks"))
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        self._save_btn.clicked.connect(self._on_save)
        buttons.addWidget(self._refresh_btn)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._fill)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._conn: Any = None
        self._norad: int | None = None

    def refresh(self, conn: Any, norad: int | None) -> None:
        """Rebuild the list from the logged frames of *norad*."""
        self._conn = conn
        self._norad = norad
        self._list.clear()
        self._entries = []
        self._asm = None
        self._preview.clear()
        self._preview.setText(_("Select an entry to preview it."))
        self._save_btn.setEnabled(False)
        spec = file_transfer_def(norad)
        if spec is None or not hasattr(conn, "execute"):
            self._summary.setText("")
            return
        asm = FileAssembler(spec)
        for (raw_hex,) in conn.execute(
            "SELECT raw_hex FROM telemetry_log WHERE norad_cat_id = ?", (norad,)
        ):
            try:
                asm.add_payload(bytes.fromhex(str(raw_hex)))
            except ValueError:
                continue
        self._asm = asm
        if not asm.blocks:
            self._summary.setText(_("No file data blocks received yet."))
            return
        self._summary.setText(asm.summary())
        starts = set(asm.start_blocks())
        for run in asm.runs():
            run_starts = sorted(s for s in starts if run.first <= s <= run.last)
            if not run_starts:
                self._add_entry(run.first, run.last, "bin", asm)
                continue
            # a file starts at each start block and ends before the next one or the run end
            for k, first in enumerate(run_starts):
                last = run_starts[k + 1] - 1 if k + 1 < len(run_starts) else run.last
                data = asm.build(first, last)
                self._add_entry(first, last, detect_kind(data) if data else "bin", asm)

    def _add_entry(self, first: int, last: int, kind: str, asm: FileAssembler) -> None:
        count = last - first + 1
        size = count * asm.spec.data_length
        names = {"jpg": _("JPEG image"), "avi": _("AVI video"), "bin": _("Fragment (no start)")}
        text = f"{names[kind]}: {first}-{last} ({count} blocks, {size} bytes)"
        if kind == "jpg" and asm.is_complete_jpeg(first):
            text += " " + _("complete")
        self._list.addItem(QListWidgetItem(text))
        self._entries.append((first, last, kind))

    def _selected_data(self) -> tuple[bytes, str] | None:
        row = self._list.currentRow()
        if self._asm is None or not 0 <= row < len(self._entries):
            return None
        first, last, kind = self._entries[row]
        data = self._asm.build(first, last, fill_gaps=self._fill.isChecked())
        return (data, kind) if data is not None else None

    def _on_row_changed(self, row: int) -> None:
        self._save_btn.setEnabled(row >= 0)
        selected = self._selected_data()
        if selected is None or selected[1] != "jpg":
            self._preview.clear()
            self._preview.setText(_("No preview for this entry."))
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(selected[0], "JPG"):
            self._preview.setPixmap(
                pixmap.scaled(
                    self._preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._preview.setText(_("The data cannot be shown as an image yet."))

    def _on_refresh_clicked(self) -> None:
        self.refresh(self._conn, self._norad)

    def _on_save(self) -> None:
        selected = self._selected_data()
        if selected is None:
            QMessageBox.information(
                self,
                _("Save"),
                _("This entry has missing blocks. Tick 'Fill missing blocks' to save it."),
            )
            return
        data, kind = selected
        default = str(
            Path.home() / f"{self._norad}_{self._entries[self._list.currentRow()][0]}.{kind}"
        )
        path, _filter = QFileDialog.getSaveFileName(self, _("Save"), default)
        if path:
            Path(path).write_bytes(data)
