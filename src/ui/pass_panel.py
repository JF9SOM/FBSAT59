"""
Pass prediction panel (tabbed layout)

PassPanel          — Upcoming Passes panel (2 tabs: Target / Group)
_GroupSearchWorker — background thread for Group search
GroupPassResult    — single Group search result
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from PySide6.QtCore import QDate, QDateTime, QLocale, Qt, QThread, QTime, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.celestial_engine import MOON_ID, CelestialEngine
from core.engine import PassInfo, PassPredictor
from i18n import _
from ui.pass_chart import QUALITY_COLORS, pass_quality

# Quick-range shortcuts: (display label, hours offset from "From")
_QUICK_RANGES: tuple[tuple[str, int], ...] = (
    ("+ 6h", 6),
    ("+24h", 24),
    ("+ 3d", 72),
    ("+ 7d", 168),
    ("+30d", 720),
)


# ---------------------------------------------------------------------------
# Timezone-aware helpers
# ---------------------------------------------------------------------------


def _utc_to_display_qdatetime(dt_utc: datetime, use_utc: bool) -> QDateTime:
    """Convert a UTC-aware datetime to a QDateTime in the chosen display timezone.

    Args:
        dt_utc:  UTC-aware datetime (tzinfo=UTC)
        use_utc: True → UTC spec QDateTime; False → LocalTime spec QDateTime

    Returns:
        QDateTime ready for use in a QDateTimeEdit.
    """
    if use_utc:
        return QDateTime(
            QDate(dt_utc.year, dt_utc.month, dt_utc.day),
            QTime(dt_utc.hour, dt_utc.minute, dt_utc.second),
            Qt.TimeSpec.UTC,
        )
    # Convert to the system local timezone before building QDateTime
    local_dt = dt_utc.astimezone()
    return QDateTime(
        QDate(local_dt.year, local_dt.month, local_dt.day),
        QTime(local_dt.hour, local_dt.minute, local_dt.second),
        Qt.TimeSpec.LocalTime,
    )


def _format_aos(dt_utc: datetime, use_utc: bool) -> str:
    """Format an AOS timestamp for table display in the chosen timezone.

    Args:
        dt_utc:  UTC-aware AOS datetime
        use_utc: True → format in UTC; False → format in system local time

    Returns:
        Formatted string like "05/28 16:30".
    """
    if use_utc:
        return dt_utc.strftime("%m/%d %H:%M")
    return dt_utc.astimezone().strftime("%m/%d %H:%M")


def _dt_to_qdatetime(dt: datetime) -> QDateTime:
    """Convert a Python datetime (UTC) to a QDateTime (UTC spec)."""
    return QDateTime(
        QDate(dt.year, dt.month, dt.day),
        QTime(dt.hour, dt.minute, dt.second),
        Qt.TimeSpec.UTC,
    )


def _qdatetime_to_dt(qdt: QDateTime) -> datetime:
    """Convert a QDateTime (any spec) to a UTC Python datetime."""
    utc = qdt.toUTC()
    d = utc.date()
    t = utc.time()
    return datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second(), tzinfo=UTC)


# ---------------------------------------------------------------------------
# Calendar widget with "Current Time" button
# ---------------------------------------------------------------------------


class _CalendarWithNow(QCalendarWidget):
    """QCalendarWidget extended with a 'Current Time' button at the bottom of the grid.

    Emits ``now_requested`` when the button is clicked; the owning
    ``_NowDateTimeEdit`` handles the actual datetime update and popup close.
    """

    now_requested: Signal = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        now_btn = QPushButton(_("Current Time"))
        now_btn.setToolTip(_("Set date and time to now"))
        now_btn.clicked.connect(self.now_requested)
        # QCalendarWidget creates a QVBoxLayout internally during __init__;
        # appending our button here places it below the month grid.
        # Apply top margin via stylesheet so the button stays separated from the
        # last week row without shrinking the calendar grid itself.
        now_btn.setStyleSheet("margin-top: 4px;")
        cal_layout = self.layout()
        if cal_layout is not None:
            cal_layout.addWidget(now_btn)


class _NowDateTimeEdit(QDateTimeEdit):
    """QDateTimeEdit whose calendar popup optionally includes a 'Current Time' button.

    Pass ``show_now_button=True`` (default) to include the button (for "From" fields).
    Pass ``show_now_button=False`` to omit it (for "To" fields).
    Supports switching between UTC and local time display via ``set_use_utc()``.
    """

    def __init__(self, parent: QWidget | None = None, *, show_now_button: bool = True) -> None:
        super().__init__(parent)
        self._use_utc: bool = True
        self.setCalendarPopup(True)
        self.setTimeSpec(Qt.TimeSpec.UTC)  # default: show UTC
        if show_now_button:
            self._cal: QCalendarWidget = _CalendarWithNow()
            assert isinstance(self._cal, _CalendarWithNow)
            self._cal.now_requested.connect(self._apply_now)
        else:
            self._cal = QCalendarWidget()
        # Force English locale so month/day names display in English
        # regardless of the system locale.
        self._cal.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        # Hide the week-number column on the left side of the calendar grid.
        self._cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setCalendarWidget(self._cal)

    def set_use_utc(self, use_utc: bool) -> None:
        """Switch the display between UTC and local time.

        Also updates the currently displayed value to the correct timezone.
        """
        if use_utc == self._use_utc:
            return
        self._use_utc = use_utc
        new_spec = Qt.TimeSpec.UTC if use_utc else Qt.TimeSpec.LocalTime
        # Convert the current value to the new timezone before changing the spec
        # so the displayed instant stays the same.
        current_utc = _qdatetime_to_dt(self.dateTime())
        self.setTimeSpec(new_spec)
        self.setDateTime(_utc_to_display_qdatetime(current_utc, use_utc))

    def _apply_now(self) -> None:
        """Reset to the current instant in the active display timezone and close popup."""
        now_utc = datetime.now(UTC)
        qdt = _utc_to_display_qdatetime(now_utc, self._use_utc)
        # Emitting activated(QDate) triggers QCalendarPopup.dateSelected which:
        #   1. propagates the date to the edit via newDateEntered → setDate
        #   2. calls hidePopup() to dismiss the popup
        self._cal.activated.emit(qdt.date())
        # setDate() only updates the date portion; apply the time separately.
        self.setTime(qdt.time())


# ---------------------------------------------------------------------------
# Group search worker
# ---------------------------------------------------------------------------


@dataclass
class GroupPassResult:
    """A single search result on the Group tab."""

    norad_cat_id: int
    sat_name: str
    pass_info: PassInfo


_CacheKey = tuple[datetime, datetime, float, tuple[int, ...]]


class _GroupSearchWorker(QThread):
    """Worker that runs a group satellite pass search in the background."""

    progress: Signal = Signal(int)
    finished_results: Signal = Signal(object)

    def __init__(
        self,
        predictor: PassPredictor,
        sat_list: list[tuple[int, str]],
        start: datetime,
        end: datetime,
        min_el: float,
        parent: QWidget | None = None,
        celestial_engine: CelestialEngine | None = None,
        observer_lat: float = 0.0,
        observer_lon: float = 0.0,
        observer_elev_m: float = 0.0,
    ) -> None:
        super().__init__(parent)
        self._predictor = predictor
        self._sat_list = sat_list
        self._start = start
        self._end = end
        self._min_el = min_el
        self._cancelled = False
        self._celestial_engine = celestial_engine
        self._observer_lat = observer_lat
        self._observer_lon = observer_lon
        self._observer_elev_m = observer_elev_m

    def cancel(self) -> None:
        """Set the cancel flag (stops at the next iteration)."""
        self._cancelled = True

    def run(self) -> None:
        results: list[GroupPassResult] = []
        total = len(self._sat_list)
        for i, (norad, name) in enumerate(self._sat_list):
            if self._cancelled:
                break
            try:
                if norad == MOON_ID and self._celestial_engine is not None:
                    passes = [
                        p
                        for p in self._celestial_engine.moon_events(
                            self._observer_lat,
                            self._observer_lon,
                            self._observer_elev_m,
                            self._start,
                            self._end,
                        )
                        if p.max_elevation_deg >= self._min_el
                    ]
                else:
                    passes = self._predictor.get_passes(norad, self._start, self._end, self._min_el)
            except Exception:  # noqa: BLE001
                passes = []
            for p in passes:
                results.append(GroupPassResult(norad_cat_id=norad, sat_name=name, pass_info=p))
            pct = int((i + 1) / total * 100) if total > 0 else 100
            self.progress.emit(pct)
        if not self._cancelled:
            results.sort(key=lambda r: r.pass_info.aos)
        self.finished_results.emit(results)


# ---------------------------------------------------------------------------
# Widget factories
# ---------------------------------------------------------------------------


def _make_dt_edit(*, show_now_button: bool = True) -> _NowDateTimeEdit:
    """Return a _NowDateTimeEdit with a UTC calendar popup.

    Args:
        show_now_button: If True (default), include the 'Current Time' button.
                         Pass False for 'To' fields where the button is not needed.
    """
    edit = _NowDateTimeEdit(show_now_button=show_now_button)
    edit.setDisplayFormat("yyyy-MM-dd HH:mm")
    return edit


def _make_quick_combo() -> QComboBox:
    """Return a QComboBox populated with quick range options."""
    combo = QComboBox()
    for label, _hours in _QUICK_RANGES:
        combo.addItem(label)
    combo.setFixedWidth(68)
    combo.setToolTip(_("Set 'To' = 'From' + selected offset"))
    return combo


# ---------------------------------------------------------------------------
# PassPanel
# ---------------------------------------------------------------------------


class PassPanel(QWidget):
    """
    Upcoming Passes panel (2-tab layout).

    Tab 1 "Target" — pass list for the selected satellite (date/time range, quick buttons, Search)
    Tab 2 "Group"  — pass list for all filtered satellites (background search, CSV export)

    Both tabs collapse the date-range controls, quick-range selector, search/cancel buttons
    and (for Group) the pagination + export into a single toolbar row so the result table
    receives the maximum available vertical space.

    Call ``set_use_utc(False)`` to switch all datetime displays to local time.
    """

    pass_selected: Signal = Signal(object)  # PassInfo
    target_search_requested: Signal = Signal(object, object)  # (start: datetime, end: datetime)
    highlight_satellite: Signal = Signal(int)  # norad_cat_id
    group_results_ready: Signal = Signal(object)  # list[GroupPassResult]

    _PAGE_SIZE: int = 50
    _TARGET_COLS: tuple[str, ...] = (
        "AOS (UTC)",
        "Max El",
        "LOS (UTC)",
        "Duration",
        "AZ In",
        "AZ Out",
        "Quality",
    )
    _GROUP_COLS: tuple[str, ...] = (
        "Satellite",
        "AOS (UTC)",
        "Max El",
        "LOS (UTC)",
        "Duration",
        "AZ In",
        "Quality",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._passes: list[PassInfo] = []
        self._predictor: PassPredictor | None = None
        self._sat_list: list[tuple[int, str]] = []
        self._group_results: list[GroupPassResult] = []
        self._group_page: int = 0
        self._worker: _GroupSearchWorker | None = None
        self._cache_key: _CacheKey | None = None
        self._cache_results: list[GroupPassResult] = []
        self._pending_cache_key: _CacheKey | None = None
        self._use_utc: bool = True
        self._celestial_engine: CelestialEngine | None = None
        self._observer_lat: float = 0.0
        self._observer_lon: float = 0.0
        self._observer_elev_m: float = 0.0
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 0)
        layout.setSpacing(0)
        self._tabs = QTabWidget()
        title = QLabel("  Upcoming Passes  ")
        title.setStyleSheet("font-weight: bold; color: black;")
        self._tabs.setCornerWidget(title, Qt.Corner.TopRightCorner)
        self._tabs.addTab(self._build_target_tab(), _("Target"))
        self._tabs.addTab(self._build_group_tab(), _("Group"))
        layout.addWidget(self._tabs)

    def _build_target_tab(self) -> QWidget:
        """Build the Target tab (single toolbar row + result table)."""
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setContentsMargins(2, 2, 2, 2)
        vbox.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(4)

        row.addWidget(QLabel(_("From:")))
        self._target_from = _make_dt_edit()
        self._target_from.setMaximumWidth(130)
        row.addWidget(self._target_from)

        row.addWidget(QLabel(_("To:")))
        self._target_to = _make_dt_edit(show_now_button=False)
        self._target_to.setMaximumWidth(130)
        row.addWidget(self._target_to)

        # Timezone label: "(UTC)" or "(Local)" — updated by set_use_utc()
        self._tz_label_target = QLabel("(UTC)")
        row.addWidget(self._tz_label_target)

        self._target_quick_combo = _make_quick_combo()
        self._target_quick_combo.activated.connect(self._on_target_quick_combo)
        row.addWidget(self._target_quick_combo)

        row.addStretch()

        search_btn = QPushButton(_("Search"))
        search_btn.clicked.connect(self._on_target_search)
        row.addWidget(search_btn)

        vbox.addLayout(row)

        self._target_table = QTableWidget(0, len(self._TARGET_COLS))
        self._target_table.setHorizontalHeaderLabels(list(self._TARGET_COLS))
        self._target_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._target_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._target_table.horizontalHeader().setStretchLastSection(True)
        self._target_table.verticalHeader().setVisible(False)
        self._target_table.verticalHeader().setDefaultSectionSize(22)
        self._target_table.itemSelectionChanged.connect(self._on_target_selection_changed)
        vbox.addWidget(self._target_table)

        self._reset_target_datetimes()
        return w

    def _build_group_tab(self) -> QWidget:
        """Build the Group tab (single toolbar row + progress + result table)."""
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setContentsMargins(2, 2, 2, 2)
        vbox.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(4)

        row.addWidget(QLabel(_("From:")))
        self._group_from = _make_dt_edit()
        self._group_from.setMaximumWidth(130)
        row.addWidget(self._group_from)

        row.addWidget(QLabel(_("To:")))
        self._group_to = _make_dt_edit(show_now_button=False)
        self._group_to.setMaximumWidth(130)
        row.addWidget(self._group_to)

        # Timezone label — updated by set_use_utc()
        self._tz_label_group = QLabel("(UTC)")
        row.addWidget(self._tz_label_group)

        row.addWidget(QLabel(_("Min El:")))
        self._group_min_el = QSpinBox()
        self._group_min_el.setRange(0, 90)
        self._group_min_el.setValue(5)
        self._group_min_el.setSuffix("°")
        self._group_min_el.setFixedWidth(56)
        row.addWidget(self._group_min_el)

        self._group_quick_combo = _make_quick_combo()
        self._group_quick_combo.activated.connect(self._on_group_quick_combo)
        row.addWidget(self._group_quick_combo)

        self._group_search_btn = QPushButton(_("Search"))
        self._group_search_btn.clicked.connect(self._on_group_search)
        row.addWidget(self._group_search_btn)

        self._group_cancel_btn = QPushButton(_("Cancel"))
        self._group_cancel_btn.clicked.connect(self._on_group_cancel)
        self._group_cancel_btn.setEnabled(False)
        row.addWidget(self._group_cancel_btn)

        row.addStretch()

        self._prev_btn = QPushButton("←")
        self._prev_btn.setFixedWidth(26)
        self._prev_btn.clicked.connect(self._on_prev_page)
        row.addWidget(self._prev_btn)

        self._page_label = QLabel("Page 1")
        row.addWidget(self._page_label)

        self._next_btn = QPushButton("→")
        self._next_btn.setFixedWidth(26)
        self._next_btn.clicked.connect(self._on_next_page)
        row.addWidget(self._next_btn)

        self._export_btn = QPushButton(_("Export CSV"))
        self._export_btn.clicked.connect(self._on_export_csv)
        row.addWidget(self._export_btn)

        vbox.addLayout(row)

        self._group_progress = QProgressBar()
        self._group_progress.setRange(0, 100)
        self._group_progress.setVisible(False)
        vbox.addWidget(self._group_progress)

        self._group_table = QTableWidget(0, len(self._GROUP_COLS))
        self._group_table.setHorizontalHeaderLabels(list(self._GROUP_COLS))
        self._group_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._group_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._group_table.horizontalHeader().setStretchLastSection(True)
        self._group_table.verticalHeader().setVisible(False)
        self._group_table.verticalHeader().setDefaultSectionSize(22)
        self._group_table.cellClicked.connect(self._on_group_cell_clicked)
        self._group_table.itemSelectionChanged.connect(self._on_group_selection_changed)
        vbox.addWidget(self._group_table)

        self._reset_group_datetimes()
        return w

    # ------------------------------------------------------------------ #
    # Timezone support
    # ------------------------------------------------------------------ #

    def set_use_utc(self, use_utc: bool) -> None:
        """Switch all datetime controls and table AOS columns between UTC and local time.

        Args:
            use_utc: True → display and accept UTC; False → local system time.
        """
        if use_utc == self._use_utc:
            return
        self._use_utc = use_utc

        # Update all four datetime edits
        for edit in (self._target_from, self._target_to, self._group_from, self._group_to):
            edit.set_use_utc(use_utc)

        # Update timezone labels
        tz_text = "(UTC)" if use_utc else _("(Local)")
        self._tz_label_target.setText(tz_text)
        self._tz_label_group.setText(tz_text)

        # Update table column headers
        aos_header = "AOS (UTC)" if use_utc else _("AOS (Local)")
        los_header = "LOS (UTC)" if use_utc else _("LOS (Local)")
        # Target: AOS=col0, LOS=col2 — Group: AOS=col1, LOS=col3
        target_aos = self._target_table.horizontalHeaderItem(0)
        target_los = self._target_table.horizontalHeaderItem(2)
        group_aos = self._group_table.horizontalHeaderItem(1)
        group_los = self._group_table.horizontalHeaderItem(3)
        if target_aos is not None:
            target_aos.setText(aos_header)
        if target_los is not None:
            target_los.setText(los_header)
        if group_aos is not None:
            group_aos.setText(aos_header)
        if group_los is not None:
            group_los.setText(los_header)

        # Refresh table rows with new timezone formatting
        self._populate_target_table(self._passes)
        if self._group_results:
            self._refresh_group_page()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _reset_target_datetimes(self) -> None:
        now = datetime.now(UTC)
        self._target_from.setDateTime(_utc_to_display_qdatetime(now, self._use_utc))
        self._target_to.setDateTime(
            _utc_to_display_qdatetime(now + timedelta(hours=24), self._use_utc)
        )

    def _reset_group_datetimes(self) -> None:
        now = datetime.now(UTC)
        self._group_from.setDateTime(_utc_to_display_qdatetime(now, self._use_utc))
        self._group_to.setDateTime(
            _utc_to_display_qdatetime(now + timedelta(hours=24), self._use_utc)
        )

    def _populate_target_table(self, passes: list[PassInfo]) -> None:
        self._target_table.setRowCount(0)
        for p in passes:
            row = self._target_table.rowCount()
            self._target_table.insertRow(row)
            self._target_table.setItem(row, 0, QTableWidgetItem(_format_aos(p.aos, self._use_utc)))
            self._target_table.setItem(row, 1, QTableWidgetItem(f"{p.max_elevation_deg:.1f}°"))
            self._target_table.setItem(row, 2, QTableWidgetItem(_format_aos(p.los, self._use_utc)))
            mins, secs = divmod(int(p.duration_s), 60)
            self._target_table.setItem(row, 3, QTableWidgetItem(f"{mins}m {secs:02d}s"))
            self._target_table.setItem(row, 4, QTableWidgetItem(f"{p.aos_azimuth_deg:.0f}°"))
            self._target_table.setItem(row, 5, QTableWidgetItem(f"{p.los_azimuth_deg:.0f}°"))
            quality = pass_quality(p.max_elevation_deg)
            q_item = QTableWidgetItem(quality)
            q_item.setForeground(QUALITY_COLORS[quality])
            self._target_table.setItem(row, 6, q_item)

    def _refresh_group_page(self) -> None:
        self._group_table.setRowCount(0)
        start = self._group_page * self._PAGE_SIZE
        end = start + self._PAGE_SIZE
        for r in self._group_results[start:end]:
            row = self._group_table.rowCount()
            self._group_table.insertRow(row)
            sat_item = QTableWidgetItem(r.sat_name)
            sat_item.setData(Qt.ItemDataRole.UserRole, r.norad_cat_id)
            self._group_table.setItem(row, 0, sat_item)
            p = r.pass_info
            self._group_table.setItem(row, 1, QTableWidgetItem(_format_aos(p.aos, self._use_utc)))
            self._group_table.setItem(row, 2, QTableWidgetItem(f"{p.max_elevation_deg:.1f}°"))
            self._group_table.setItem(row, 3, QTableWidgetItem(_format_aos(p.los, self._use_utc)))
            mins, secs = divmod(int(p.duration_s), 60)
            self._group_table.setItem(row, 4, QTableWidgetItem(f"{mins}m {secs:02d}s"))
            self._group_table.setItem(row, 5, QTableWidgetItem(f"{p.aos_azimuth_deg:.0f}°"))
            quality = pass_quality(p.max_elevation_deg)
            q_item = QTableWidgetItem(quality)
            q_item.setForeground(QUALITY_COLORS[quality])
            self._group_table.setItem(row, 6, q_item)
        total_pages = max(1, (len(self._group_results) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)
        self._page_label.setText(
            f"Page {self._group_page + 1}/{total_pages}  ({len(self._group_results)} passes)"
        )
        self._prev_btn.setEnabled(self._group_page > 0)
        self._next_btn.setEnabled(end < len(self._group_results))

    # ------------------------------------------------------------------ #
    # Callbacks — Target tab
    # ------------------------------------------------------------------ #

    def _on_target_quick_combo(self, index: int) -> None:
        """Apply the quick range selected from the combo box to the Target tab."""
        _label, hours = _QUICK_RANGES[index]
        self._on_target_quick(hours)

    def _on_target_quick(self, hours: int) -> None:
        start = _qdatetime_to_dt(self._target_from.dateTime())
        self._target_to.setDateTime(
            _utc_to_display_qdatetime(start + timedelta(hours=hours), self._use_utc)
        )

    def _on_target_search(self) -> None:
        start = _qdatetime_to_dt(self._target_from.dateTime())
        end = _qdatetime_to_dt(self._target_to.dateTime())
        self.target_search_requested.emit(start, end)

    def _on_target_selection_changed(self) -> None:
        selected = self._target_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        if 0 <= row < len(self._passes):
            self.pass_selected.emit(self._passes[row])

    # ------------------------------------------------------------------ #
    # Callbacks — Group tab
    # ------------------------------------------------------------------ #

    def _on_group_quick_combo(self, index: int) -> None:
        """Apply the quick range selected from the combo box to the Group tab."""
        _label, hours = _QUICK_RANGES[index]
        self._on_group_quick(hours)

    def _on_group_quick(self, hours: int) -> None:
        start = _qdatetime_to_dt(self._group_from.dateTime())
        self._group_to.setDateTime(
            _utc_to_display_qdatetime(start + timedelta(hours=hours), self._use_utc)
        )

    def _on_group_search(self) -> None:
        if self._predictor is None or not self._sat_list:
            QMessageBox.information(
                self, _("Group Search"), _("No satellites or predictor available.")
            )
            return
        start = _qdatetime_to_dt(self._group_from.dateTime())
        end = _qdatetime_to_dt(self._group_to.dateTime())
        min_el = float(self._group_min_el.value())
        norads: tuple[int, ...] = tuple(n for n, _ in self._sat_list)
        key: _CacheKey = (start, end, min_el, norads)
        if key == self._cache_key:
            self._group_results = self._cache_results
            self._group_page = 0
            self._refresh_group_page()
            return
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait()
        self._group_results = []
        self._group_page = 0
        self._group_table.setRowCount(0)
        self._group_progress.setValue(0)
        self._group_progress.setVisible(True)
        self._group_search_btn.setEnabled(False)
        self._group_cancel_btn.setEnabled(True)
        self._pending_cache_key = key
        self._worker = _GroupSearchWorker(
            self._predictor,
            self._sat_list,
            start,
            end,
            min_el,
            self,
            celestial_engine=self._celestial_engine,
            observer_lat=self._observer_lat,
            observer_lon=self._observer_lon,
            observer_elev_m=self._observer_elev_m,
        )
        self._worker.progress.connect(self._on_group_progress)
        self._worker.finished_results.connect(self._on_group_results)
        self._worker.start()

    def _on_group_cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
        self._group_progress.setVisible(False)
        self._group_search_btn.setEnabled(True)
        self._group_cancel_btn.setEnabled(False)

    def _on_group_progress(self, pct: int) -> None:
        self._group_progress.setValue(pct)

    def _on_group_results(self, results: object) -> None:
        result_list: list[GroupPassResult] = results  # type: ignore[assignment]
        self._group_results = result_list
        if self._pending_cache_key is not None:
            self._cache_key = self._pending_cache_key
            self._cache_results = result_list
        self._group_page = 0
        self._group_progress.setVisible(False)
        self._group_search_btn.setEnabled(True)
        self._group_cancel_btn.setEnabled(False)
        self._refresh_group_page()
        self.group_results_ready.emit(result_list)

    def _on_group_cell_clicked(self, row: int, col: int) -> None:
        if col == 0:
            item = self._group_table.item(row, 0)
            if item is not None:
                norad = item.data(Qt.ItemDataRole.UserRole)
                if norad is not None:
                    self.highlight_satellite.emit(int(norad))

    def _on_group_selection_changed(self) -> None:
        selected = self._group_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        idx = self._group_page * self._PAGE_SIZE + row
        if 0 <= idx < len(self._group_results):
            self.pass_selected.emit(self._group_results[idx].pass_info)

    def _on_prev_page(self) -> None:
        if self._group_page > 0:
            self._group_page -= 1
            self._refresh_group_page()

    def _on_next_page(self) -> None:
        if (self._group_page + 1) * self._PAGE_SIZE < len(self._group_results):
            self._group_page += 1
            self._refresh_group_page()

    def _on_export_csv(self) -> None:
        if not self._group_results:
            QMessageBox.information(self, _("Export CSV"), _("No results to export."))
            return
        path, _filter = QFileDialog.getSaveFileName(self, _("Export CSV"), "", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "Satellite",
                        "NORAD",
                        "AOS (UTC)",
                        "Max El (deg)",
                        "LOS (UTC)",
                        "Duration",
                        "AZ In (deg)",
                        "AZ Out (deg)",
                        "Quality",
                    ]
                )
                for r in self._group_results:
                    p = r.pass_info
                    mins, secs = divmod(int(p.duration_s), 60)
                    writer.writerow(
                        [
                            r.sat_name,
                            r.norad_cat_id,
                            p.aos.strftime("%Y-%m-%d %H:%M:%S"),
                            f"{p.max_elevation_deg:.1f}",
                            p.los.strftime("%Y-%m-%d %H:%M:%S"),
                            f"{mins}m {secs:02d}s",
                            f"{p.aos_azimuth_deg:.0f}",
                            f"{p.los_azimuth_deg:.0f}",
                            pass_quality(p.max_elevation_deg),
                        ]
                    )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, _("Export Error"), str(exc))

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_passes(self, passes: list[PassInfo]) -> None:
        """Set the pass list on the Target tab (called directly from outside)."""
        self._passes = passes
        self._populate_target_table(passes)

    def clear(self) -> None:
        """Clear the pass list on the Target tab."""
        self._passes = []
        self._target_table.setRowCount(0)

    def set_pass_predictor(self, predictor: PassPredictor | None) -> None:
        """Set the pass predictor used by the Group tab search."""
        self._predictor = predictor

    def set_satellites(self, sat_list: list[tuple[int, str]]) -> None:
        """Set the satellite list for the Group tab search. Call on filter change."""
        self._sat_list = sat_list
        # Invalidate the cache because the satellite list has changed
        self._cache_key = None
        self._cache_results = []

    def show_target_tab(self) -> None:
        """Switch to the Target sub-tab (e.g. when a specific satellite is selected)."""
        self._tabs.setCurrentIndex(0)

    def show_group_tab(self) -> None:
        """Switch to the Group sub-tab (e.g. when an auto-search filter is selected)."""
        self._tabs.setCurrentIndex(1)

    def auto_search(self, hours: float) -> None:
        """Run a Group search over [now, now + hours) without a manual Search click.

        Sets the Group tab's From/To fields to the computed window and reuses
        the existing search pipeline (background worker + cache), so this is
        cheap to call repeatedly for an unchanged satellite list/window. Silently
        does nothing if the predictor isn't ready yet or the satellite list is
        empty (e.g. an empty Favorite group) — unlike a manual Search click,
        an automatic trigger should never pop up a dialog.
        """
        if self._predictor is None or not self._sat_list:
            return
        now = datetime.now(UTC)
        self._group_from.setDateTime(_utc_to_display_qdatetime(now, self._use_utc))
        self._group_to.setDateTime(
            _utc_to_display_qdatetime(now + timedelta(hours=hours), self._use_utc)
        )
        self._on_group_search()

    def set_celestial_engine(self, engine: CelestialEngine) -> None:
        """Provide the CelestialEngine so that MOON_ID entries can be searched."""
        self._celestial_engine = engine

    def set_observer_location(self, lat: float, lon: float, elev_m: float) -> None:
        """Update the observer location used for Moon event calculation."""
        self._observer_lat = lat
        self._observer_lon = lon
        self._observer_elev_m = elev_m

    # Any is used explicitly here so that mypy does not flag ANN methods
    @staticmethod
    def _noop(*_args: Any) -> None:  # noqa: ANN401
        pass
