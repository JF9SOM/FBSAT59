"""APRS-IS monitor window: satellite APRS packets other stations report to the internet."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
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
    FULL_FEED_PORT,
    NO_MATCH_FILTER,
    SATELLITE_CALLSIGNS,
    AprsIsClient,
    build_filter,
    match_satellite,
    parse_is_line,
)
from i18n import _

# Rows shown in the list, and rows kept in memory (the search looks through the latter).
_VISIBLE_ROWS = 2000
_STORED_ROWS = 20000
_FLUSH_MS = 200
_CHECK_COLUMNS = 4
_RADIUS_PRESETS_KM = (100, 300, 1000, 3000)
# Used for the range filter until the station's own QTH is known (central Japan).
_DEFAULT_LATLON = (35.0, 139.0)

_Row = tuple[str, str, str, str, str]


class AprsIsWindow(QWidget):
    """Separate window listing packets seen on APRS-IS (receive only).

    The server filter is built from the ticked satellites, an optional range around a point
    and an optional raw filter (APRS-IS ORs them); "Show everything" connects to the
    unfiltered full feed instead. The search box only narrows what is listed.
    """

    def __init__(
        self,
        login_call: str = "",
        location_fn: Callable[[], tuple[float, float] | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(_("APRS-IS Monitor"))
        self.resize(1000, 700)
        self._login_call = login_call.strip().upper() or "N0CALL"
        self._location_fn = location_fn
        self._connected_port: int | None = None
        self._rows: deque[_Row] = deque(maxlen=_STORED_ROWS)
        self._pending: list[str] = []
        self._client = AprsIsClient(self)
        self._client.line_received.connect(self._on_line)
        self._client.status_changed.connect(self._on_status)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()

        root = QVBoxLayout(self)
        root.addLayout(self._build_connection_row())
        root.addWidget(self._build_filter_group())

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel(_("Search:")))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(_("callsign or text"))
        self._search_edit.textChanged.connect(self._rebuild_table)
        search_row.addWidget(self._search_edit, stretch=1)
        root.addLayout(search_row)

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

        self._status_label = QLabel(_("Disconnected"))
        self._count_label = QLabel("")
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        status_row.addWidget(self._count_label)
        root.addLayout(status_row)
        self._update_filter_widgets()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_connection_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(_("Server:")))
        self._server_edit = QLineEdit(DEFAULT_SERVER)
        self._server_edit.setMinimumWidth(180)
        row.addWidget(self._server_edit)
        row.addWidget(QLabel(_("Port:")))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(DEFAULT_PORT)
        row.addWidget(self._port_spin)
        row.addStretch()
        self._connect_btn = QPushButton(_("Connect"))
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        row.addWidget(self._connect_btn)
        self._clear_btn = QPushButton(_("Clear"))
        self._clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self._clear_btn)
        return row

    def _build_filter_group(self) -> QGroupBox:
        group = QGroupBox(_("Filter"))
        layout = QVBoxLayout(group)

        self._all_check = QCheckBox(_("Show everything (no filter — about 100 packets per second)"))
        self._all_check.setToolTip(
            _("Connect to the unfiltered full feed. The other filters are ignored.")
        )
        self._all_check.toggled.connect(self._on_filter_changed)
        layout.addWidget(self._all_check)

        # Satellites
        sat_head = QHBoxLayout()
        sat_head.addWidget(QLabel(_("Satellites")))
        self._select_all_btn = QPushButton(_("Select all"))
        self._select_all_btn.clicked.connect(lambda: self._set_all_satellites(True))
        sat_head.addWidget(self._select_all_btn)
        self._select_none_btn = QPushButton(_("Select none"))
        self._select_none_btn.clicked.connect(lambda: self._set_all_satellites(False))
        sat_head.addWidget(self._select_none_btn)
        sat_head.addStretch()
        layout.addLayout(sat_head)
        grid = QGridLayout()
        self._sat_checks: dict[str, QCheckBox] = {}
        for i, name in enumerate(SATELLITE_CALLSIGNS):
            box = QCheckBox(f"{name} ({', '.join(SATELLITE_CALLSIGNS[name])})")
            box.setChecked(True)
            box.toggled.connect(self._on_filter_changed)
            self._sat_checks[name] = box
            grid.addWidget(box, i // _CHECK_COLUMNS, i % _CHECK_COLUMNS)
        layout.addLayout(grid)

        # Range around a point
        range_row = QHBoxLayout()
        self._range_check = QCheckBox(_("Stations within"))
        self._range_check.toggled.connect(self._on_filter_changed)
        range_row.addWidget(self._range_check)
        self._km_spin = QSpinBox()
        self._km_spin.setRange(1, 20000)
        self._km_spin.setValue(300)
        self._km_spin.setSuffix(" km")
        self._km_spin.editingFinished.connect(self._on_filter_changed)
        range_row.addWidget(self._km_spin)
        self._preset_btns: list[QPushButton] = []
        for km in _RADIUS_PRESETS_KM:
            btn = QPushButton(str(km))
            btn.setFixedWidth(50)
            btn.clicked.connect(lambda _checked=False, k=km: self._set_radius(k))
            range_row.addWidget(btn)
            self._preset_btns.append(btn)
        range_row.addWidget(QLabel(_("of")))
        lat, lon = self._initial_latlon()
        self._lat_spin = QDoubleSpinBox()
        self._lat_spin.setRange(-90.0, 90.0)
        self._lat_spin.setDecimals(4)
        self._lat_spin.setValue(lat)
        self._lat_spin.editingFinished.connect(self._on_filter_changed)
        range_row.addWidget(self._lat_spin)
        self._lon_spin = QDoubleSpinBox()
        self._lon_spin.setRange(-180.0, 180.0)
        self._lon_spin.setDecimals(4)
        self._lon_spin.setValue(lon)
        self._lon_spin.editingFinished.connect(self._on_filter_changed)
        range_row.addWidget(self._lon_spin)
        self._qth_btn = QPushButton(_("My QTH"))
        self._qth_btn.setToolTip(_("Use the station position saved in the settings"))
        self._qth_btn.clicked.connect(self._on_use_qth)
        range_row.addWidget(self._qth_btn)
        range_row.addStretch()
        layout.addLayout(range_row)

        # Raw filter
        extra_row = QHBoxLayout()
        extra_row.addWidget(QLabel(_("Extra APRS-IS filter:")))
        self._extra_edit = QLineEdit()
        self._extra_edit.setPlaceholderText("e.g. b/JA1ABC*  or  p/JA")
        self._extra_edit.editingFinished.connect(self._on_filter_changed)
        extra_row.addWidget(self._extra_edit, stretch=1)
        layout.addLayout(extra_row)
        return group

    # ------------------------------------------------------------------ #
    # Filter
    # ------------------------------------------------------------------ #

    def _initial_latlon(self) -> tuple[float, float]:
        pos = self._saved_qth()
        return pos if pos is not None else _DEFAULT_LATLON

    def _saved_qth(self) -> tuple[float, float] | None:
        return self._location_fn() if self._location_fn is not None else None

    def selected_satellites(self) -> list[str]:
        """Names of the ticked satellites."""
        return [n for n, box in self._sat_checks.items() if box.isChecked()]

    def show_everything(self) -> bool:
        """True when the unfiltered full feed is selected."""
        return self._all_check.isChecked()

    def current_filter(self) -> str:
        """The APRS-IS filter string for the current selection ("" for the full feed)."""
        if self.show_everything():
            return ""
        nearby = (
            (self._lat_spin.value(), self._lon_spin.value(), self._km_spin.value())
            if self._range_check.isChecked()
            else None
        )
        return build_filter(self.selected_satellites(), self._extra_edit.text(), nearby)

    def _effective_port(self) -> int:
        return FULL_FEED_PORT if self.show_everything() else self._port_spin.value()

    def _set_all_satellites(self, checked: bool) -> None:
        for box in self._sat_checks.values():
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)
        self._on_filter_changed()

    def _set_radius(self, km: int) -> None:
        self._km_spin.setValue(km)
        self._range_check.setChecked(True)
        self._on_filter_changed()

    def _on_use_qth(self) -> None:
        pos = self._saved_qth()
        if pos is None:
            self._status_label.setText(_("Station position is not set — configure it in Settings"))
            return
        self._lat_spin.setValue(pos[0])
        self._lon_spin.setValue(pos[1])
        self._range_check.setChecked(True)
        self._on_filter_changed()

    def _update_filter_widgets(self) -> None:
        """Grey out the filters the full feed ignores."""
        filtered = not self.show_everything()
        for widget in (
            *self._sat_checks.values(),
            self._select_all_btn,
            self._select_none_btn,
            self._range_check,
            self._km_spin,
            self._lat_spin,
            self._lon_spin,
            self._qth_btn,
            self._extra_edit,
            *self._preset_btns,
        ):
            widget.setEnabled(filtered)
        self._port_spin.setEnabled(filtered and not self._client.running)

    def _on_filter_changed(self, *_args: object) -> None:
        """Apply a changed filter to the running session."""
        self._update_filter_widgets()
        if not self._client.running:
            return
        if self._effective_port() != self._connected_port:
            self._connect()  # full feed <-> filtered use different server ports
        else:
            self._client.update_filter(self.current_filter() or NO_MATCH_FILTER)

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _on_connect_clicked(self) -> None:
        if self._client.running:
            self._client.stop()
            self._set_connected_ui(False)
            return
        self._connect()

    def _connect(self) -> None:
        filter_text = self.current_filter()
        if not filter_text and not self.show_everything():
            self._status_label.setText(_("Select at least one satellite or enter a filter"))
            return
        self._connected_port = self._effective_port()
        self._client.start(
            self._server_edit.text().strip(), self._connected_port, self._login_call, filter_text
        )
        self._set_connected_ui(True)

    def _set_connected_ui(self, connected: bool) -> None:
        self._connect_btn.setText(_("Disconnect") if connected else _("Connect"))
        self._server_edit.setEnabled(not connected)
        self._update_filter_widgets()
        if not connected:
            self._connected_port = None

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

    # ------------------------------------------------------------------ #
    # Packet list
    # ------------------------------------------------------------------ #

    def _on_line(self, line: str) -> None:
        """Queue one received line; the timer adds the queued ones to the list in a batch."""
        self._pending.append(line)

    def _flush_pending(self) -> None:
        """Parse the queued lines and add the ones that pass the search to the list."""
        if not self._pending:
            return
        lines, self._pending = self._pending, []
        now = datetime.now(UTC).strftime("%H:%M:%S")
        fresh: list[_Row] = []
        for line in lines:
            packet = parse_is_line(line)
            if packet is None:
                continue
            row: _Row = (
                now,
                match_satellite(packet) or "—",
                packet.source,
                ",".join(packet.path),
                packet.info,
            )
            self._rows.append(row)
            if self._row_matches(row):
                fresh.append(row)
        if fresh:
            self._append_rows(fresh[-_VISIBLE_ROWS:])
        self._count_label.setText(_("{n} packets").format(n=len(self._rows)))

    def _row_matches(self, row: _Row) -> bool:
        needle = self._search_edit.text().strip().lower()
        return not needle or any(needle in cell.lower() for cell in row)

    def _append_rows(self, rows: list[_Row]) -> None:
        bar = self._table.verticalScrollBar()
        at_bottom = bar is None or bar.value() >= bar.maximum() - 2
        self._table.setUpdatesEnabled(False)
        try:
            for row in rows:
                index = self._table.rowCount()
                self._table.insertRow(index)
                for col, text in enumerate(row):
                    self._table.setItem(index, col, QTableWidgetItem(text))
            for _i in range(self._table.rowCount() - _VISIBLE_ROWS):
                self._table.removeRow(0)
        finally:
            self._table.setUpdatesEnabled(True)
        if at_bottom:
            self._table.scrollToBottom()

    def _rebuild_table(self, *_args: object) -> None:
        """Re-list the stored packets that match the search."""
        matching = [r for r in self._rows if self._row_matches(r)][-_VISIBLE_ROWS:]
        self._table.setRowCount(0)
        self._append_rows(matching)

    def row_count(self) -> int:
        """Number of packets currently listed."""
        return self._table.rowCount()

    def _on_clear(self) -> None:
        self._pending.clear()
        self._rows.clear()
        self._table.setRowCount(0)
        self._count_label.setText("")

    def showEvent(self, event: QShowEvent) -> None:
        """Resume the list updates (the timer stops while the window is closed)."""
        self._flush_timer.start()
        super().showEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Disconnect when the window is closed."""
        self._flush_timer.stop()
        self._client.stop()
        self._set_connected_ui(False)
        super().closeEvent(event)
