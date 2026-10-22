"""APRS-IS monitor window: satellite APRS packets other stations report to the internet."""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.aprs.aprs_is import (
    DEFAULT_PORT,
    DEFAULT_SERVER,
    SATELLITE_CALLSIGNS,
    AprsIsClient,
    build_filter,
    match_satellite,
    parse_is_line,
)
from i18n import _

_MAX_ROWS = 2000
_CHECK_COLUMNS = 4


class AprsIsWindow(QWidget):
    """Separate window listing satellite packets seen on APRS-IS (receive only).

    The server filter is built from the ticked satellites (plus an optional raw filter);
    the search box only hides rows already received.
    """

    def __init__(self, login_call: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(_("APRS-IS Monitor"))
        self.resize(980, 560)
        self._login_call = login_call.strip().upper() or "N0CALL"
        self._client = AprsIsClient(self)
        self._client.line_received.connect(self._on_line)
        self._client.status_changed.connect(self._on_status)

        root = QVBoxLayout(self)

        # -- Connection --
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel(_("Server:")))
        self._server_edit = QLineEdit(DEFAULT_SERVER)
        self._server_edit.setMinimumWidth(180)
        conn_row.addWidget(self._server_edit)
        conn_row.addWidget(QLabel(_("Port:")))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(DEFAULT_PORT)
        conn_row.addWidget(self._port_spin)
        conn_row.addStretch()
        self._connect_btn = QPushButton(_("Connect"))
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        conn_row.addWidget(self._connect_btn)
        self._clear_btn = QPushButton(_("Clear"))
        self._clear_btn.clicked.connect(self._on_clear)
        conn_row.addWidget(self._clear_btn)
        root.addLayout(conn_row)

        # -- Satellite selection --
        sat_group = QGroupBox(_("Satellites"))
        grid = QGridLayout(sat_group)
        self._sat_checks: dict[str, QCheckBox] = {}
        for i, name in enumerate(SATELLITE_CALLSIGNS):
            box = QCheckBox(f"{name} ({', '.join(SATELLITE_CALLSIGNS[name])})")
            box.setChecked(True)
            box.toggled.connect(self._on_filter_changed)
            self._sat_checks[name] = box
            grid.addWidget(box, i // _CHECK_COLUMNS, i % _CHECK_COLUMNS)
        root.addWidget(sat_group)

        # -- Filters --
        filt_row = QHBoxLayout()
        filt_row.addWidget(QLabel(_("Extra APRS-IS filter:")))
        self._extra_edit = QLineEdit()
        self._extra_edit.setPlaceholderText("e.g. b/JA1ABC*  or  p/JA")
        self._extra_edit.editingFinished.connect(self._on_filter_changed)
        filt_row.addWidget(self._extra_edit, stretch=1)
        filt_row.addWidget(QLabel(_("Search:")))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(_("callsign or text"))
        self._search_edit.textChanged.connect(self._apply_search)
        filt_row.addWidget(self._search_edit, stretch=1)
        root.addLayout(filt_row)

        # -- Packet list --
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            [_("Time (UTC)"), _("Satellite"), _("From"), _("Path"), _("Info")]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in enumerate((80, 90, 110, 260)):
            self._table.setColumnWidth(col, width)
        root.addWidget(self._table, stretch=1)

        # -- Status --
        self._status_label = QLabel(_("Disconnected"))
        self._count_label = QLabel("")
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        status_row.addWidget(self._count_label)
        root.addLayout(status_row)

    # ------------------------------------------------------------------ #

    def selected_satellites(self) -> list[str]:
        """Names of the ticked satellites."""
        return [n for n, box in self._sat_checks.items() if box.isChecked()]

    def current_filter(self) -> str:
        """The APRS-IS filter string for the current selection."""
        return build_filter(self.selected_satellites(), self._extra_edit.text())

    def row_count(self) -> int:
        """Number of rows in the list (visible or hidden by the search)."""
        return self._table.rowCount()

    def _on_connect_clicked(self) -> None:
        if self._client.running:
            self._client.stop()
            self._set_connected_ui(False)
            return
        filter_text = self.current_filter()
        if not filter_text:
            self._status_label.setText(_("Select at least one satellite or enter a filter"))
            return
        self._client.start(
            self._server_edit.text().strip(), self._port_spin.value(), self._login_call, filter_text
        )
        self._set_connected_ui(True)

    def _set_connected_ui(self, connected: bool) -> None:
        self._connect_btn.setText(_("Disconnect") if connected else _("Connect"))
        self._server_edit.setEnabled(not connected)
        self._port_spin.setEnabled(not connected)

    def _on_filter_changed(self) -> None:
        """Send the new filter to the running session (no reconnect needed)."""
        if self._client.running:
            self._client.update_filter(self.current_filter())

    def _on_status(self, status: str) -> None:
        if status == "connected":
            text = _("Connected — {server}").format(server=self._server_edit.text().strip())
        elif status == "connecting":
            text = _("Connecting…")
        elif status == "disconnected":
            text = _("Disconnected")
            self._set_connected_ui(False)
        else:
            text = _("Connection failed: {err}").format(err=status.removeprefix("error: "))
            self._set_connected_ui(False)
        self._status_label.setText(text)

    def _on_line(self, line: str) -> None:
        """Add one received packet line to the list."""
        packet = parse_is_line(line)
        if packet is None:
            return
        sat = match_satellite(packet) or "—"
        row = self._table.rowCount()
        self._table.insertRow(row)
        cells = (
            datetime.now(UTC).strftime("%H:%M:%S"),
            sat,
            packet.source,
            ",".join(packet.path),
            packet.info,
        )
        for col, text in enumerate(cells):
            self._table.setItem(row, col, QTableWidgetItem(text))
        self._table.setRowHidden(row, not self._row_matches(row))
        if self._table.rowCount() > _MAX_ROWS:
            self._table.removeRow(0)
        self._table.scrollToBottom()
        self._count_label.setText(_("{n} packets").format(n=self._table.rowCount()))

    def _row_matches(self, row: int) -> bool:
        needle = self._search_edit.text().strip().lower()
        if not needle:
            return True
        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            if item is not None and needle in item.text().lower():
                return True
        return False

    def _apply_search(self) -> None:
        for row in range(self._table.rowCount()):
            self._table.setRowHidden(row, not self._row_matches(row))

    def _on_clear(self) -> None:
        self._table.setRowCount(0)
        self._count_label.setText("")

    def closeEvent(self, event: QCloseEvent) -> None:
        """Disconnect when the window is closed."""
        self._client.stop()
        self._set_connected_ui(False)
        super().closeEvent(event)
